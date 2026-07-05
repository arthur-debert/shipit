"""OBS04-WS02 — the readiness pillars redefined over the funnel (ADR-0006).

WS01 made the snapshot CARRY the funnel breadcrumb; this suite pins what WS02
DECIDES from it:

  * **Funnel normalization** — each reviewer's signals fold to ONE `FunnelState`,
    behind the adapter interface (the engine never name-branches). A local
    reviewer reads its `review: <agent>-local` check run; an App reviewer folds
    from its lifecycle.
  * **Settled / Reviewed redefinition** — a required reviewer is SETTLED at any
    recorded terminal outcome (posted / empty / failed / timed-out), NOT only
    success. Reviewed = all required settled + posted-review threads resolved.
  * **Degraded, non-blocking** — failed / empty / timed-out settle but do NOT hold
    Ready; they collect into `TaskStatus.degraded` and surface on `pr status`.
  * **Holds** — only never-requested and in-flight hold the PR.
  * **Provisioning-as-flake** — an unprovisioned reviewer (no breadcrumb) whose
    review still posts reads POSTED → settled, never blocked.

Tests assert EXTERNAL behaviour from a recorded snapshot + a fixed "now": the
engine's state / degraded set, never an implementation detail. The wait-window
ageing of in-flight→timed-out is WS03; here the engine already treats a recorded
`timed-out` funnel state as settled+degraded, which is all WS02 owns.
"""

from __future__ import annotations

import pytest
from conftest import load_context

from shipit.prstate.model import FunnelState, Review, ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import TaskState, evaluate

# The required set every readiness test below uses: an App reviewer (copilot, always
# posted in the base fixture) PLUS a local-agent reviewer (codex) whose funnel
# signal each test varies. Passing it explicitly proves the engine is data-driven,
# not coupled to this repo's deployed `[reviewers]` policy.
_REQUIRED = [by_name("copilot"), by_name("codex")]

#: The codex local-agent bot login (`<app>-codex-review[bot]` matches the
#: `codex-review` slug + `[bot]` suffix). A review by it is what `detect` reads.
_CODEX_BOT = "adr-codex-review[bot]"


def _ctx(funnel=None, codex_review=False, head="beef03"):
    """Load the otherwise-ready base snapshot and inject a codex funnel signal.

    `funnel` is the codex-local breadcrumb (a `ReviewFunnelCheck` or None);
    `codex_review` adds a POSTED review by the codex bot on the head. The base has
    copilot already posted + CI green + a CLEAN merge, so the codex signal alone
    decides holds vs settled vs degraded.
    """
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [funnel] if funnel is not None else []
    if codex_review:
        ctx.reviews.append(
            Review(
                review_id=9102,
                author=_CODEX_BOT,
                state="COMMENTED",
                commit_id=head,
                body="",
            )
        )
    return ctx


def _codex_funnel(status, conclusion):
    # started_at is 5m before the base fixture's now (00:30) — comfortably WITHIN
    # the 20m default wait window, so an IN_PROGRESS breadcrumb HOLDS here (this
    # suite owns the within-window verdict); WS03's own suite owns aging an
    # in-flight run PAST the window into TIMED_OUT.
    return ReviewFunnelCheck(
        reviewer="codex-local",
        status=status,
        conclusion=conclusion,
        started_at="2026-01-01T00:25:00Z",
    )


# --- table-driven funnel-state × verdict matrix ----------------------------

