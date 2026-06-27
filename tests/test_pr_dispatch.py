"""Unit tests for the next-action dispatcher — the PURE decision (WS06).

Per `TaskState`, assert the dispatcher routes to the EXPECTED act on the injected
`Acts` boundary. No network, no engine re-test: each test hands a hand-built
`TaskStatus` (the engine's output shape) to `dispatch` with a recording fake and
asserts which act fired. This is the deep module's test seam.
"""

from __future__ import annotations

import pytest

from conftest import load_context
from shipit.prstate.model import ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import TaskState, TaskStatus, evaluate
from shipit.verbs.pr.dispatch import dispatch


class RecordingActs:
    """A fake `Acts` that records which method fired (no GitHub)."""

    def __init__(self) -> None:
        self.called: str | None = None

    def report(self, status: TaskStatus) -> str:
        self.called = "report"
        return "reported"

    def request_review(self, status: TaskStatus) -> str:
        self.called = "request_review"
        return "requested"

    def flip_ready(self, status: TaskStatus) -> str:
        self.called = "flip_ready"
        return "flipped"


def _status(
    state: TaskState,
    next_action: str = "x",
    *,
    to_request: list[str] | None = None,
    degraded: dict[str, str] | None = None,
) -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action=next_action,
        pr=42,
        to_request=list(to_request or []),
        degraded=dict(degraded or {}),
    )


@pytest.mark.parametrize(
    "state,expected",
    [
        (TaskState.NO_PR, "report"),
        (TaskState.ADDRESSING, "report"),
        (TaskState.REVIEWED, "report"),
        (TaskState.VALIDATING, "report"),
        (TaskState.BLOCKED, "report"),
        (TaskState.READY, "flip_ready"),
    ],
)
def test_each_state_routes_to_expected_act(state, expected):
    acts = RecordingActs()
    dispatch(_status(state), acts)
    assert acts.called == expected


def test_reviews_pending_with_a_reviewer_to_request_requests():
    """A REVIEWS_PENDING with a required reviewer to request (NEVER_REQUESTED, so
    the engine listed it in `to_request`) → request, decided from structure."""
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=["copilot"])
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_rerequest_also_requests():
    """A stale-after-push reviewer (re-request) rides the SAME `to_request` set and
    routes to the single request act — request and re-request are one act."""
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=["copilot"])
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_only_waiting_reports():
    """When every holding reviewer is in-flight-within-window (`to_request` empty)
    → report, not re-poke (PRD user story 5/6: don't re-request a reviewer already
    mid-review). WS03 guarantees a past-window reviewer is already settled
    (TIMED_OUT), so an empty `to_request` here means strictly in-flight."""
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=[])
    dispatch(status, acts)
    assert acts.called == "report"


def test_reviews_pending_routing_ignores_next_action_wording():
    """The #24.1 regression: routing is decided from `to_request`, NEVER the
    `next_action` prose. The SAME structured state with WILDLY different wording —
    including prose that names a "request" verb — routes identically; and an empty
    `to_request` reports even when the prose screams "request"."""
    # Same (empty) to_request, three different next_action strings → all report.
    for prose in (
        "waiting on required review(s): copilot — wait (already requested ...)",
        "request for the current head: copilot",  # misleading prose, empty signal
        "any arbitrary wording at all",
    ):
        acts = RecordingActs()
        dispatch(_status(TaskState.REVIEWS_PENDING, prose, to_request=[]), acts)
        assert acts.called == "report"
    # Same (non-empty) to_request, prose that says "wait" → still request.
    acts = RecordingActs()
    dispatch(
        _status(
            TaskState.REVIEWS_PENDING,
            "wait (already requested / in flight on the current head): copilot",
            to_request=["copilot"],
        ),
        acts,
    )
    assert acts.called == "request_review"


def test_degraded_but_ready_still_flips():
    """A degraded set (required reviewers settled non-success) does NOT block the
    flip: a READY PR with degraded reviewers still routes to flip_ready (ADR-0006).
    The engine already let it reach READY; the dispatcher hands it off."""
    acts = RecordingActs()
    status = _status(
        TaskState.READY,
        "run `pr ready`",
        degraded={"codex-local": "failed"},
    )
    dispatch(status, acts)
    assert acts.called == "flip_ready"


def test_dispatch_returns_the_acts_line():
    """The dispatcher returns the act's line verbatim (what `pr next` prints)."""
    acts = RecordingActs()
    line = dispatch(_status(TaskState.READY), acts)
    assert line == "flipped"


# --- end-to-end: real engine FunnelState → to_request → routed act ----------
#
# The unit tests above hand `dispatch` a hand-built `TaskStatus`. These prove the
# WHOLE chain: a recorded snapshot → `evaluate` folds each reviewer's signals to a
# FunnelState and settles `to_request`/`degraded` → `dispatch` routes off that
# structure to the expected act. No prose anywhere on the path.

# Required set for the local-reviewer cases: an App reviewer (copilot, posted in
# the base fixture) + a local-agent reviewer (codex) whose breadcrumb we vary.
_REQUIRED = [by_name("copilot"), by_name("codex")]


def _codex_breadcrumb(status, conclusion):
    # started_at 5m before the base fixture's now (00:30) → WITHIN the 20m window,
    # so an IN_PROGRESS breadcrumb reads IN_FLIGHT (holds), not aged-out TIMED_OUT.
    return ReviewFunnelCheck(
        reviewer="codex-local",
        status=status,
        conclusion=conclusion,
        started_at="2026-01-01T00:25:00Z",
    )


def test_e2e_never_requested_routes_to_request():
    """A never-requested required reviewer → engine settles `to_request` non-empty
    → dispatch requests (the default copilot-only required set, copilot absent)."""
    status = evaluate(load_context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request  # the engine flagged a (re-)request
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_e2e_stale_after_push_routes_to_request():
    """A rerun=True reviewer with a review staled by a push → re-request: same
    `to_request` set, same request act (request and re-request are one act)."""
    ctx = load_context("copilot_stale_needs_rerequest")
    ctx.reviewer_rerun = {"copilot": True}
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST" in status.next_action  # prose says re-request…
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "request_review"  # …and routing agrees, off structure


def test_e2e_in_flight_within_window_routes_to_wait():
    """An in-flight-within-window required reviewer → engine leaves `to_request`
    empty (it is the wait case) → dispatch reports, never re-pokes."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [_codex_breadcrumb("IN_PROGRESS", None)]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == []  # in-flight → wait, nothing to (re-)request
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "report"


def test_e2e_degraded_but_ready_routes_to_flip():
    """A required reviewer settled non-success (degraded) on an otherwise-ready PR
    → engine reaches READY with a degraded set → dispatch flips (degraded does not
    block the hand-off)."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [_codex_breadcrumb("COMPLETED", "FAILURE")]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.READY
    assert status.degraded == {"codex-local": "failed"}
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "flip_ready"
