"""The lifecycle engine records its decision — what `pr next` / `pr status` decide.

OBS01-WS03 introduced the decision record; LOG02-WS03 (the glassbox spray) makes
it a lifecycle MILESTONE: `evaluate` logs the resolved state + next action at
INFO with the decision's inputs as flat event fields (jq-sliceable by `pr`), and
surfaces degraded-settled reviewers at WARNING — without changing the returned
snapshot or any user-facing output.

Convention-level tests (the glassbox testing decision): key lifecycle events
EXIST and CARRY the required fields — no per-message string assertions beyond
the original OBS01 pin.
"""

from __future__ import annotations

import logging

from conftest import load_context
from shipit.prstate.model import ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import evaluate, no_pr


def _decision_records(caplog):
    """The decision records — identified by their FIELDS, not message text."""
    return [r for r in caplog.records if hasattr(r, "state") and hasattr(r, "funnel")]


def test_evaluate_logs_the_decision(context, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        status = evaluate(context("ready_checks_green"))
    records = [r.getMessage() for r in caplog.records]
    text = "\n".join(records)
    # The resolved state + the exact next_action the caller will report.
    assert "decision pr#" in text
    assert f"state={status.state.value}" in text
    assert status.next_action in text


def test_decision_is_an_info_milestone_with_required_fields(context, caplog):
    """LOG02: the state decision is a milestone — INFO, with the pr key and the
    decision inputs as flat event fields (not just prose)."""
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
    # The per-reviewer funnel view rides the record as one flat field.
    for name, rf in status.reviewer_funnel.items():
        assert f"{name}={rf.state.value}" in rec.funnel


def test_degraded_reviewers_surface_at_warning_with_fields(caplog):
    """A required reviewer settled non-success (failed breadcrumb) is degraded —
    non-blocking, but recorded LOUD: a WARNING carrying `pr` + `degraded`."""
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
    assert status.degraded  # the scenario really is degraded
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
    """No degraded reviewer → no WARNING: the loud record only fires when there
    is something to be loud about."""
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(context("ready_checks_green"))
    assert not status.degraded
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_pr_does_not_log_a_decision(caplog):
    """`no_pr` is a pre-engine shortcut, not a state-machine resolution: it emits
    no decision record. (This also keeps a no-PR `pr status --json` run clean of
    log lines on the CI stdout sink, where command output and logs share stdout.)"""
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        status = no_pr()
    assert status.state.value == "no_pr"
    assert not _decision_records(caplog)


def test_evaluate_return_value_is_unchanged_by_logging(context):
    """The log is the only side effect — the snapshot is identical to the pure
    engine's result."""
    from shipit.prstate.state import _evaluate

    ctx = context("ready_checks_green")
    assert evaluate(ctx).to_dict() == _evaluate(ctx).to_dict()


def test_degraded_reviewer_is_tagged_as_a_dev_cycle_event(caplog):
    """The settled non-success IS the `review.degraded` event (LOG04-WS02 /
    ADR-0032): one tagged INFO record per degraded reviewer, first sight per
    process — the aggregate WARNING above stays loud on EVERY evaluation."""
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

    # A re-evaluation re-reads the same settled outcome: the WARNING repeats
    # (loud on every status), the milestone does not.
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
    """A clean, never-reviewed-head-free snapshot tags nothing: no round, no
    breaker, no degradation — events fire on witnessed milestones only."""
    from shipit import events

    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(context("copilot_never_requested"))
    assert not [r for r in caplog.records if getattr(r, events.EXTRA_KEY, None)]
