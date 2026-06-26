"""The lifecycle engine records its decision — what `pr next` / `pr status` decide.

OBS01-WS03: `evaluate` is the state machine's resolution point; it logs the
resolved lifecycle state + next action at DEBUG so a parked-PR decision is
reconstructable after the run, without changing the returned snapshot or any
user-facing output.
"""

from __future__ import annotations

import logging

from shipit.prstate.state import evaluate, no_pr


def test_evaluate_logs_the_decision(context, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        status = evaluate(context("ready_checks_green"))
    records = [r.getMessage() for r in caplog.records]
    text = "\n".join(records)
    # The resolved state + the exact next_action the caller will report.
    assert "decision pr#" in text
    assert f"state={status.state.value}" in text
    assert status.next_action in text


def test_no_pr_logs_the_decision(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        no_pr()
    assert any("no PR for branch" in r.getMessage() for r in caplog.records)


def test_evaluate_return_value_is_unchanged_by_logging(context):
    """The log is the only side effect — the snapshot is identical to the pure
    engine's result."""
    from shipit.prstate.state import _evaluate

    ctx = context("ready_checks_green")
    assert evaluate(ctx).to_dict() == _evaluate(ctx).to_dict()
