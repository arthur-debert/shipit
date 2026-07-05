"""OBS04-WS03 — the uniform wait window ages an in-flight reviewer to *timed-out*.

WS02 made the engine treat a `TIMED_OUT` funnel state as settled + degraded; this
suite owns PRODUCING that state from the injected "now". The window is a pure
function of (now, the reviewer's OWN request timestamp, its window) — the engine
calls no clock — so every case is deterministic with a FIXED injected "now":

  * **Aged from each reviewer's own request timestamp.** A LOCAL reviewer ages
    against its `review: <agent>-local` check run's `started_at`; an APP reviewer
    ages against its `review_requested` edge time (`ctx.requested_at`).
  * **20m default + per-reviewer override.** Uniform across reviewer kinds. The
    `[reviewers]` `window` option (carried on the reviewer's Roster entry,
    `ctx.roster`) lengthens OR shortens it for one reviewer without touching the
    others.
  * **Within window → holds; past window → timed-out → settled + degraded.**
  * **No timestamp → never ages.** A reviewer with no recorded request time can't
    be aged — the window never invents a timeout from absent data.

Everything asserts EXTERNAL engine behaviour (the funnel state, the readiness verdict,
the degraded set) from a recorded snapshot + an injected "now" — never a clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import load_context

from shipit.prstate.model import FunnelState, ReviewFunnelCheck
from shipit.prstate.reviewers import DEFAULT_WAIT_WINDOW, by_name
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import TaskState, evaluate

# The base fixture's injected "now". Every request timestamp below is expressed as
# an offset from it, so a case is "X minutes into the wait" regardless of wall time.
NOW = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


def test_default_window_is_twenty_minutes():
    """The shipped default the spec fixes (ADR-0006): 20m, uniform across kinds."""
    assert DEFAULT_WAIT_WINDOW == timedelta(minutes=20)


def _iso(minutes_ago: float) -> str:
    """An ISO-8601 request timestamp `minutes_ago` before the fixture's now."""
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _local_ctx(age_min: float, window: int | None = None):
    """An otherwise-ready PR whose codex-local run is IN_PROGRESS, started
    `age_min` before now. Required set = codex only (copilot stays best-effort), so
    codex's window verdict alone decides holds vs settled."""
    ctx = load_context("local_reviewer_otherwise_ready")  # now = 00:30
    ctx.review_funnel = [
        ReviewFunnelCheck("codex-local", "IN_PROGRESS", None, _iso(age_min))
    ]
    if window is not None:
        ctx.roster = Roster((RosterEntry(name="codex", window_seconds=window),))
    return ctx, [by_name("codex")]


def _app_ctx(age_min: float, window: int | None = None):
    """An otherwise-ready PR where copilot is REQUESTED (its review dropped, a
    pending request edge placed `age_min` before now via the timeline). Required
    set = copilot only, so copilot's window verdict alone decides holds vs settled."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.reviews = [r for r in ctx.reviews if "copilot" not in r.author.lower()]
    ctx.requested_logins = ["Copilot"]
    ctx.requested_at = {"Copilot": _iso(age_min)}
    if window is not None:
        ctx.roster = Roster((RosterEntry(name="copilot", window_seconds=window),))
    return ctx, [by_name("copilot")]


# (kind, builder, reviewer-key, display-name, held-state-within-window)
_KINDS = {
    "local": (_local_ctx, "codex", "codex-local", FunnelState.IN_FLIGHT),
    "app": (_app_ctx, "copilot", "copilot", FunnelState.REQUESTED),
}

# (kind, label, age_min, window_seconds_override, expect_timeout)
_MATRIX = [
    # default 20m window: within holds, past times out — for BOTH reviewer kinds.
    ("local", "within_default", 5, None, False),
    ("local", "past_default", 30, None, True),
    ("app", "within_default", 5, None, False),
    ("app", "past_default", 30, None, True),
    # a per-reviewer override LENGTHENS the window: a 30m-old reviewer that would
    # time out under the 20m default still HOLDS under a 60m override.
    ("local", "override_lengthens_holds", 30, 60 * 60, False),
    ("app", "override_lengthens_holds", 30, 60 * 60, False),
    # a per-reviewer override SHORTENS the window: a 5m-old reviewer that would hold
    # under the 20m default TIMES OUT under a 2m override.
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
        # Past window → TIMED_OUT → settled + degraded; the otherwise-ready PR flips
        # to READY (a timed-out reviewer never holds it), naming the degradation.
        assert status.reviewer_funnel[key].state is FunnelState.TIMED_OUT
        assert status.state is TaskState.READY
        assert status.degraded == {display: FunnelState.TIMED_OUT.value}
    else:
        # Within window → still its holding state → the PR holds at reviews-pending,
        # nothing degraded (the review is still legitimately coming).
        assert status.reviewer_funnel[key].state is held_state
        assert status.state is TaskState.REVIEWS_PENDING
        assert status.degraded == {}


@pytest.mark.parametrize("kind", ["local", "app"])
def test_exactly_at_the_window_boundary_still_holds(kind):
    """Age == window holds (the window is EXCEEDED, not reached); one second past it
    times out. Pinned for both kinds so the boundary is identical across them."""
    builder, key, _display, held_state = _KINDS[kind]
    at_edge, required = builder(20)  # exactly the 20m default
    assert evaluate(at_edge, required=required).reviewer_funnel[key].state is held_state

    past_edge, required = builder(20 + 1 / 60)  # 20m and 1s
    assert (
        evaluate(past_edge, required=required).reviewer_funnel[key].state
        is FunnelState.TIMED_OUT
    )


def test_app_reviewer_without_a_request_time_never_ages():
    """A requested App reviewer with NO recorded edge time can't be aged — it HOLDS
    no matter how old the PR is. The window never invents a timeout from absent
    data (the missing-timestamp branch of the pure timeout function)."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.reviews = [r for r in ctx.reviews if "copilot" not in r.author.lower()]
    ctx.requested_logins = ["Copilot"]
    ctx.requested_at = {}  # no timeline edge time recorded
    ctx.now = datetime(2030, 1, 1, tzinfo=UTC)  # decades later
    status = evaluate(ctx, required=[by_name("copilot")])
    assert status.reviewer_funnel["copilot"].state is FunnelState.REQUESTED
    assert status.state is TaskState.REVIEWS_PENDING


def test_a_terminal_breadcrumb_is_never_re_aged():
    """A reviewer already SETTLED at a terminal outcome (here a producer-recorded
    FAILURE) is never re-aged by the window, however old its `started_at`: only an
    in-flight / requested reviewer can time out."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [
        ReviewFunnelCheck("codex-local", "COMPLETED", "FAILURE", _iso(999))
    ]
    status = evaluate(ctx, required=[by_name("codex")])
    assert status.reviewer_funnel["codex"].state is FunnelState.FAILED


def test_no_injected_now_holds_rather_than_times_out():
    """With no "now" on the snapshot the window can't be aged, so an in-flight
    reviewer HOLDS — the engine never falls back to a wall clock to force a timeout."""
    ctx, required = _local_ctx(999)  # ancient, but...
    ctx.now = None  # ...no clock to age against
    status = evaluate(ctx, required=required)
    assert status.reviewer_funnel["codex"].state is FunnelState.IN_FLIGHT
    assert status.state is TaskState.REVIEWS_PENDING
