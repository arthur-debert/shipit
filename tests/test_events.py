from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from shipit import events, logcontext, logsetup
from shipit.identity import repo_from_slug

REPO = repo_from_slug("acme/widget")


@pytest.fixture(autouse=True)
def _reset_package_logger():
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    saved = list(logger.handlers)
    saved_level, saved_prop = logger.level, logger.propagate
    for handler in saved:
        logger.removeHandler(handler)
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_prop


def _records(base_dir: Path) -> list[dict]:
    path = logsetup.log_file_path(REPO, base_dir=base_dir)
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_registered_name_lands_event_plus_bound_domain_keys(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(pr=368, repo="acme/widget", epic="RVW01", ws=1)

    events.emit(
        logging.getLogger("shipit.prstate"),
        "review.requested",
        "review request from %s attached on pr#%s (verified)",
        "copilot",
        368,
        extra={"reviewer": "copilot"},
    )

    (record,) = _records(tmp_path)
    assert record["event"] == "review.requested"
    assert record["level"] == "info"
    assert record["logger"] == "shipit.prstate"
    assert record["msg"] == "review request from copilot attached on pr#368 (verified)"
    assert record["reviewer"] == "copilot"
    assert record["pr"] == 368
    assert record["epic"] == "RVW01"
    assert record["ws"] == 1
    for absent in ("session", "tree", "run", "agent", "role"):
        assert absent not in record


def test_unknown_name_raises_and_logs_nothing(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)

    with pytest.raises(ValueError, match="unknown dev-cycle event"):
        events.emit(
            logging.getLogger("shipit.prstate"), "review.reqested", "typo'd milestone"
        )
    with pytest.raises(ValueError, match="unknown dev-cycle event"):
        events.emit(
            logging.getLogger("shipit.prstate"), "agent.diary", "freeform narration"
        )

    assert _records(tmp_path) == []


def test_the_starting_vocabulary_is_registered():
    assert events.EVENT_NAMES == {
        "session.started",
        "session.intent",
        "tree.created",
        "agent.spawned",
        "agent.phase",
        "agent.done",
        "launcher.overridden",
        "commit.created",
        "install.started",
        "install.completed",
        "install.failed",
        "ghsetup.started",
        "ghsetup.completed",
        "ghsetup.failed",
        "review.requested",
        "review.received",
        "review.degraded",
        "review.deduped",
        "review.calibrated",
        "finding.dispositioned",
        "review.pass.launched",
        "review.pass.settled",
        "round.detected",
        "breaker.fired",
        "wait.started",
        "wait.state_changed",
        "wait.fired",
        "wait.actionable",
        "wait.timed_out",
        "finding.severity_overridden",
        "release.unsigned",
        "pr.ready",
        "pr.unready",
        "sweep.started",
        "sweep.repo.done",
        "sweep.completed",
        "planning.grill.started",
        "planning.adr.written",
        "planning.spec.written",
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }


def test_every_name_is_dot_namespaced_lowercase():
    for name in events.EVENT_NAMES:
        assert "." in name
        assert name == name.lower()
        assert " " not in name


def test_the_event_field_is_reserved_for_the_registered_name(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)

    events.emit(
        logging.getLogger("shipit.prstate"),
        "review.requested",
        "milestone",
        extra={"event": "agent.diary"},
    )

    (record,) = _records(tmp_path)
    assert record["event"] == "review.requested"


def test_emit_once_dedupes_on_the_identity_key(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    log = logging.getLogger("shipit.prstate")
    sightings = events.Sightings()

    assert events.emit_once(
        sightings, log, "review.received", ("acme/widget", 368, 11), "review received"
    )
    assert not events.emit_once(
        sightings, log, "review.received", ("acme/widget", 368, 11), "review received"
    )
    assert events.emit_once(
        sightings, log, "review.received", ("acme/widget", 368, 12), "review received"
    )

    records = _records(tmp_path)
    assert len(records) == 2
    assert {r["event"] for r in records} == {"review.received"}


def test_emit_once_scope_is_the_registry_value_not_the_process(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    log = logging.getLogger("shipit.prstate")

    first = events.Sightings()
    assert events.emit_once(
        first, log, "review.received", ("acme/widget", 368, 11), "review received"
    )
    second = events.Sightings()
    assert events.emit_once(
        second, log, "review.received", ("acme/widget", 368, 11), "review received"
    )
    assert not events.emit_once(
        first, log, "review.received", ("acme/widget", 368, 11), "review received"
    )
    assert len(_records(tmp_path)) == 2


def test_emit_once_keys_are_scoped_per_event_name(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    log = logging.getLogger("shipit.prstate")
    sightings = events.Sightings()

    assert events.emit_once(
        sightings, log, "round.detected", ("acme/widget", 368), "round"
    )
    assert events.emit_once(
        sightings, log, "breaker.fired", ("acme/widget", 368), "breaker"
    )
    assert [r["event"] for r in _records(tmp_path)] == [
        "round.detected",
        "breaker.fired",
    ]


def test_emit_once_unknown_name_raises_and_never_poisons_the_registry(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    log = logging.getLogger("shipit.prstate")
    sightings = events.Sightings()

    with pytest.raises(ValueError, match="unknown dev-cycle event"):
        events.emit_once(
            sightings, log, "review.recieved", ("acme/widget", 368, 11), "typo"
        )
    assert _records(tmp_path) == []
    assert events.emit_once(
        sightings, log, "review.received", ("acme/widget", 368, 11), "review received"
    )
    assert len(_records(tmp_path)) == 1
