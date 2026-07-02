"""`shipit pr next` — do the ONE next action, then report.

The act counterpart to the read-only `pr status`: resolve the PR → gather a
snapshot → evaluate it → route the lifecycle state through the next-action
:mod:`.dispatch`er to the single act, perform it, and report what happened plus
the resulting status. It is the SINGLE-SHOT form of release's looping `wait` —
there is NO polling loop here: `pr next` takes one safe step and returns; the
driver (a human or an outer loop) calls it again.

The dispatcher is a pure decision (state → act); the doing lives in
:class:`_NextActs`, the concrete :class:`~.dispatch.Acts` boundary this verb
injects. Only two acts mutate the PR — the REVIEWS_PENDING request and the READY
flip — and the flip goes through the SAME guarded re-check (`ready.guarded_flip`)
that `pr ready` uses, so `pr next` can never flip a not-actually-ready PR.

The request act delegates to the canonical reviewer-request helper
`verbs/pr/_request.py::request_reviewers(...)` (WS05) — the ONE attach-verified
request path `pr review request` also uses. `pr next` owns only the
reviewer-SELECTION, which it reads from the engine's structured
`TaskStatus.to_request` (the required reviewers needing a (re-)request now) rather
than re-deriving from the lifecycle map — so an in-flight local-agent reviewer is
never re-poked mid-review; the request placement + #614 attach-verify is the
shared helper's job.
"""

from __future__ import annotations

import logging
import sys

import click

from ... import execrun
from ...prstate.errors import PrStateError
from ...prstate.fetch import gather
from ...prstate.reviewers import required_reviewers
from ...prstate.state import TaskStatus, evaluate, no_pr
from . import ready as ready_verb
from . import status as status_verb
from ._request import request_reviewers
from ._resolve import resolve_pr
from .dispatch import dispatch

#: The `pr` verbs' logger (LOG02 spray, ADR-0029): the action `pr next` takes is
#: a lifecycle milestone — before this, the "action:" print was its only record.
logger = logging.getLogger("shipit.pr")


class _NextActs:
    """The concrete execution boundary `pr next` injects into the dispatcher.

    Each method performs one act against the live PR and returns the line stating
    what it did. `report` is the no-op act (surface the engine's next-action);
    `request_review` and `flip_ready` are the two mutating acts.
    """

    def __init__(self, pr: int) -> None:
        self._pr = pr

    def report(self, status: TaskStatus) -> str:
        # Report-only: nothing mutates. The engine's next_action already carries
        # the right instruction for no_pr / addressing / reviewed / validating /
        # blocked / waiting-reviews, so surface it verbatim.
        return f"no action taken — {status.next_action}"

    def request_review(self, status: TaskStatus) -> str:
        """Request/re-request the pending required reviewers on the head.

        Two concerns, deliberately split:

          * SELECTION (here): which reviewers to act on. This CONSUMES the engine's
            structured `status.to_request` — the required reviewers whose funnel
            state says they need a (re-)request NOW (NEVER_REQUESTED, or a prior
            review staled by a push) — and maps those names back to their adapters.
            It no longer re-derives the set from the lifecycle `status.reviewers`
            map: that map cannot tell a genuinely-IN_FLIGHT local-agent reviewer
            (its `review: <agent>-local` check run still running) apart from a
            never-requested one — both read lifecycle `not_requested`, because a
            local agent has no native `review_requested` edge — so re-deriving here
            would re-poke a reviewer mid-review. The engine already settled the
            funnel/window state and the required-vs-best-effort split into
            `to_request`, so the act reads THAT (the act-side completion of WS04's
            structured-state routing; the dispatcher routes on the same field).
          * EXECUTION (delegated): the actual request + #614 attach-verify is
            WS05's canonical `_request.request_reviewers` — the ONE request path
            `pr review request` also uses. We pass `force=True` because we have
            ALREADY filtered to the reviewers that need acting on; the helper then
            places each request and polls until its `review_requested` edge is
            verified (or reports it dropped). A silently-dropped edge is a hard
            failure: surface it as a `PrStateError` so the verb renders a clean stderr
            + non-zero exit, exactly like `pr review request`.
        """
        by_name = {r.name: r for r in required_reviewers()}
        selected = [by_name[name] for name in status.to_request if name in by_name]
        if not selected:
            return f"no requestable reviewer to (re-)request — {status.next_action}"
        # force=True: selection is done above, so the helper requests exactly
        # these and attach-verifies each remote edge. ExecError/PrStateError (e.g. a deferred
        # local-agent reviewer, or a gh failure) propagates to the verb.
        result = request_reviewers(self._pr, selected, force=True)
        if not result.ok:
            # A remote request edge was silently dropped (#614) — fail loud rather
            # than park the PR invisibly at reviews-pending.
            raise PrStateError(
                "review request dropped by GitHub (no review_requested edge "
                f"attached): {', '.join(result.dropped)} — re-run `pr next`"
            )
        acted = result.verified + result.in_flight
        if not acted:
            # Only no-op (auto-triggering) backends were selected — nothing placed.
            return f"no requestable reviewer to (re-)request — {status.next_action}"
        return f"requested review(s): {', '.join(acted)}"

    def flip_ready(self, status: TaskStatus) -> str:
        # The single hand-off. Go through the SHARED guarded re-check so a status
        # that moved since `gather` cannot flip a not-ready PR. guarded_flip
        # re-evaluates live and raises NotReady if it is no longer READY.
        flipped = ready_verb.guarded_flip(self._pr)
        return f"flipped draft→ready — {flipped.next_action}"


