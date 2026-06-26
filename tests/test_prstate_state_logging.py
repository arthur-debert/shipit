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


def test_no_pr_does_not_log_a_decision(caplog):
    """`no_pr` is a pre-engine shortcut, not a state-machine resolution: it emits
    no decision record. (This also keeps a no-PR `pr status --json` run clean of
    log lines on the CI stdout sink, where command output and logs share stdout.)"""
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        status = no_pr()
    assert status.state.value == "no_pr"
    assert not [r for r in caplog.records if "decision" in r.getMessage()]


def test_evaluate_return_value_is_unchanged_by_logging(context):
    """The log is the only side effect — the snapshot is identical to the pure
    engine's result."""
    from shipit.prstate.state import _evaluate

    ctx = context("ready_checks_green")
    assert evaluate(ctx).to_dict() == _evaluate(ctx).to_dict()
