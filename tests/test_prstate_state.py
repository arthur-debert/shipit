"""State machine: scenario -> TaskState, plus check-rollup classification."""

from __future__ import annotations

import dataclasses

import pytest

from shipit.identity import Sha
from shipit.prstate.model import ReadinessView, Review, readiness_view
from shipit.prstate.reviewers import by_name
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import (
    ChecksState,
    TaskState,
    classify_checks,
    evaluate,
    no_pr,
)

# Full, validated commit identities (COR02): the current head, an earlier
# (stale) head, and a generic head for single-commit scenarios.
NEW = Sha("beef" * 10)
OLD = Sha("dead" * 10)
HEAD = Sha("abcd" * 10)

# The PR CORE now lives on the composed (frozen) `PR`, so overriding a core field
# means replacing `view.pr`, not the view. This helper routes core overrides
# (number/head_sha/is_draft/base_ref/merge_state) to the PR and everything else
# (mergeable/checks/reviews/…) to the view — a core-aware `dataclasses.replace`.
_CORE_FIELDS = {"number", "head_sha", "is_draft", "base_ref", "merge_state"}


def _replace(ctx: ReadinessView, **overrides) -> ReadinessView:
    core = {k: v for k, v in overrides.items() if k in _CORE_FIELDS}
    view = {k: v for k, v in overrides.items() if k not in _CORE_FIELDS}
    new_pr = dataclasses.replace(ctx.pr, **core) if core else ctx.pr
    return dataclasses.replace(ctx, pr=new_pr, **view)


def test_no_pr():
    status = no_pr()
    assert status.state is TaskState.NO_PR
    assert "create a draft PR" in status.next_action


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("gemini_eyes_copilot_requested", TaskState.REVIEWS_PENDING),
        # review-once is the DEFAULT (rerun=False): the earlier-head Copilot
        # review counts as done, so this PR is no longer REVIEWS_PENDING — it
        # falls through to its BLOCKED merge state (mergeStateStatus=BLOCKED).
        # The head-strict (rerun=True) re-request case is asserted separately.
        ("copilot_stale_review", TaskState.BLOCKED),
        ("copilot_changes_requested", TaskState.ADDRESSING),
        ("reviewed_mergeable_unknown", TaskState.REVIEWED),
        ("validating_checks_pending", TaskState.VALIDATING),
        ("ready_checks_green", TaskState.READY),
        ("copilot_clean_gemini_clean", TaskState.READY),
        ("copilot_done_all_resolved", TaskState.READY),
        ("blocked_checks_failing", TaskState.BLOCKED),
        ("blocked_merge_conflict", TaskState.BLOCKED),
    ],
)
def test_evaluate_states(context, fixture, expected):
    assert evaluate(context(fixture)).state is expected


# --- mergeStateStatus blocking (release#675) ----------------------------------
# `mergeable` is computed async and stale on first read (optimistic MERGEABLE);
# READY requires the authoritative `mergeStateStatus == CLEAN`. We vary only the
# merge fields on the otherwise-READY fixture to isolate the merge-state check. CLEAN is the
# ONLY ready state — every other COMPUTED state is a real block (this fleet
# requires 0 approving reviews, so a reviewed+green PR reaches CLEAN without a
# human; a non-CLEAN computed state is never a waiting-on-approval handoff).


