"""`shipit pr next` — do the ONE next action, then report. Glue + renderers.

The act counterpart to the read-only `pr status`: resolve the PR → gather a
snapshot → evaluate it → route the lifecycle state through the engine's
next-action dispatcher (:mod:`shipit.prstate.dispatch`) to the single act,
perform it, and report what happened plus the resulting status. There is NO
polling loop here: `pr next` takes one safe step and returns. Blocking until
the next state change is `shipit pr wait`'s job (ADR-0034) — the ONE verb that
blocks — so the driver parks behind `pr wait`, then calls `pr next` again;
`pr next` itself stays a pure single-shot read-then-act.

Everything that DECIDES or DOES lives in the engine (CLI01-WS03): the
dispatcher is a pure decision (state → act); the doing is the engine's
:class:`~shipit.prstate.dispatch.NextActs` boundary (reviewer selection, the
canonical attach-verified request service, the SAME guarded flip `pr ready`
uses — so `pr next` can never flip a not-actually-ready PR). This module is
ADR-0030 glue + renderers: the shared PR-target param, one domain call chain,
a frozen :class:`NextResult` rendered by the pure :func:`format_next` through
the shared emit, with runtime failures (including the engine's ``NotReady``
refusal) mapped by the one :func:`~.._errors.cli_errors` shell.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from ... import events
from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.dispatch import NextActs, dispatch
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.state import TaskStatus, evaluate, no_pr
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import json_option, pr_number_argument
from .._render import emit
from ._format import format_status


@dataclass(frozen=True)
class NextResult:
    """The verb's typed result: the action taken + the resulting status.

    ``action`` is the dispatcher's line (what the one step did); ``status`` is
    the post-act snapshot. ``to_dict`` is the ``--json`` surface, serialized by
    the shared render seam.
    """

    action: str
    status: TaskStatus

    def to_dict(self) -> dict:
        return {"action": self.action, "status": self.status.to_dict()}


def format_next(result: NextResult) -> str:
    """The pure text renderer: the action line, then the shared status block.

    Reuses :func:`~._format.format_status` (the render-seam helper `pr status`
    also uses) so `pr next` and `pr status` render the status identically —
    shared through the seam, never a cross-verb import.
    """
    return f"action: {result.action}\n{format_status(result.status)}"


@click.command(name="next")
@pr_number_argument
@json_option
def cmd(pr: int | None, as_json: bool) -> None:
    """Do the single next action for PR, then report it + the resulting status.

    PR is the number; omitted, it resolves the current branch's PR. Performs at
    most ONE step (request a review / flip draft→ready / report waiting/blocked)
    and returns — never a polling loop; blocking on the next state change is
    `shipit pr wait`'s job (ADR-0034).
    """
    raise SystemExit(run(pr, as_json=as_json))


@cli_errors
def run(
    pr: int | None = None, *, as_json: bool = False, repo: Repo | None = None
) -> int:
    """Resolve → gather → evaluate → dispatch → perform one act → render.

    ``repo`` is the identity half of the PR target: omitted (the CLI path), the
    root context's ambient repo — resolved once per invocation (ADR-0030); a
    direct caller (a test) injects it as a value.

    Returns 0 on a performed / reported action. A real gh/auth failure, a
    silently-dropped request edge, or a guarded-flip refusal (a status that
    moved out of READY between gather and flip — the engine's ``NotReady``)
    propagates to the :func:`~shipit.verbs._errors.cli_errors` shell (clean
    ``error: …`` stderr + exit 1). A branch with no PR is a normal report (the
    act is the human's: create a draft PR), exit 0 — matching `pr status`.
    """
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        status = no_pr()
        # The report act inline: with no PR there is no target to construct
        # an act boundary around — the one action is the human's.
        emit(
            NextResult(f"no action taken — {status.next_action}", status),
            format_next,
            as_json=as_json,
        )
        return 0
    # The ONE reviewer-config read of this invocation (CLI01-WS04): the Roster
    # rides the snapshot for BOTH evaluates below and feeds the request act's
    # selection / run-options — one value, never re-resolved.
    roster = load_roster()
    # The ONE first-sight registry of this invocation (ADR-0032 / ADR-0021 rule
    # 4): `pr next` gathers up to three snapshots (gather → the ready act's
    # guarded re-gather → the report re-gather), so the observational dev-cycle
    # events dedupe against this one value — each milestone tagged at most once
    # per invocation, with no module global for a test suite to reset.
    sightings = events.Sightings()
    status = evaluate(gather(target, roster, sightings=sightings))
    action = dispatch(status, NextActs(target, roster, sightings))
    # Re-read the status AFTER a mutating act so the reported snapshot reflects
    # what just happened (e.g. a freshly-requested reviewer now REQUESTED). A
    # second gather is cheap and keeps the report honest; on report-only acts it
    # is the same status. Skipped when there is no PR (handled above).
    final = evaluate(gather(target, roster, sightings=sightings))
    emit(NextResult(action, final), format_next, as_json=as_json)
    return 0
