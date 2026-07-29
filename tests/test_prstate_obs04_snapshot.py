from __future__ import annotations

from datetime import UTC, datetime

from conftest import DEFAULT_NOW, load_context

from shipit.prstate.model import FunnelState, ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import ChecksState, ReviewLifecycle, evaluate


def test_snapshot_carries_injected_now(context):
    ctx = context("local_funnel_failed_ci_green")
    assert ctx.now == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    assert ctx.now.tzinfo is not None


def test_default_now_when_fixture_omits_it(context):
    assert context("ready_checks_green").now == DEFAULT_NOW


def test_fixed_now_plus_recorded_snapshot_is_deterministic(context):
    first = evaluate(context("local_funnel_failed_ci_green")).to_dict()
    second = evaluate(context("local_funnel_failed_ci_green")).to_dict()
    assert first == second


def test_now_ages_an_inflight_reviewer_past_its_window(context):
    within = evaluate(context("local_funnel_failed_ci_green"))
    past = evaluate(
        load_context(
            "local_funnel_failed_ci_green",
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    assert within.reviewer_funnel["agy"].state is FunnelState.IN_FLIGHT
    assert past.reviewer_funnel["agy"].state is FunnelState.TIMED_OUT


def test_funnel_runs_lifted_out_of_ci_checks(context):
    ctx = context("local_funnel_failed_ci_green")
    check_names = {c.get("name") or c.get("context") for c in ctx.checks}
    assert check_names == {"ci / check", "license/cla"}
    assert all(not (c.get("name") or "").startswith("review:") for c in ctx.checks)
    reviewers = {f.reviewer for f in ctx.review_funnel}
    assert reviewers == {"codex-local", "agy-local"}


def test_failed_funnel_run_does_not_fail_the_ci_checks(context):
    status = evaluate(context("local_funnel_failed_ci_green"))
    assert status.checks is ChecksState.GREEN


def test_funnel_check_parsed_fields(context):
    ctx = context("local_funnel_failed_ci_green")
    by_reviewer = {f.reviewer: f for f in ctx.review_funnel}
    codex = by_reviewer["codex-local"]
    assert codex == ReviewFunnelCheck(
        reviewer="codex-local",
        status="COMPLETED",
        conclusion="FAILURE",
        started_at="2026-01-01T00:00:00Z",
    )
    agy = by_reviewer["agy-local"]
    assert agy.status == "IN_PROGRESS"
    assert agy.conclusion is None
    assert agy.started_at == "2026-01-01T00:25:00Z"


def test_local_adapter_claims_its_funnel_run(context):
    ctx = context("local_funnel_failed_ci_green")
    codex_fc = by_name("codex").funnel_check(ctx)
    assert codex_fc is not None and codex_fc.conclusion == "FAILURE"
    agy_fc = by_name("agy").funnel_check(ctx)
    assert agy_fc is not None and agy_fc.status == "IN_PROGRESS"


def test_app_adapter_has_no_funnel_run(context):
    ctx = context("local_funnel_failed_ci_green")
    assert by_name("copilot").funnel_check(ctx) is None


def test_funnel_check_selects_latest_started_not_list_order(context):
    ctx = context("local_funnel_failed_ci_green")
    ctx.review_funnel = [
        ReviewFunnelCheck(
            reviewer="codex-local",
            status="IN_PROGRESS",
            conclusion=None,
            started_at="2026-01-01T00:20:00Z",
        ),
        ReviewFunnelCheck(
            reviewer="codex-local",
            status="COMPLETED",
            conclusion="FAILURE",
            started_at="2026-01-01T00:00:00Z",
        ),
    ]
    picked = by_name("codex").funnel_check(ctx)
    assert picked is not None
    assert picked.status == "IN_PROGRESS"
    assert picked.started_at == "2026-01-01T00:20:00Z"


def test_task_status_carries_structured_funnel(context):
    status = evaluate(context("local_funnel_failed_ci_green"))
    codex = status.reviewer_funnel["codex"]
    assert codex.lifecycle is ReviewLifecycle.NOT_REQUESTED
    assert codex.check_status == "COMPLETED"
    assert codex.check_conclusion == "FAILURE"
    assert codex.check_started_at == "2026-01-01T00:00:00Z"

    copilot = status.reviewer_funnel["copilot"]
    assert copilot.lifecycle is ReviewLifecycle.DONE_CLEAN
    assert copilot.check_status is None
    assert copilot.check_conclusion is None


def test_reviewer_funnel_serializes_to_dict(context):
    d = evaluate(context("local_funnel_failed_ci_green")).to_dict()
    funnel = d["reviewer_funnel"]
    assert funnel["codex"] == {
        "lifecycle": "not_requested",
        "state": "failed",
        "check_status": "COMPLETED",
        "check_conclusion": "FAILURE",
        "check_started_at": "2026-01-01T00:00:00Z",
    }
    assert funnel["agy"]["check_status"] == "IN_PROGRESS"
    assert set(funnel) == set(d["reviewers"])
