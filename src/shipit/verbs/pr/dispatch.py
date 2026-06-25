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
reviewer's name and never touches the network — it reads the state the engine
already settled and routes to one act. Swapping a real `Acts` for a fake is the
whole test seam.
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
        boundary owns the request + attach check (see the WS05 reconcile seam in
        :mod:`.next_action`). Returns the line describing who was requested.
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
        # request/re-request the pending reviewers, UNLESS they are already
        # requested / in-progress on the head (then there is nothing to do but
        # wait). The engine already encoded that distinction in next_action: a
        # "wait (already requested ...)" with no request/re-request clause means
        # every pending reviewer is mid-flight. Routing on that keeps the
        # dispatcher reading the engine's decision rather than re-deriving it.
        if _only_waiting(status):
            return acts.report(status)
        return acts.request_review(status)

    if state is TaskState.READY:
        return acts.flip_ready(status)

    # no_pr, addressing, reviewed, validating, blocked — all report-only. The
    # engine's next_action already carries the right human-language instruction
    # (create a draft PR / triage open threads / re-check mergeability / wait for
    # CI / the real blocker), so the report act just surfaces it.
    return acts.report(status)


def _only_waiting(status: TaskStatus) -> bool:
    """True when every pending required reviewer is already requested/in-progress
    on the head — i.e. the act is to WAIT, not to (re-)request.

    The engine's `_reviews_pending_action` builds the next-action from up to
    three clauses — "request for the current head: …", "RE-REQUEST … : …", and
    "wait (already requested on the current head): …". When the only clause is
    the wait one, there is no reviewer to (re-)request and `pr next` reports
    waiting instead of poking a reviewer that is already mid-review. Keyed off
    the presence of the request/re-request verbs in the next-action text — the
    engine's own words — rather than re-inspecting reviewer lifecycles here, so
    the dispatcher stays a thin routing decision over the engine's output.
    """
    action = status.next_action
    return (
        "wait (already requested" in action
        and "request for the current head:" not in action
        and "RE-REQUEST for the current head" not in action
    )