@pytest.mark.parametrize(
    ("mergeable", "merge_state", "expected"),
    [
        # The bug: optimistic MERGEABLE while GitHub's real verdict is a conflict.
        ("MERGEABLE", "DIRTY", TaskState.BLOCKED),
        # Behind the base — cannot merge cleanly until updated.
        ("MERGEABLE", "BEHIND", TaskState.BLOCKED),
        # Merge state not yet computed — must re-poll, never hand off.
        ("MERGEABLE", "UNKNOWN", TaskState.REVIEWED),
        ("MERGEABLE", None, TaskState.REVIEWED),
        # Genuinely clean — the ONLY ready state.
        ("MERGEABLE", "CLEAN", TaskState.READY),
        # Computed but non-CLEAN: GitHub is blocking the merge (branch protection
        # / required status) — BLOCKED, not a handoff point.
        ("MERGEABLE", "BLOCKED", TaskState.BLOCKED),
        # UNSTABLE with an already-green rollup is a transient ready_for_review
        # re-queue lag (a SKIPPED/NEUTRAL check re-runs) — defer to the rollup and
        # reach READY, not a false-alarm BLOCKED (release#715). The fixture's
        # rollup is green, so this isolates the merge-state branch.
        ("MERGEABLE", "UNSTABLE", TaskState.READY),
        # DIRTY wins even if `mergeable` lags at the optimistic value.
        ("UNKNOWN", "DIRTY", TaskState.BLOCKED),
        # CLEAN is authoritative even if `mergeable` still lags at UNKNOWN.
        ("UNKNOWN", "CLEAN", TaskState.READY),
        # A stale CONFLICTING must NOT block when the fresher merge state is
        # CLEAN — merge_state is authoritative (the mirror of the core bug).
        ("CONFLICTING", "CLEAN", TaskState.READY),
        # CONFLICTING is honored only as a fallback when merge_state is uncomputed.
        ("CONFLICTING", "UNKNOWN", TaskState.BLOCKED),
        ("CONFLICTING", None, TaskState.BLOCKED),
    ],
)
def test_ready_requires_clean_merge_state(context, mergeable, merge_state, expected):

    ctx = _replace(
        context("ready_checks_green"), mergeable=mergeable, merge_state=merge_state
    )
    status = evaluate(ctx)
    assert status.state is expected, f"{mergeable}/{merge_state} -> {status.state}"


def test_dirty_merge_state_names_the_conflict_fix(context):

    ctx = _replace(context("ready_checks_green"), merge_state="DIRTY")
    assert "conflict" in evaluate(ctx).next_action


def test_behind_base_says_update_the_branch(context):

    ctx = _replace(context("ready_checks_green"), merge_state="BEHIND")
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert "behind" in status.next_action and "update" in status.next_action


def test_behind_base_takes_precedence_over_pending_ci(context):
    # A moved base re-stales CI, so a behind PR with pending checks must give the
    # actionable "update the branch" next action, not "wait for checks" — BEHIND
    # is evaluated before CI state (release#675).

    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]
    ctx = _replace(context("ready_checks_green"), merge_state="BEHIND", checks=pending)
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert "behind" in status.next_action


def test_non_clean_block_message_names_the_merge_state(context):
    # A genuine computed non-CLEAN merge state the rollup can't disprove (BLOCKED:
    # e.g. a missing required status) stays BLOCKED and names mergeStateStatus in
    # the next action. (UNSTABLE is handled separately — see the #715 tests.)

    ctx = _replace(context("ready_checks_green"), merge_state="BLOCKED")
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert "BLOCKED" in status.next_action


# --- UNSTABLE = transient ready_for_review re-queue lag (release#715) --------
# GitHub re-runs a SKIPPED/NEUTRAL check on the `ready_for_review` event (phos's
# `e2e-gpu`, conclusion=skipped), flipping mergeStateStatus to UNSTABLE for a beat
# while the statusCheckRollup still reads green — a false-alarm BLOCKED right after
# `pr ready`. The engine already inspects every check via the rollup, so when the
# rollup is green it defers to it and reaches READY.


def test_unstable_with_green_rollup_is_ready(context):
    # The exact #715 scenario: green rollup (a SKIPPED e2e-gpu among them) but
    # mergeStateStatus lags at UNSTABLE — defer to the rollup → READY.

    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "name": "e2e-gpu",
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "SKIPPED",
        },
    ]
    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE", checks=rollup)
    status = evaluate(ctx)
    assert status.state is TaskState.READY
    assert "shipit pr ready" in status.next_action  # draft fixture → flip
    assert "UNSTABLE" in status.next_action  # explains why it's not a flat CLEAN


def test_unstable_with_a_genuinely_failing_check_is_still_blocked(context):
    # UNSTABLE must NOT mask a real failure: a FAILING rollup is caught by the CI
    # checks BEFORE the merge-state branch, so it stays BLOCKED with the CI message.

    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "name": "e2e-gpu",
            "__typename": "CheckRun",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
        },
    ]
    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE", checks=rollup)
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert "failing" in status.next_action


