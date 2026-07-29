from __future__ import annotations

from shipit.prstate.reviewers import by_name
from shipit.prstate.state import TaskState, evaluate

_COPILOT_ONLY = [by_name("copilot")]


def test_live_addressing_real_payload(context):
    status = evaluate(context("live_addressing_pr342"), required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert status.reviewers == {
        "copilot": "done_comments",
        "coderabbit": "not_requested",
        "gemini": "done_comments",
        "codex": "not_requested",
        "agy": "not_requested",
    }
    assert status.open_threads == 2
    assert status.cycles == 1
    assert status.breaker == "no-major-finding"


def test_live_ready_real_payload(context):
    ctx = context("live_ready_pr342")
    status = evaluate(ctx, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.open_threads == 0
    assert status.checks.value == "green"
    assert status.mergeable == "MERGEABLE"
