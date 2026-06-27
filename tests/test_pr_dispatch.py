"""Unit tests for the next-action dispatcher — the PURE decision (WS06).

Per `TaskState`, assert the dispatcher routes to the EXPECTED act on the injected
`Acts` boundary. No network, no engine re-test: each test hands a hand-built
`TaskStatus` (the engine's output shape) to `dispatch` with a recording fake and
asserts which act fired. This is the deep module's test seam.
"""

from __future__ import annotations

import pytest

from shipit.prstate.state import TaskState, TaskStatus
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


def _status(state: TaskState, next_action: str = "x") -> TaskStatus:
    return TaskStatus(state=state, next_action=next_action, pr=42)


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
    """A REVIEWS_PENDING next-action naming a reviewer to (re-)request → request."""
    acts = RecordingActs()
    status = _status(
        TaskState.REVIEWS_PENDING,
        "waiting on required review(s): copilot — request for the current head: copilot",
    )
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_rerequest_clause_also_requests():
    acts = RecordingActs()
    status = _status(
        TaskState.REVIEWS_PENDING,
        "waiting on required review(s): copilot — RE-REQUEST for the current head "
        "(a prior review is stale after a push): copilot",
    )
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_only_waiting_reports():
    """When every pending reviewer is already requested/in-progress → report, not
    re-poke (PRD user story 5/6: don't re-request a reviewer already mid-review)."""
    acts = RecordingActs()
    status = _status(
        TaskState.REVIEWS_PENDING,
        "waiting on required review(s): copilot — wait (already requested / in "
        "flight on the current head): copilot",
    )
    dispatch(status, acts)
    assert acts.called == "report"


def test_dispatch_returns_the_acts_line():
    """The dispatcher returns the act's line verbatim (what `pr next` prints)."""
    acts = RecordingActs()
    line = dispatch(_status(TaskState.READY), acts)
    assert line == "flipped"