def test_unstable_with_no_rollup_is_not_promoted(context):
    # #737 review: an empty/absent rollup (ChecksState.NONE) is NOT evidence the
    # checks passed, so an UNSTABLE-with-no-rollup must NOT be blindly promoted to
    # READY — only an EXPLICITLY GREEN rollup tolerates UNSTABLE.

    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE", checks=[])
    status = evaluate(ctx)
    assert status.state is not TaskState.READY


def test_unstable_with_a_re_running_check_is_validating(context):
    # The check is genuinely mid-re-run (IN_PROGRESS) → the rollup is PENDING, so
    # the CI checks report VALIDATING (wait for checks), never a flip. Only an
    # already-green rollup reaches READY.

    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "name": "e2e-gpu",
            "__typename": "CheckRun",
            "status": "IN_PROGRESS",
            "conclusion": None,
        },
    ]
    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE", checks=rollup)
    status = evaluate(ctx)
    assert status.state is TaskState.VALIDATING


def test_unstable_non_draft_says_done_not_flip(context):
    # Post-flip (already ready-for-review) UNSTABLE-with-green-rollup must say
    # done/await merge, never re-prescribe the flip the agent already made.

    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE")
    ctx = _replace(ctx, is_draft=False)
    status = evaluate(ctx)
    assert status.state is TaskState.READY
    assert "shipit pr ready" not in status.next_action
    assert "done" in status.next_action and "merge" in status.next_action


def test_best_effort_gemini_does_not_hold_ready(context):
    # Gemini is NOT_REQUESTED here, yet Copilot (required) is done clean with
    # green checks -> READY. A best-effort reviewer must not hold it back.
    status = evaluate(context("ready_checks_green"))
    assert status.state is TaskState.READY
    assert status.reviewers["gemini"] == "done_clean"


def test_addressing_reports_open_thread_count(context):
    status = evaluate(context("copilot_changes_requested"))
    assert status.state is TaskState.ADDRESSING
    assert status.open_threads == 1
    assert "1 open thread" in status.next_action


def test_addressing_names_the_thread_reading_tool(context):
    # Discoverability (#564): the agent must learn HOW to read the threads from
    # the next action itself, not fall back to raw `gh api`. It points at a
    # command that actually displays threads (`gh pr view --comments`); shipit
    # has no thread-listing verb (`shipit pr review` is request-only).
    status = evaluate(context("copilot_changes_requested"))
    assert "gh pr view --comments" in status.next_action
    assert "resolve" in status.next_action


# --- READY next-action: draft vs already-flipped (#564) ----------------------


def test_ready_draft_says_flip(context):
    status = evaluate(context("ready_checks_green"))  # isDraft: true
    assert status.state is TaskState.READY
    assert "shipit pr ready" in status.next_action


def test_ready_non_draft_says_done_not_flip(context):
    # Post-flip a READY PR is in the human's hands: the next action must say
    # done/await merge, never re-prescribe the flip the agent already made.
    ctx = context("ready_checks_green")
    ctx = _replace(ctx, is_draft=False)
    status = evaluate(ctx)
    assert status.state is TaskState.READY
    assert "shipit pr ready" not in status.next_action
    assert "done" in status.next_action
    assert "merge" in status.next_action


def test_blocked_reasons_are_distinct(context):
    assert "conflict" in evaluate(context("blocked_merge_conflict")).next_action
    assert "failing" in evaluate(context("blocked_checks_failing")).next_action


def test_status_to_dict_round_trips(context):
    d = evaluate(context("ready_checks_green")).to_dict()
    assert d["state"] == "ready"
    assert d["checks"] == "green"
    assert d["mergeable"] == "MERGEABLE"
    assert set(d) == {
        "pr",
        "state",
        "next_action",
        "reviewers",
        "open_threads",
        "checks",
        "mergeable",
        "cycles",
        "breaker",
        "reviewer_funnel",
        "degraded",
        "to_request",
    }


