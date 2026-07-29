"""`shipit pr wait` — block until the review loop reaches a state.

See docs/adr/0034-pr-wait-blocking-verb.md.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

import click

from ... import events
from ...gh import resolve_pr
from ...identity import Repo
from ...prstate import wait as wait_engine
from ...prstate.errors import PrStateError
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.state import TaskStatus, evaluate
from ...prstate.wait import Outcome, Until, WaitResult, wait_for
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import DURATION, json_option, pr_number_argument
from .._render import emit
from ._format import format_status

EXIT_TIMEOUT = 3

EXIT_ACTIONABLE = 4


def format_wait(result: WaitResult) -> str:
    """The pure text renderer: one wait-outcome line, then the shared status block."""
    if result.outcome is Outcome.FIRED:
        head = (
            f"wait: {result.until.value} fired after {result.ticks} poll(s) "
            f"({result.waited_seconds:.0f}s)"
        )
    elif result.outcome is Outcome.ACTIONABLE:
        head = (
            f"wait: stopped on {result.status.state.value} after {result.ticks} "
            f"poll(s) ({result.waited_seconds:.0f}s) — {result.until.value} cannot "
            f"arrive until this caller acts — {result.status.next_action}"
        )
    else:
        head = (
            f"wait: timed out after {result.ticks} poll(s) "
            f"({result.waited_seconds:.0f}s) — {result.status.next_action}"
        )
    return f"{head}\n{format_status(result.status)}"


@click.command(name="wait")
@pr_number_argument
@click.option(
    "--until",
    "until",
    type=click.Choice([u.value for u in Until]),
    required=True,
    help=(
        "The awaited state: `reviews-in` — the latest round's reviews have all "
        "landed (an addressing agent is dispatchable); `ready` — the engine "
        f"reports READY (stops early, exit {EXIT_ACTIONABLE}, on `addressing` — "
        "a state only the waiting caller can clear)."
    ),
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=DURATION,
    default="30m",
    show_default=True,
    help=(
        "Hard deadline (e.g. 30m, 900s). On expiry the verb exits promptly "
        f"with code {EXIT_TIMEOUT} and reports what it is still waiting on."
    ),
)
@json_option
def cmd(pr: int | None, until: str, timeout_seconds: float, as_json: bool) -> None:
    """Block until PR reaches the awaited review-loop state, then report."""
    raise SystemExit(
        run(pr, until=Until(until), timeout_seconds=timeout_seconds, as_json=as_json)
    )


@cli_errors
def run(
    pr: int | None = None,
    *,
    until: Until,
    timeout_seconds: float,
    as_json: bool = False,
    repo: Repo | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Resolve → loop(gather → evaluate) until fired/deadline → render; returns 0, EXIT_ACTIONABLE, or EXIT_TIMEOUT, and refuses a branch with no PR."""
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError(
            "no PR for this branch — nothing to wait on; "
            "open the draft PR first, then `shipit pr wait`"
        )
    roster = load_roster()
    poll_seconds = (
        roster.poll_interval
        if roster.poll_interval is not None
        else wait_engine.POLL_INTERVAL_SECONDS
    )
    sightings = events.Sightings()

    def poll() -> TaskStatus:
        return evaluate(gather(target, roster, sightings=sightings))

    def on_change(status: TaskStatus) -> None:
        print(
            f"pr#{target.number} wait: {status.state.value} — {status.next_action}",
            file=sys.stderr,
            flush=True,
        )

    result = wait_for(
        poll,
        pr=target.number,
        until=until,
        required_names=roster.required_names,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        on_change=on_change,
        sleep=sleep,
        monotonic=monotonic,
    )
    emit(result, format_wait, as_json=as_json)
    if result.outcome is Outcome.FIRED:
        return 0
    if result.outcome is Outcome.ACTIONABLE:
        return EXIT_ACTIONABLE
    return EXIT_TIMEOUT
