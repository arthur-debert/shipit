from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import load_context

from shipit.prstate.model import FunnelState, ReviewFunnelCheck
from shipit.prstate.reviewers import DEFAULT_WAIT_WINDOW, by_name
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import TaskState, evaluate

NOW = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


def test_default_window_is_twenty_minutes():
    assert DEFAULT_WAIT_WINDOW == timedelta(minutes=20)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _local_ctx(age_min: float, window: int | None = None):
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [
        ReviewFunnelCheck("codex-local", "IN_PROGRESS", None, _iso(age_min))
    ]
    if window is not None:
        ctx.roster = Roster((RosterEntry(name="codex", window_seconds=window),))
    return ctx, [by_name("codex")]


def _app_ctx(age_min: float, window: int | None = None):
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.reviews = [r for r in ctx.reviews if "copilot" not in r.author.lower()]
    ctx.requested_logins = ["Copilot"]
    ctx.requested_at = {"Copilot": _iso(age_min)}
    if window is not None:
        ctx.roster = Roster((RosterEntry(name="copilot", window_seconds=window),))
    return ctx, [by_name("copilot")]


_KINDS = {
    "local": (_local_ctx, "codex", "codex-local", FunnelState.IN_FLIGHT),
    "app": (_app_ctx, "copilot", "copilot", FunnelState.REQUESTED),
}

_MATRIX = [
    ("local", "within_default", 5, None, False),
    ("local", "past_default", 30, None, True),
    ("app", "within_default", 5, None, False),
    ("app", "past_default", 30, None, True),
    ("local", "override_lengthens_holds", 30, 60 * 60, False),
    ("app", "override_lengthens_holds", 30, 60 * 60, False),
    ("local", "override_shortens_times_out", 5, 2 * 60, True),
    ("app", "override_shortens_times_out", 5, 2 * 60, True),
]


@pytest.mark.parametrize(
    "kind,label,age_min,window,expect_timeout",
    _MATRIX,
    ids=[f"{row[0]}-{row[1]}" for row in _MATRIX],
)
def test_wait_window_matrix(kind, label, age_min, window, expect_timeout):
    builder, key, display, held_state = _KINDS[kind]
    ctx, required = builder(age_min, window)
    status = evaluate(ctx, required=required)

    if expect_timeout:
        assert status.reviewer_funnel[key].state is FunnelState.TIMED_OUT
        assert status.state is TaskState.READY
        assert status.degraded == {display: FunnelState.TIMED_OUT.value}
    else:
        assert status.reviewer_funnel[key].state is held_state
        assert status.state is TaskState.REVIEWS_PENDING
        assert status.degraded == {}


@pytest.mark.parametrize("kind", ["local", "app"])
def test_exactly_at_the_window_boundary_still_holds(kind):
    builder, key, _display, held_state = _KINDS[kind]
    at_edge, required = builder(20)
    assert evaluate(at_edge, required=required).reviewer_funnel[key].state is held_state

    past_edge, required = builder(20 + 1 / 60)
    assert (
        evaluate(past_edge, required=required).reviewer_funnel[key].state
        is FunnelState.TIMED_OUT
    )


def test_app_reviewer_without_a_request_time_never_ages():
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.reviews = [r for r in ctx.reviews if "copilot" not in r.author.lower()]
    ctx.requested_logins = ["Copilot"]
    ctx.requested_at = {}
    ctx.now = datetime(2030, 1, 1, tzinfo=UTC)
    status = evaluate(ctx, required=[by_name("copilot")])
    assert status.reviewer_funnel["copilot"].state is FunnelState.REQUESTED
    assert status.state is TaskState.REVIEWS_PENDING


def test_a_terminal_breadcrumb_is_never_re_aged():
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [
        ReviewFunnelCheck("codex-local", "COMPLETED", "FAILURE", _iso(999))
    ]
    status = evaluate(ctx, required=[by_name("codex")])
    assert status.reviewer_funnel["codex"].state is FunnelState.FAILED


def test_no_injected_now_holds_rather_than_times_out():
    ctx, required = _local_ctx(999)
    ctx.now = None
    status = evaluate(ctx, required=required)
    assert status.reviewer_funnel["codex"].state is FunnelState.IN_FLIGHT
    assert status.state is TaskState.REVIEWS_PENDING
