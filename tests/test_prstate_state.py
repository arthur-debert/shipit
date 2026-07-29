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

NEW = Sha("beef" * 10)
OLD = Sha("dead" * 10)
HEAD = Sha("abcd" * 10)

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
        ("copilot_stale_review", TaskState.BLOCKED),
        ("copilot_changes_requested", TaskState.ADDRESSING),
        ("reviewed_mergeable_unknown", TaskState.REVIEWED),
        ("validating_checks_pending", TaskState.VALIDATING),
        ("ready_checks_green", TaskState.READY),
        ("copilot_clean_gemini_clean", TaskState.READY),
        ("copilot_done_all_resolved", TaskState.READY),
        ("blocked_checks_failing", TaskState.BLOCKED),
        ("blocked_checks_cancelled", TaskState.BLOCKED),
        ("blocked_merge_conflict", TaskState.BLOCKED),
    ],
)
def test_evaluate_states(context, fixture, expected):
    assert evaluate(context(fixture)).state is expected


@pytest.mark.parametrize(
    ("mergeable", "merge_state", "expected"),
    [
        ("MERGEABLE", "DIRTY", TaskState.BLOCKED),
        ("MERGEABLE", "BEHIND", TaskState.BLOCKED),
        ("MERGEABLE", "UNKNOWN", TaskState.REVIEWED),
        ("MERGEABLE", None, TaskState.REVIEWED),
        ("MERGEABLE", "CLEAN", TaskState.READY),
        ("MERGEABLE", "BLOCKED", TaskState.BLOCKED),
        ("MERGEABLE", "UNSTABLE", TaskState.READY),
        ("UNKNOWN", "DIRTY", TaskState.BLOCKED),
        ("UNKNOWN", "CLEAN", TaskState.READY),
        ("CONFLICTING", "CLEAN", TaskState.READY),
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

    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]
    ctx = _replace(context("ready_checks_green"), merge_state="BEHIND", checks=pending)
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert "behind" in status.next_action


def test_non_clean_block_message_names_the_merge_state(context):

    ctx = _replace(context("ready_checks_green"), merge_state="BLOCKED")
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert "BLOCKED" in status.next_action


def test_unstable_with_green_rollup_is_ready(context):

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
    assert "shipit pr ready" in status.next_action
    assert "UNSTABLE" in status.next_action


def test_unstable_with_a_genuinely_failing_check_is_still_blocked(context):

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

    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE", checks=[])
    status = evaluate(ctx)
    assert status.state is not TaskState.READY


def test_unstable_with_a_re_running_check_is_validating(context):

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

    ctx = _replace(context("ready_checks_green"), merge_state="UNSTABLE")
    ctx = _replace(ctx, is_draft=False)
    status = evaluate(ctx)
    assert status.state is TaskState.READY
    assert "shipit pr ready" not in status.next_action
    assert "done" in status.next_action and "merge" in status.next_action


def test_best_effort_gemini_does_not_hold_ready(context):
    status = evaluate(context("ready_checks_green"))
    assert status.state is TaskState.READY
    assert status.reviewers["gemini"] == "done_clean"


def test_addressing_reports_open_thread_count(context):
    status = evaluate(context("copilot_changes_requested"))
    assert status.state is TaskState.ADDRESSING
    assert status.open_threads == 1
    assert "1 open thread" in status.next_action


def test_addressing_names_the_thread_reading_tool(context):
    status = evaluate(context("copilot_changes_requested"))
    assert "gh pr view --comments" in status.next_action
    assert "resolve" in status.next_action


def test_ready_draft_says_flip(context):
    status = evaluate(context("ready_checks_green"))
    assert status.state is TaskState.READY
    assert "shipit pr ready" in status.next_action


def test_ready_non_draft_says_done_not_flip(context):
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


def test_reviews_pending_never_requested_says_request(context):
    status = evaluate(context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert "request for the current head" in status.next_action
    assert "copilot" in status.next_action
    assert "RE-REQUEST" not in status.next_action
    assert "stale" not in status.next_action


def test_reviews_pending_stale_after_push_says_rerequest(context):
    ctx = context("copilot_stale_needs_rerequest")
    ctx.roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "RE-REQUEST for the current head" in status.next_action
    assert "stale after a push" in status.next_action
    assert "copilot" in status.next_action


def test_review_once_earlier_head_is_done_never_rerequested(context):
    status = evaluate(context("copilot_stale_needs_rerequest"))
    assert status.state is not TaskState.REVIEWS_PENDING
    assert "RE-REQUEST" not in status.next_action
    assert status.reviewers["copilot"].startswith("done")


def test_reviews_pending_already_requested_says_wait(context):
    status = evaluate(context("gemini_eyes_copilot_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert (
        "wait (already requested / in flight on the current head)" in status.next_action
    )
    assert "RE-REQUEST" not in status.next_action


def test_engine_requests_never_requested_required_copilot(context):
    status = evaluate(context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]


def test_engine_rerequests_stale_head_copilot_under_rerun(context):
    ctx = context("copilot_stale_needs_rerequest")
    ctx.roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST for the current head" in status.next_action


def _failing_rollup() -> list[dict]:
    return [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]


def _pending_rollup() -> list[dict]:
    return [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]


def test_failing_checks_outrank_review_requests(context):
    ctx = _replace(context("copilot_never_requested"), checks=_failing_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "fix CI first" in status.next_action
    assert "review requests deferred" in status.next_action
    assert "copilot" in status.next_action


def test_same_snapshot_with_green_checks_requests_reviewers(context):
    green = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    status = evaluate(_replace(context("copilot_never_requested"), checks=green))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]


def test_pending_checks_still_allow_requesting(context):
    ctx = _replace(context("copilot_never_requested"), checks=_pending_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]


def test_failing_checks_with_only_in_flight_reviewers_says_fix_ci(context):
    ctx = _replace(context("gemini_eyes_copilot_requested"), checks=_failing_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "fix CI first" in status.next_action
    assert "review requests deferred" not in status.next_action


def _cancelled_rollup() -> list[dict]:
    return [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"}
    ]


def test_cancelled_run_is_blocked_with_rerun_advice(context):
    status = evaluate(context("blocked_checks_cancelled"))
    assert status.state is TaskState.BLOCKED
    assert status.checks is ChecksState.CANCELLED
    assert "gh run rerun" in status.next_action
    assert "fix and push" not in status.next_action
    assert "failing" not in status.next_action


def test_cancelled_run_never_reads_as_pending(context):
    status = evaluate(context("blocked_checks_cancelled"))
    assert status.state is not TaskState.VALIDATING
    assert status.checks is not ChecksState.PENDING
    assert status.to_dict()["checks"] == "cancelled"


def test_cancelled_outranks_review_requests(context):
    ctx = _replace(context("copilot_never_requested"), checks=_cancelled_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "gh run rerun" in status.next_action
    assert "review requests deferred" in status.next_action
    assert "copilot" in status.next_action


def test_cancelled_with_only_in_flight_reviewers_still_says_rerun(context):
    ctx = _replace(context("gemini_eyes_copilot_requested"), checks=_cancelled_rollup())
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "gh run rerun" in status.next_action
    assert "review requests deferred" not in status.next_action
    assert "already requested / in flight" in status.next_action
    assert "copilot" in status.next_action


def _both_required():
    return [by_name("copilot"), by_name("coderabbit")]


def _green_checks() -> list[dict]:
    return [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def _ctx_with_reviews(*authors_on_head: str) -> ReadinessView:
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
    status = evaluate(
        _ctx_with_reviews("Copilot", "coderabbitai[bot]"), required=_both_required()
    )
    assert status.state is TaskState.READY
    assert status.reviewers["copilot"].startswith("done")
    assert status.reviewers["coderabbit"].startswith("done")


def test_missing_coderabbit_review_is_not_ready_and_names_it_outstanding():
    status = evaluate(_ctx_with_reviews("Copilot"), required=_both_required())
    assert status.state is TaskState.REVIEWS_PENDING
    assert "coderabbit" in status.next_action
    assert "copilot" not in status.next_action.split("—")[1]


def test_missing_copilot_review_is_not_ready_and_names_it_outstanding():
    status = evaluate(_ctx_with_reviews("coderabbitai[bot]"), required=_both_required())
    assert status.state is TaskState.REVIEWS_PENDING
    assert "copilot" in status.next_action


def test_required_set_is_data_driven_single_reviewer():
    only_coderabbit = [by_name("coderabbit")]
    status = evaluate(_ctx_with_reviews("coderabbitai[bot]"), required=only_coderabbit)
    assert status.state is TaskState.READY


def test_required_set_is_data_driven_three_reviewers():
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
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert "RE-REQUEST" in status.next_action
    assert "copilot" in status.next_action
    assert "coderabbit" in status.next_action


def test_review_once_both_earlier_head_reaches_ready():
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
        roster=Roster(
            (
                RosterEntry(name="copilot", required=True, rerun=False),
                RosterEntry(name="coderabbit", required=True, rerun=False),
            )
        ),
    )
    status = evaluate(ctx)
    assert status.state is TaskState.READY


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
    rollup = [{"__typename": "StatusContext", "state": "EXPECTED"}]
    assert classify_checks(rollup) is ChecksState.PENDING


def test_classify_neutral_and_skipped_are_green():
    rollup = [
        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert classify_checks(rollup) is ChecksState.GREEN


def test_classify_cancelled_is_its_own_verdict_not_failing():
    rollup = [{"status": "COMPLETED", "conclusion": "CANCELLED"}]
    assert classify_checks(rollup) is ChecksState.CANCELLED


def test_classify_cancelled_beside_green_is_cancelled():
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
    ]
    assert classify_checks(rollup) is ChecksState.CANCELLED


def test_classify_failure_beats_cancelled():
    rollup = [
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert classify_checks(rollup) is ChecksState.FAILING


def test_classify_pending_beats_cancelled():
    rollup = [
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
        {"status": "IN_PROGRESS", "conclusion": None},
    ]
    assert classify_checks(rollup) is ChecksState.PENDING
