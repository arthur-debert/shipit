"""`shipit pr wait` — block until the review loop reaches a state (ADR-0034).

The ONE verb that blocks: it polls the same evaluator `pr status` reads —
resolve the PR → gather → evaluate, repeated at the fixed tool-owned cadence —
and exits the moment the awaited state arrives. `--until reviews-in` fires when
the latest round's reviews have all landed (the moment an addressing agent
becomes dispatchable); `--until ready` when the engine reports READY. `pr
status` / `pr next` stay pure single-shot reads; a driver parks behind this
verb instead of re-deriving a poll cadence per session.

A `ready` wait stops EARLY — distinct exit code :data:`EXIT_ACTIONABLE` (4) —
the moment it observes `addressing` (#583): that state is caller-actionable
(the parked process is the one actor whose action unblocks READY), so polling
through it is a guaranteed dead wait to the deadline. The verb reports the
state it stopped on plus the engine's next-action line; the caller addresses
the round (dispatch the shepherd) and re-waits.

The cadence is config, never a flag: the shipped default is
:data:`~shipit.prstate.wait.POLL_INTERVAL_SECONDS` (60s), overridable via the
`[reviewers]` table-level ``poll_interval`` key in `.shipit.toml`. `--timeout`
is the HARD deadline (required semantics — it always has a value; default 30m):
on expiry the verb exits promptly with the DISTINCT code :data:`EXIT_TIMEOUT`
(3) and a state report (the engine's next-action line, naming what is still
outstanding) — an advisory outcome for the supervisor, not a failure of the PR,
and distinct from exit 1 (a real gh/auth/
config failure through the error shell) and exit 2 (usage).

On every poll tick where the observed state CHANGED, one progress line goes to
STDERR (stdout stays the typed result, so `--json` output is parseable and a
supervisor tails progress on the other stream) and a flow-log event lands
(ADR-0032: ``wait.started`` / ``wait.state_changed`` / ``wait.fired`` /
``wait.timed_out``).

ADR-0030 glue + renderers: the shared PR-target param, one domain call chain
(the loop itself is the engine's :func:`~shipit.prstate.wait.wait_for`), a
frozen :class:`~shipit.prstate.wait.WaitResult` rendered by the pure
:func:`format_wait` through the shared emit, with runtime failures mapped by
the one :func:`~.._errors.cli_errors` shell. Unlike `pr status`, a branch with
NO PR is a runtime REFUSAL (exit 1), not a normal report: there is nothing to
wait on, and a waiter that polls a nonexistent PR until its deadline would
just relocate the hang.
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

#: The two-tier exit contract's third code (ADR-0034): the wait DID NOT FIRE
#: before its hard deadline. Distinct from 0 (fired), 1 (runtime failure) and
#: 2 (usage) so a supervising script can branch on "still waiting" without
#: parsing output.
EXIT_TIMEOUT = 3

#: The deadlock-guard exit code (#583): the wait stopped EARLY on a
#: caller-actionable state (a `ready` wait observing `addressing` — the
#: awaited state cannot arrive until the waiting caller itself acts). Distinct
#: from 0 (fired) and 3 (deadline expired, state may still arrive) so a
#: supervising script can branch straight to dispatching the round's
#: addressing without parsing output.
EXIT_ACTIONABLE = 4


def format_wait(result: WaitResult) -> str:
    """The pure text renderer: one wait-outcome line, then the shared status block.

    Reuses :func:`~._format.format_status` (the render-seam helper the whole
    `pr` family shares) so the final observed status renders identically to
    `pr status`. The timeout and actionable lines both carry the engine's
    next-action line — the state report naming what is still outstanding
    (ADR-0034) or what the caller must now do (#583).
    """
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
    """Block until PR reaches the awaited review-loop state, then report.

    PR is the number; omitted, it resolves the current branch's PR. The ONE
    verb that blocks (ADR-0034): polls the same evaluator `pr status` reads at
    the fixed config-owned interval (default 60s; `[reviewers].poll_interval`
    in .shipit.toml), printing one stderr line per observed state change. Exits
    0 when the state arrives, 4 when a `ready` wait observes `addressing` (a
    state only the waiting caller can clear — act, then re-wait), 3 on the
    --timeout hard deadline.
    """
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
    """Resolve → loop(gather → evaluate) until fired/deadline → render.

    ``repo`` is the identity half of the PR target: omitted (the CLI path), the
    root context's ambient repo — resolved once per invocation (ADR-0030); a
    direct caller (a test) injects it as a value, and ``sleep`` / ``monotonic``
    are the same test seam for the clock.

    Returns 0 when the awaited state arrived, :data:`EXIT_ACTIONABLE` (4) when
    the wait stopped early on a caller-actionable state (#583 — address the
    round, then re-wait), :data:`EXIT_TIMEOUT` (3) when the hard deadline
    expired first. A branch with NO PR is a refusal (there is
    nothing to wait on — open the draft PR first), and a real gh/auth/config
    failure — at resolution or on any poll tick — propagates to the
    :func:`~shipit.verbs._errors.cli_errors` shell (clean ``error: …`` stderr +
    exit 1) instead of being retried until the deadline.
    """
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError(
            "no PR for this branch — nothing to wait on; "
            "open the draft PR first, then `shipit pr wait`"
        )
    # The ONE reviewer-config read of this invocation (CLI01-WS04): the Roster
    # rides every gather, and the poll cadence is read off it here — config
    # with the documented default, never a per-call flag (ADR-0034).
    roster = load_roster()
    poll_seconds = (
        roster.poll_interval
        if roster.poll_interval is not None
        else wait_engine.POLL_INTERVAL_SECONDS
    )
    # ONE first-sight registry for the whole wait (ADR-0032 / ADR-0021 rule 4):
    # the loop gathers many snapshots, and each observational milestone (a
    # landed review, a reviewed head, a fired breaker) is tagged on the tick
    # that FIRST sees it — once per invocation, however long the wait runs.
    sightings = events.Sightings()

    def poll() -> TaskStatus:
        return evaluate(gather(target, roster, sightings=sightings))

    def on_change(status: TaskStatus) -> None:
        # The tail-able progress line (ADR-0034), one per observed change —
        # on STDERR so stdout stays the typed result (`--json` parseable).
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
