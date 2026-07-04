"""The next-action dispatcher — a DEEP module (ADR-0002), promoted to the engine.

A PURE decision: given a :class:`~shipit.prstate.state.TaskStatus` (the engine's
snapshot — lifecycle state + context), return the single act to take. The act's
EXECUTION is an injected boundary (the :class:`Acts` protocol), so the decision
itself makes no GitHub calls and is unit-testable per `TaskState` without a
network. :class:`NextActs` is the concrete boundary the CLI injects — it lives
here WITH the dispatcher (CLI01-WS03) because reviewer selection and the two
mutating acts are engine logic, not click glue.

The mapping is the PRD's "`pr next` behavior" table (prf01-pr-flow.md):

    no_pr           → report "create a draft PR" (the human's act)
    reviews_pending → request/re-request the pending required reviewers, OR
                      report "waiting" when they are already requested / in
                      progress on the head
    addressing      → surface the open threads (read-only)
    reviewed        → report "mergeability computing — re-check"
    validating      → report "CI running — wait"
    ready           → flip draft→ready (guarded), then stop
    blocked         → report the real blocker

Failing checks outrank review requests (#352): a red-checks PR never reaches
the request branch — the engine ranks it BLOCKED (fix CI first) with
`to_request` suppressed, so `pr next` reports the CI fix instead of burning
token-billed reviews on a head that is about to change.

The split that keeps this a deep module: :func:`dispatch` decides WHICH act and
with WHAT message from the (already-computed) `TaskStatus`; the `Acts` boundary
decides HOW to carry it out (talk to `gh`). The decision never branches on a
reviewer's name and never touches the network — it reads the STRUCTURED state the
engine already settled (the lifecycle `TaskState`, and for REVIEWS_PENDING the
`to_request` set of required reviewers needing a (re-)request) and routes to one
act. It does NOT parse the human-facing `next_action` prose: a wording change to
that text cannot re-route the dispatcher (OBS04-WS04, absorbing #24.1). Swapping a
real `Acts` for a fake is the whole test seam.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .. import events
from ..pr import PrId
from .errors import PrStateError
from .flip import guarded_flip
from .request import request_reviewers
from .reviewers import required_adapters
from .roster import Roster
from .state import TaskState, TaskStatus

#: The engine's logger (shared name across :mod:`shipit.prstate`): the single
#: action a `pr next` invocation takes is a lifecycle milestone (LOG02 spray,
#: ADR-0029) — the durable twin of the verb's "action:" render.
logger = logging.getLogger("shipit.prstate")


class Acts(Protocol):
    """The injected execution boundary — the three things `pr next` can DO.

    Each method performs one act against the live PR and returns a human-readable
    line describing what happened (rendered as the "action taken"). The dispatcher
    calls exactly ONE of these per invocation; everything else is `report`, the
    no-op act that just states the situation. Splitting execution out here is what
    makes :func:`dispatch` a pure decision: a test passes a recording fake and
    asserts which method fired, with no `gh` in sight.
    """

    def report(self, status: TaskStatus) -> str:
        """Report-only: state the situation, change nothing. Returns the line."""
        ...

    def request_review(self, status: TaskStatus) -> str:
        """Request / re-request the pending required reviewers on the head.

        The act for REVIEWS_PENDING when a reviewer still needs requesting. The
        boundary owns the request placement + attach-verify (it delegates to the
        shared `request.request_reviewers` service). Returns the line describing
        who was requested.
        """
        ...

    def flip_ready(self, status: TaskStatus) -> str:
        """Flip draft→ready — the single hand-off act for READY. Returns the line.

        The boundary re-checks readiness before flipping (the shared guarded
        flip), so a stale status can never flip a not-actually-ready PR.
        """
        ...


def dispatch(status: TaskStatus, acts: Acts) -> str:
    """Route a `TaskStatus` to the ONE act and perform it via `acts`.

    Pure decision, injected execution: this function chooses the act purely from
    `status` (no network) and delegates the doing to `acts`. Returns the act's
    line (what happened), suitable for rendering alongside the resulting status.

    The only state that mutates the PR is READY (flip) and the request branch of
    REVIEWS_PENDING; every other state routes to `report` — `pr next` does at
    most one safe step.
    """
    state = status.state

    if state is TaskState.REVIEWS_PENDING and status.to_request:
        # Route on the engine's STRUCTURED decision, not next_action prose (#24.1):
        # `status.to_request` lists the required reviewers whose funnel state says
        # they need a (re-)request now (NEVER_REQUESTED — never asked, or a prior
        # review staled by a push). When it is non-empty there is a reviewer to
        # (re-)request; when it is empty every holding reviewer is in-flight within
        # its window (REQUESTED / IN_FLIGHT — WS03 already aged any past-window one
        # into a settled TIMED_OUT, which would not hold here at all), so the only
        # act is to wait. A wording change to next_action cannot re-route this.
        line = acts.request_review(status)
    elif state is TaskState.READY:
        # Flip draft→ready. A degraded set (required reviewers settled non-success)
        # does NOT block the flip — the engine already let a degraded-but-otherwise
        # -ready PR reach READY (ADR-0006); the dispatcher just hands it off.
        line = acts.flip_ready(status)
    else:
        # no_pr, addressing, reviewed, validating, blocked — and the
        # REVIEWS_PENDING wait case — all report-only. The engine's next_action
        # already carries the right human-language instruction (create a draft PR /
        # triage open threads / re-check mergeability / wait for CI / the real
        # blocker), so the report act just surfaces it. The STATE drives this
        # routing; the prose is only what `report` echoes, never a routing key.
        line = acts.report(status)
    # The action-taken milestone (LOG02 convergence): the single step this
    # dispatch performed, durable alongside the verb's user-facing render. The
    # ``pr`` key is present-when-bound (a NO_PR status has none), never null.
    if status.pr is not None:
        logger.info(
            "pr#%s next action taken — %s",
            status.pr,
            line,
            extra={"pr": status.pr},
        )
    return line


class NextActs:
    """The concrete :class:`Acts` boundary the `pr next` verb injects.

    Each method performs one act against the live PR and returns the line stating
    what it did. `report` is the no-op act (surface the engine's next-action);
    `request_review` and `flip_ready` are the two mutating acts. Lives with the
    dispatcher because reviewer SELECTION (which names in ``to_request`` map to
    which adapters) is engine logic (CLI01-WS03), not verb glue.
    """

    def __init__(
        self,
        pr: PrId,
        roster: Roster | None = None,
        sightings: events.Sightings | None = None,
    ) -> None:
        self._pr = pr
        # The verb's ONE loaded Roster (CLI01-WS04), threaded in so the request
        # act reads reviewer settings off the same value the engine evaluated —
        # never a config re-read. None only for the no-PR report path, which
        # never requests.
        self._roster = roster if roster is not None else Roster()
        # The verb's invocation-wide first-sight registry (ADR-0032 / ADR-0021
        # rule 4): threaded into the ready act's guarded re-gather so a
        # milestone the first gather already tagged is not re-tagged there.
        self._sightings = sightings

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
            It never re-derives the set from the lifecycle `status.reviewers`
            map: that map cannot tell a genuinely-IN_FLIGHT local-agent reviewer
            (its `review: <agent>-local` check run still running) apart from a
            never-requested one — both read lifecycle `not_requested`, because a
            local agent has no native `review_requested` edge — so re-deriving here
            would re-poke a reviewer mid-review. The engine already settled the
            funnel/window state and the required-vs-best-effort split into
            `to_request`, so the act reads THAT (the act-side completion of WS04's
            structured-state routing; the dispatcher routes on the same field).
          * EXECUTION (delegated): the actual request + #614 attach-verify is the
            canonical :func:`~shipit.prstate.request.request_reviewers` — the ONE
            request path `pr review request` also uses. We pass `force=True`
            because we have ALREADY filtered to the reviewers that need acting on;
            the service then places each request and polls until its
            `review_requested` edge is verified (or reports it dropped). A
            silently-dropped edge is a hard failure: surface it as a `PrStateError`
            so the caller renders a clean stderr + non-zero exit, exactly like
            `pr review request`.
        """
        by_name = {r.name: r for r in required_adapters(self._roster)}
        selected = [by_name[name] for name in status.to_request if name in by_name]
        if not selected:
            return f"no requestable reviewer to (re-)request — {status.next_action}"
        # force=True: selection is done above, so the service requests exactly
        # these and attach-verifies each remote edge. ExecError/PrStateError (e.g. a
        # deferred local-agent reviewer, or a gh failure) propagates to the caller.
        result = request_reviewers(self._pr, selected, self._roster, force=True)
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
        # The verb's already-loaded Roster rides into the flip's re-check, so the
        # READY path never resolves reviewer settings twice (CLI01-WS04).
        flipped = guarded_flip(self._pr, self._roster, sightings=self._sightings)
        return f"flipped draft→ready — {flipped.next_action}"
