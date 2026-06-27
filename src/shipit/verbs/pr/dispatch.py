"""The next-action dispatcher — a DEEP module (ADR-0002).

A PURE decision: given a :class:`~shipit.prstate.state.TaskStatus` (the engine's
snapshot — lifecycle state + context), return the single act to take. The act's
EXECUTION is an injected boundary (the :class:`Acts` protocol), so the decision
itself makes no GitHub calls and is unit-testable per `TaskState` without a
network.

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

from typing import Protocol

from ...prstate.state import TaskState, TaskStatus


class Acts(Protocol):
    """The injected execution boundary — the three things `pr next` can DO.

    Each method performs one act against the live PR and returns a human-readable
    line describing what happened (printed as the "action taken"). The dispatcher
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
        shared `_request.request_reviewers` helper). Returns the line describing
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
    line (what happened), suitable for printing alongside the resulting status.

    The only state that mutates the PR is READY (flip) and the request branch of
    REVIEWS_PENDING; every other state routes to `report` — `pr next` does at
    most one safe step.
    """
    state = status.state

    if state is TaskState.REVIEWS_PENDING:
        # Route on the engine's STRUCTURED decision, not next_action prose (#24.1):
        # `status.to_request` lists the required reviewers whose funnel state says
        # they need a (re-)request now (NEVER_REQUESTED — never asked, or a prior
        # review staled by a push). When it is non-empty there is a reviewer to
        # (re-)request; when it is empty every holding reviewer is in-flight within
        # its window (REQUESTED / IN_FLIGHT — WS03 already aged any past-window one
        # into a settled TIMED_OUT, which would not hold here at all), so the only
        # act is to wait. A wording change to next_action cannot re-route this.
        if status.to_request:
            return acts.request_review(status)
        return acts.report(status)

    if state is TaskState.READY:
        # Flip draft→ready. A degraded set (required reviewers settled non-success)
        # does NOT block the flip — the engine already let a degraded-but-otherwise
        # -ready PR reach READY (ADR-0006); the dispatcher just hands it off.
        return acts.flip_ready(status)

    # no_pr, addressing, reviewed, validating, blocked — all report-only. The
    # engine's next_action already carries the right human-language instruction
    # (create a draft PR / triage open threads / re-check mergeability / wait for
    # CI / the real blocker), so the report act just surfaces it. The STATE drives
    # this routing; the prose is only what `report` echoes, never a routing key.
    return acts.report(status)
