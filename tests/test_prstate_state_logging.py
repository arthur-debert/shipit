from __future__ import annotations

import logging

from conftest import load_context

from shipit.prstate.model import ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import evaluate, no_pr


def _decision_records(caplog):
    return [r for r in caplog.records if hasattr(r, "state") and hasattr(r, "funnel")]


def test_evaluate_logs_the_decision(context, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        status = evaluate(context("ready_checks_green"))
    records = [r.getMessage() for r in caplog.records]
    text = "\n".join(records)
    assert "decision pr#" in text
    assert f"state={status.state.value}" in text
    assert status.next_action in text


def test_decision_is_an_info_milestone_with_required_fields(context, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(context("ready_checks_green"))
    decisions = _decision_records(caplog)
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec.levelno == logging.INFO
    assert rec.pr == status.pr
    assert rec.state == status.state.value
    assert rec.checks == status.checks.value
    assert rec.open_threads == status.open_threads
    assert rec.cycles == status.cycles
    for name, rf in status.reviewer_funnel.items():
        assert f"{name}={rf.state.value}" in rec.funnel


def test_degraded_reviewers_surface_at_warning_with_fields(caplog):
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [
        ReviewFunnelCheck(
            reviewer="codex-local",
            status="COMPLETED",
            conclusion="FAILURE",
            started_at="2026-01-01T00:25:00Z",
        )
    ]
    required = [by_name("copilot"), by_name("codex")]
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(ctx, required=required)
    assert status.degraded
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and hasattr(r, "degraded")
    ]
    assert len(warnings) == 1
    rec = warnings[0]
    assert rec.pr == status.pr
    for name, why in status.degraded.items():
        assert f"{name}={why}" in rec.degraded


def test_clean_snapshot_logs_no_degraded_warning(context, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(context("ready_checks_green"))
    assert not status.degraded
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_pr_does_not_log_a_decision(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        status = no_pr()
    assert status.state.value == "no_pr"
    assert not _decision_records(caplog)


def test_evaluate_return_value_is_unchanged_by_logging(context):
    from shipit.prstate.state import _evaluate

    ctx = context("ready_checks_green")
    assert evaluate(ctx).to_dict() == _evaluate(ctx).to_dict()


def test_degraded_reviewer_is_tagged_as_a_dev_cycle_event(caplog):
    from shipit import events

    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [
        ReviewFunnelCheck(
            reviewer="codex-local",
            status="COMPLETED",
            conclusion="FAILURE",
            started_at="2026-01-01T00:25:00Z",
        )
    ]
    required = [by_name("copilot"), by_name("codex")]
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(ctx, required=required)
    tagged = [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == "review.degraded"
    ]
    assert {(r.reviewer, r.reason) for r in tagged} == set(status.degraded.items())
    assert all(r.levelno == logging.INFO and r.pr == status.pr for r in tagged)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx, required=required)
    assert not [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == "review.degraded"
    ]
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_clean_snapshot_emits_no_observational_events(context, caplog):
    from shipit import events

    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(context("copilot_never_requested"))
    assert not [r for r in caplog.records if getattr(r, events.EXTRA_KEY, None)]
