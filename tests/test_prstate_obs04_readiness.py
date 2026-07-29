from __future__ import annotations

import pytest
from conftest import load_context

from shipit.identity import Sha
from shipit.prstate.model import FunnelState, Review, ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import TaskState, evaluate

_REQUIRED = [by_name("copilot"), by_name("codex")]

_CODEX_BOT = "adr-codex-review[bot]"


_FIXTURE_HEAD = Sha("beef030000000000000000000000000000000000")


def _ctx(funnel=None, codex_review=False, head=_FIXTURE_HEAD):
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [funnel] if funnel is not None else []
    if codex_review:
        ctx.reviews.append(
            Review(
                review_id=9102,
                author=_CODEX_BOT,
                state="COMMENTED",
                commit_id=head if isinstance(head, Sha) else Sha(head),
                body="",
            )
        )
    return ctx


def _codex_funnel(status, conclusion):
    return ReviewFunnelCheck(
        reviewer="codex-local",
        status=status,
        conclusion=conclusion,
        started_at="2026-01-01T00:25:00Z",
    )


_MATRIX = [
    ("never_requested", None, False, FunnelState.NEVER_REQUESTED, True, False),
    (
        "in_flight",
        ("IN_PROGRESS", None),
        False,
        FunnelState.IN_FLIGHT,
        True,
        False,
    ),
    (
        "posted_breadcrumb",
        ("COMPLETED", "SUCCESS"),
        False,
        FunnelState.POSTED,
        False,
        False,
    ),
    ("failed", ("COMPLETED", "FAILURE"), False, FunnelState.FAILED, False, True),
    ("empty", ("COMPLETED", "NEUTRAL"), False, FunnelState.EMPTY, False, True),
    (
        "timed_out",
        ("COMPLETED", "TIMED_OUT"),
        False,
        FunnelState.TIMED_OUT,
        False,
        True,
    ),
]


@pytest.mark.parametrize(
    "label,breadcrumb,codex_review,expected_state,holds,degraded",
    _MATRIX,
    ids=[row[0] for row in _MATRIX],
)
def test_funnel_state_matrix(
    label, breadcrumb, codex_review, expected_state, holds, degraded
):
    funnel = _codex_funnel(*breadcrumb) if breadcrumb else None
    ctx = _ctx(funnel=funnel, codex_review=codex_review)
    status = evaluate(ctx, required=_REQUIRED)

    assert status.reviewer_funnel["codex"].state is expected_state

    if holds:
        assert status.state is TaskState.REVIEWS_PENDING
    else:
        assert status.state is TaskState.READY

    if degraded:
        assert status.degraded == {"codex-local": expected_state.value}
    else:
        assert status.degraded == {}


@pytest.mark.parametrize("conclusion", ["FAILURE", "NEUTRAL", "TIMED_OUT"])
def test_non_success_required_reviewer_is_reviewed_non_blocking(conclusion):
    ctx = _ctx(funnel=_codex_funnel("COMPLETED", conclusion))
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.READY
    assert status.next_action
    assert "codex-local" in status.degraded


def test_never_requested_required_reviewer_holds():
    status = evaluate(_ctx(), required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "codex" in status.next_action
    assert status.degraded == {}


def test_in_flight_required_reviewer_holds_and_says_wait():
    status = evaluate(
        _ctx(funnel=_codex_funnel("IN_PROGRESS", None)), required=_REQUIRED
    )
    assert status.state is TaskState.REVIEWS_PENDING
    assert "wait (already requested" in status.next_action
    assert "request for the current head" not in status.next_action


def test_degraded_surfaces_even_while_another_reviewer_holds():
    ctx = _ctx(funnel=_codex_funnel("COMPLETED", "FAILURE"))
    ctx.reviews = [r for r in ctx.reviews if "copilot" not in r.author.lower()]
    ctx.requested_logins = ["Copilot"]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.degraded == {"codex-local": "failed"}


def test_unprovisioned_but_posted_review_is_settled_never_blocked():
    ctx = _ctx(funnel=None, codex_review=True)
    status = evaluate(ctx, required=_REQUIRED)
    assert status.reviewer_funnel["codex"].state is FunnelState.POSTED
    assert status.state is TaskState.READY
    assert status.degraded == {}


def test_genuinely_no_outcome_holds_but_never_blocks():
    status = evaluate(_ctx(funnel=None, codex_review=False), required=_REQUIRED)
    assert status.reviewer_funnel["codex"].state is FunnelState.NEVER_REQUESTED
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.state is not TaskState.BLOCKED


def test_app_reviewer_folds_from_lifecycle_not_a_breadcrumb():
    ctx = _ctx(funnel=_codex_funnel("COMPLETED", "SUCCESS"))
    status = evaluate(ctx, required=_REQUIRED)
    assert status.reviewer_funnel["copilot"].state is FunnelState.POSTED
    assert status.reviewer_funnel["copilot"].check_status is None


def test_empty_distinguished_from_failed_by_conclusion():
    empty = evaluate(
        _ctx(funnel=_codex_funnel("COMPLETED", "NEUTRAL")), required=_REQUIRED
    )
    failed = evaluate(
        _ctx(funnel=_codex_funnel("COMPLETED", "FAILURE")), required=_REQUIRED
    )
    assert empty.degraded == {"codex-local": "empty"}
    assert failed.degraded == {"codex-local": "failed"}