# (label, breadcrumb, codex_review?) → (expected FunnelState, holds?, degraded?)
_MATRIX = [
    # never-requested: no breadcrumb, no review → holds, not degraded.
    ("never_requested", None, False, FunnelState.NEVER_REQUESTED, True, False),
    # in-flight WITHIN the window: an in_progress breadcrumb 5m old → holds. (WS03's
    # own suite covers an in-flight run aged PAST the window → timed-out.)
    (
        "in_flight",
        ("IN_PROGRESS", None),
        False,
        FunnelState.IN_FLIGHT,
        True,
        False,
    ),
    # posted via the breadcrumb's success conclusion → settled, not degraded.
    (
        "posted_breadcrumb",
        ("COMPLETED", "SUCCESS"),
        False,
        FunnelState.POSTED,
        False,
        False,
    ),
    # failed → settled + degraded (non-blocking).
    ("failed", ("COMPLETED", "FAILURE"), False, FunnelState.FAILED, False, True),
    # empty (the ADR-0005 neutral mapping) → settled + degraded.
    ("empty", ("COMPLETED", "NEUTRAL"), False, FunnelState.EMPTY, False, True),
    # timed-out (producer-recorded) → settled + degraded.
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

    # 1. The adapter normalized codex's signal to the expected funnel state.
    assert status.reviewer_funnel["codex"].state is expected_state

    # 2. Holds ⇒ the PR is parked at reviews-pending; settled ⇒ it is NOT.
    if holds:
        assert status.state is TaskState.REVIEWS_PENDING
    else:
        # Otherwise-ready base (copilot posted, CI green, CLEAN) ⇒ settled reaches
        # READY — a failed/empty/timed-out reviewer never holds it.
        assert status.state is TaskState.READY

    # 3. Degraded membership — only the non-success terminal outcomes appear, named
    #    under the reviewer's `<agent>-local` display name.
    if degraded:
        assert status.degraded == {"codex-local": expected_state.value}
    else:
        assert status.degraded == {}


# --- Reviewed / Ready redefinition (the named stories) ----------------------


@pytest.mark.parametrize("conclusion", ["FAILURE", "NEUTRAL", "TIMED_OUT"])
def test_non_success_required_reviewer_is_reviewed_non_blocking(conclusion):
    """A failed / empty / timed-out REQUIRED reviewer settles: the PR reaches READY
    (non-blocking) with that reviewer degraded — Reviewed is outcome-recorded, not
    review-succeeded (ADR-0006)."""
    ctx = _ctx(funnel=_codex_funnel("COMPLETED", conclusion))
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.READY
    assert status.next_action  # a real next action, not a block
    assert "codex-local" in status.degraded


def test_never_requested_required_reviewer_holds():
    """A never-requested required reviewer HOLDS the PR at reviews-pending so the
    review loop starts — it is not silently skipped."""
    status = evaluate(_ctx(), required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "codex" in status.next_action
    assert status.degraded == {}


def test_in_flight_required_reviewer_holds_and_says_wait():
    """An in-flight required reviewer holds, and the action is WAIT (not request) —
    even though a local reviewer's lifecycle has no requested edge, the funnel state
    folds the in_progress breadcrumb so the engine does not advise a duplicate run."""
    status = evaluate(
        _ctx(funnel=_codex_funnel("IN_PROGRESS", None)), required=_REQUIRED
    )
    assert status.state is TaskState.REVIEWS_PENDING
    assert "wait (already requested" in status.next_action
    assert "request for the current head" not in status.next_action


def test_degraded_surfaces_even_while_another_reviewer_holds():
    """codex failed (degraded) while copilot is still mid-flight: the PR holds on
    copilot, but codex's degradation is still surfaced — degraded is reported on
    every status, not only on a clean-but-degraded one."""
    ctx = _ctx(funnel=_codex_funnel("COMPLETED", "FAILURE"))
    # Drop copilot's posted review so it holds the PR (requested, no review yet).
    ctx.reviews = [r for r in ctx.reviews if "copilot" not in r.author.lower()]
    ctx.requested_logins = ["Copilot"]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING  # copilot still holds
    assert status.degraded == {"codex-local": "failed"}


# --- provisioning-as-flake (ADR-0005 / ADR-0006) ---------------------------


def test_unprovisioned_but_posted_review_is_settled_never_blocked():
    """A reviewer whose App lacks ``checks:write`` opens NO breadcrumb — but its
    review STILL posts (ADR-0005). That posted review settles it (POSTED), so the
    PR is READY, never blocked, with no degradation. Provisioning failure is a
    flake, not a block."""
    ctx = _ctx(funnel=None, codex_review=True)
    status = evaluate(ctx, required=_REQUIRED)
    assert status.reviewer_funnel["codex"].state is FunnelState.POSTED
    assert status.state is TaskState.READY
    assert status.degraded == {}


def test_genuinely_no_outcome_holds_but_never_blocks():
    """The residual provisioning case — NO breadcrumb AND no posted review — is, in
    a pure snapshot, indistinguishable from never-requested. It reads
    NEVER_REQUESTED → holds at reviews-pending with an actionable *request* step,
    NEVER a BLOCKED terminal state. So "not provisioned" never blocks the PR; the
    dispatcher advances it by requesting (ADR-0006)."""
    status = evaluate(_ctx(funnel=None, codex_review=False), required=_REQUIRED)
    assert status.reviewer_funnel["codex"].state is FunnelState.NEVER_REQUESTED
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.state is not TaskState.BLOCKED


# --- the funnel mapping lives behind the adapter, not the engine ------------


def test_app_reviewer_folds_from_lifecycle_not_a_breadcrumb():
    """An App reviewer (copilot) has no check-run breadcrumb: its funnel state comes
    straight from the lifecycle. A posted approval folds to POSTED."""
    ctx = _ctx(funnel=_codex_funnel("COMPLETED", "SUCCESS"))
    status = evaluate(ctx, required=_REQUIRED)
    assert status.reviewer_funnel["copilot"].state is FunnelState.POSTED
    assert status.reviewer_funnel["copilot"].check_status is None


def test_empty_distinguished_from_failed_by_conclusion():
    """The neutral→empty / failure→failed split (the producer's ADR-0005 mapping)
    is what lets the engine name the right "why" without the snapshot carrying the
    check-run output text."""
    empty = evaluate(
        _ctx(funnel=_codex_funnel("COMPLETED", "NEUTRAL")), required=_REQUIRED
    )
    failed = evaluate(
        _ctx(funnel=_codex_funnel("COMPLETED", "FAILURE")), required=_REQUIRED
    )
    assert empty.degraded == {"codex-local": "empty"}
    assert failed.degraded == {"codex-local": "failed"}