# --- REVIEWS_PENDING next-action wording (request vs re-request vs wait) ----


def test_reviews_pending_never_requested_says_request(context):
    # No review ever landed and Copilot is not requested → the action is to
    # REQUEST (not wait), and it must NOT mention re-request/stale.
    status = evaluate(context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert "request for the current head" in status.next_action
    assert "copilot" in status.next_action  # the reviewer is named in the clause
    assert "RE-REQUEST" not in status.next_action
    assert "stale" not in status.next_action


def test_reviews_pending_stale_after_push_says_rerequest(context):
    # ONLY a rerun=True (head-strict) reviewer can be stale-after-push: Copilot
    # reviewed an EARLIER commit and a push moved the head. With rerun opted in,
    # the action distinguishes this from a fresh request: RE-REQUEST for the
    # current head, and names the staleness. (Under the review-once default the
    # earlier-head review would simply count as done — see the reviewers tests.)
    ctx = context("copilot_stale_needs_rerequest")
    ctx.roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "RE-REQUEST for the current head" in status.next_action
    assert "stale after a push" in status.next_action
    assert "copilot" in status.next_action


def test_review_once_earlier_head_is_done_never_rerequested(context):
    # The DEFAULT (review-once): the SAME stale fixture, with no rerun opt-in, is
    # NOT pending — the earlier-head review counts as done, so the reviewer never
    # appears in RE-REQUEST advice. (mergeStateStatus=BLOCKED in the fixture is
    # then the only thing holding it, proving review holds cleared.)
    status = evaluate(context("copilot_stale_needs_rerequest"))
    assert status.state is not TaskState.REVIEWS_PENDING
    assert "RE-REQUEST" not in status.next_action
    assert status.reviewers["copilot"].startswith("done")


def test_reviews_pending_already_requested_says_wait(context):
    # Copilot is REQUESTED on the current head (no review yet) → just wait; the
    # action must not tell the caller to (re-)request what is already pending.
    status = evaluate(context("gemini_eyes_copilot_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert (
        "wait (already requested / in flight on the current head)" in status.next_action
    )
    assert "RE-REQUEST" not in status.next_action


# --- RVW01-WS01 regression pin: the engine is the SOLE requester (ADR-0031) --
#
# The repo cutover deletes the Actions caller workflow, so Copilot's
# `review_requested` edge exists ONLY if the engine's request path places it.
# Pin the two invariants the whole epic leans on (tests only, no engine change):
# a required, never-requested Copilot lands in the engine's `to_request` set
# (the structured field `pr next` routes on — not the prose), and under this
# repo's `rerun = true` (head-strict) policy a Copilot review left on a stale
# head is advised RE-REQUEST, re-surfacing in `to_request` for the new head.


def test_engine_requests_never_requested_required_copilot(context):
    # No caller workflow anymore: the ONLY source of Copilot's initial request
    # is the engine's to-request set. A required, never-requested Copilot must
    # appear there — structurally, not merely in the next-action prose.
    status = evaluate(context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]


def test_engine_rerequests_stale_head_copilot_under_rerun(context):
    # This repo's roster policy (`copilot = { rerun = true }`): a push re-stales
    # Copilot's earlier-head review, so the engine must both ADVISE the
    # re-request in the prose and ROUTE to it via `to_request` — this is what
    # generates the per-push review rounds the round counter and the
    # all-nitpick breaker are exercised by.
    ctx = context("copilot_stale_needs_rerequest")
    ctx.roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST for the current head" in status.next_action


# --- failing checks outrank review requests (#352) ---------------------------
#
# Every reviewer is token-billed and a CI fix always pushes a new head, so a
# red-checks PR must never advise (or structurally route to) a review request:
# it ranks BLOCKED/fix-CI with `to_request` suppressed. PENDING checks do NOT
# defer — reviewing in parallel with a running CI run is deliberate.


def _failing_rollup() -> list[dict]:
    return [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]


def _pending_rollup() -> list[dict]:
    return [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]


def test_failing_checks_outrank_review_requests(context):
    # Never-requested required reviewer + red checks → fix CI is the next action;
    # `to_request` is suppressed so `pr next` cannot burn a review on this head.
    ctx = _replace(context("copilot_never_requested"), checks=_failing_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "fix CI first" in status.next_action
    # The deferral is named — held intentionally, not forgotten.
    assert "review requests deferred" in status.next_action
    assert "copilot" in status.next_action


def test_same_snapshot_with_green_checks_requests_reviewers(context):
    # The green-checks twin of the test above: the reviewer surfaces in
    # `to_request` again, proving the suppression keys purely off the rollup.
    green = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    status = evaluate(_replace(context("copilot_never_requested"), checks=green))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]


def test_pending_checks_still_allow_requesting(context):
    # Regression-pin the parallelism: checks still RUNNING (not failed) must not
    # defer review requests — a green outcome wastes nothing.
    ctx = _replace(context("copilot_never_requested"), checks=_pending_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]


def test_failing_checks_with_only_in_flight_reviewers_says_fix_ci(context):
    # Copilot already requested (nothing to defer) + red checks: still ranks
    # fix-CI — there is no request to hold, but "wait for the review" is the
    # wrong next action when the head is known-doomed. The in-flight reviewer is
    # still named so the state stays legible.
    ctx = _replace(context("gemini_eyes_copilot_requested"), checks=_failing_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "fix CI first" in status.next_action
    assert "review requests deferred" not in status.next_action
    assert "copilot" in status.next_action


# --- parallel-required: BOTH reviewers hold (release#622) -------------------
#
# The dual set is no longer the shipped default (coderabbit is a phos-org
# pilot, opted in per-repo), so these tests pass the pair explicitly — the
# both-hold BEHAVIOR they prove is unchanged for any repo that requires both.


def _both_required():
    return [by_name("copilot"), by_name("coderabbit")]


def _green_checks() -> list[dict]:
    return [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def _ctx_with_reviews(*authors_on_head: str) -> ReadinessView:
    """A draft PR, green + mergeable, with an APPROVED review on the head per
    named author — everything but the review set held constant. `merge_state`
    is CLEAN so the merge-state check (release#675) doesn't hold back a context
    built to isolate REVIEWER logic."""
    return readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        reviews=[
            Review(i, a, "APPROVED", HEAD, "") for i, a in enumerate(authors_on_head, 1)
        ],
        checks=_green_checks(),
    )


def test_both_required_reviewers_reviewed_reaches_ready():
    # Copilot AND CodeRabbit both reviewed the current head → READY.
    status = evaluate(
        _ctx_with_reviews("Copilot", "coderabbitai[bot]"), required=_both_required()
    )
    assert status.state is TaskState.READY
    assert status.reviewers["copilot"].startswith("done")
    assert status.reviewers["coderabbit"].startswith("done")


def test_missing_coderabbit_review_is_not_ready_and_names_it_outstanding():
    # Copilot reviewed but CodeRabbit has not → still REVIEWS_PENDING, and the
    # engine names CodeRabbit as the outstanding required reviewer (the mocked
    # single-reviewer-outage case).
    status = evaluate(_ctx_with_reviews("Copilot"), required=_both_required())
    assert status.state is TaskState.REVIEWS_PENDING
    assert "coderabbit" in status.next_action
    assert (
        "copilot" not in status.next_action.split("—")[1]
    )  # copilot is done, not pending


def test_missing_copilot_review_is_not_ready_and_names_it_outstanding():
    status = evaluate(_ctx_with_reviews("coderabbitai[bot]"), required=_both_required())
    assert status.state is TaskState.REVIEWS_PENDING
    assert "copilot" in status.next_action


# --- the required SET is data-driven, not hard-coded to the two -------------


def test_required_set_is_data_driven_single_reviewer():
    # Drive the engine with a DIFFERENT required set — just CodeRabbit. With
    # only CodeRabbit's review present (no Copilot), it now reaches READY: the
    # required set follows the config, not a hard-coded pair.
    only_coderabbit = [by_name("coderabbit")]
    status = evaluate(_ctx_with_reviews("coderabbitai[bot]"), required=only_coderabbit)
    assert status.state is TaskState.READY


def test_required_set_is_data_driven_three_reviewers():
    # A three-reviewer required set proves the engine reads the SET generically
    # — no two-reviewer assumption. The third is a tiny FAKE requestable adapter
    # (not Gemini, which is non-requestable and may never be required): with no
    # review from it, the PR stays REVIEWS_PENDING and names it outstanding.
    from shipit.prstate.model import ReviewLifecycle
    from shipit.prstate.reviewers import ReviewerAdapter

    class _Falcon(ReviewerAdapter):
        name = "falcon"
        requestable = True

        def matches(self, login: str) -> bool:
            return "falcon" in login.lower()

        def detect(self, ctx) -> ReviewLifecycle:
            on_head = any(self.matches(r.author) for r in ctx.reviews_on_head())
            return (
                ReviewLifecycle.DONE_CLEAN if on_head else ReviewLifecycle.NOT_REQUESTED
            )

    three = [by_name("copilot"), by_name("coderabbit"), _Falcon()]
    status = evaluate(_ctx_with_reviews("Copilot", "coderabbitai[bot]"), required=three)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "falcon" in status.next_action


def test_a_push_re_stales_both_required_reviewers_when_rerun():
    # Both reviewed an EARLIER head; a push moved the head. With BOTH opted into
    # rerun (head-strict), both are now stale → the engine asks to RE-REQUEST
    # both for the current head. (Under the review-once default both would count
    # as done — see test_review_once_both_earlier_head_reaches_ready.)
    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        mergeable="MERGEABLE",
        reviews=[
            Review(1, "Copilot", "APPROVED", OLD, ""),
            Review(2, "coderabbitai[bot]", "APPROVED", OLD, ""),
        ],
        checks=_green_checks(),
        roster=Roster(
            (
                RosterEntry(name="copilot", required=True, rerun=True),
                RosterEntry(name="coderabbit", required=True, rerun=True),
            )
        ),
    )
    # The Roster already makes both reviewers required — derive the required set
    # from it (the production shape) rather than re-passing an explicit override.
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "RE-REQUEST" in status.next_action
    assert "copilot" in status.next_action
    assert "coderabbit" in status.next_action


def test_review_once_both_earlier_head_reaches_ready():
    # The DEFAULT (review-once): the SAME both-reviewed-an-earlier-head context,
    # with no rerun opt-in, reaches READY — neither earlier-head review is stale,
    # so a push does NOT re-open the review holds (the whole point of the policy).
    ctx = readiness_view(
        number=1,
        head_sha=NEW,
        is_draft=True,
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        reviews=[
            Review(1, "Copilot", "APPROVED", OLD, ""),
            Review(2, "coderabbitai[bot]", "APPROVED", OLD, ""),
        ],
        checks=_green_checks(),
    )
    status = evaluate(ctx, required=_both_required())
    assert status.state is TaskState.READY


# --- classify_checks ------------------------------------------------------


def test_classify_empty_is_none():
    assert classify_checks([]) is ChecksState.NONE


def test_classify_all_success_is_green():
    rollup = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "StatusContext", "state": "SUCCESS"},
    ]
    assert classify_checks(rollup) is ChecksState.GREEN


def test_classify_pending_beats_green():
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS", "conclusion": None},
    ]
    assert classify_checks(rollup) is ChecksState.PENDING


def test_classify_failing_beats_everything():
    rollup = [
        {"status": "IN_PROGRESS", "conclusion": None},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert classify_checks(rollup) is ChecksState.FAILING


def test_classify_status_context_error_is_failing():
    rollup = [{"__typename": "StatusContext", "state": "ERROR"}]
    assert classify_checks(rollup) is ChecksState.FAILING


def test_classify_expected_status_is_pending():
    # EXPECTED = a status that's expected but hasn't reported yet -> not green.
    rollup = [{"__typename": "StatusContext", "state": "EXPECTED"}]
    assert classify_checks(rollup) is ChecksState.PENDING


def test_classify_neutral_and_skipped_are_green():
    rollup = [
        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert classify_checks(rollup) is ChecksState.GREEN