@click.command(name="next")
@click.argument("pr", required=False, type=int)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the resulting status as JSON."
)
def cmd(pr: int | None, as_json: bool) -> None:
    """Do the single next action for PR, then report it + the resulting status.

    PR is the number; omitted, it resolves the current branch's PR. Performs at
    most ONE step (request a review / flip draft→ready / report waiting/blocked)
    — the single-shot form of a wait loop, never a polling loop.
    """
    raise SystemExit(run(pr, as_json=as_json))


def run(pr: int | None = None, *, as_json: bool = False) -> int:
    """Resolve → gather → evaluate → dispatch → perform one act → report.

    Returns 0 on a performed/ reported action; non-zero on a real gh/auth failure
    or a guarded-flip refusal (a status that moved out of READY between gather and
    flip). A branch with no PR is a normal report (the act is the human's: create
    a draft PR), exit 0 — matching `pr status`.
    """
    resolved: int | None = None
    try:
        resolved = resolve_pr(pr)
        if resolved is None:
            status = no_pr()
            _report(_NextActs(0).report(status), status, as_json=as_json)
            return 0
        status = evaluate(gather(resolved), required=required_reviewers())
        action = dispatch(status, _NextActs(resolved))
    except ready_verb.NotReady as exc:
        # The guarded flip refused: the PR moved out of READY between the gather
        # and the flip. Report the real (refused) status as a clean non-zero.
        logger.warning(
            "pr#%s flip refused — not Ready (state=%s)",
            exc.status.pr,
            exc.status.state.value,
            extra={"pr": exc.status.pr},
        )
        print(f"refusing to flip: {exc}", file=sys.stderr)
        return 1
    except (execrun.ExecError, PrStateError) as exc:
        logger.error("pr next failed", exc_info=True, extra={"pr": resolved})
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # The action-taken milestone (LOG02 convergence): the single step this
    # invocation performed, durable alongside the user-facing "action:" print.
    logger.info(
        "pr#%s next action taken — %s", resolved, action, extra={"pr": resolved}
    )
    # Re-read the status AFTER a mutating act so the reported snapshot reflects
    # what just happened (e.g. a freshly-requested reviewer now REQUESTED). A
    # second gather is cheap and keeps the report honest; on report-only acts it
    # is the same status. Skipped when there is no PR (handled above).
    final = evaluate(gather(resolved), required=required_reviewers())
    _report(action, final, as_json=as_json)
    return 0


def _report(action: str, status: TaskStatus, *, as_json: bool) -> None:
    """Print the action taken, then the resulting status (reusing `status`'s render).

    Reuses :func:`status._emit` for the status block so `pr next` and `pr status`
    render identically. The action line is printed first (text) or carried under
    an ``action`` key (JSON).
    """
    if as_json:
        import json

        payload = {"action": action, "status": status.to_dict()}
        print(json.dumps(payload, indent=2))
        return
    print(f"action: {action}")
    status_verb._emit(status, as_json=False)
