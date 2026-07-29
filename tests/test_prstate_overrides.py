from __future__ import annotations

import json
import logging

import pytest

from shipit import events
from shipit.finding import Severity
from shipit.identity import repo_from_slug
from shipit.prstate.errors import PrStateError
from shipit.prstate.overrides import (
    OVERRIDE_EVENT,
    load_overrides,
    record_override,
)

REPO = repo_from_slug("owner/repo")


def override_record(pr: int, comment: int, severity: str, **extra) -> str:
    return json.dumps(
        {
            "event": OVERRIDE_EVENT,
            "pr": pr,
            "comment": comment,
            "severity": severity,
            "msg": f"finding {comment} on pr#{pr} severity overridden to {severity}",
            **extra,
        }
    )


def write_log(base, lines, name="shipit.log"):
    log_dir = base / "owner" / "repo"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_event_is_registered():
    assert OVERRIDE_EVENT in events.EVENT_NAMES


def test_load_folds_the_pr_scoped_overrides(tmp_path):
    write_log(
        tmp_path,
        [
            override_record(5, 100, "nit"),
            override_record(5, 200, "major", reason="changes behaviour"),
            override_record(7, 300, "minor"),
            json.dumps({"msg": "an ordinary non-event record", "pr": 5}),
            "not json at all",
        ],
    )
    assert load_overrides(REPO, 5, base_dir=tmp_path) == {
        100: Severity.NIT,
        200: Severity.MAJOR,
    }
    assert load_overrides(REPO, 7, base_dir=tmp_path) == {300: Severity.MINOR}


def test_load_missing_file_is_no_overrides(tmp_path):
    assert load_overrides(REPO, 5, base_dir=tmp_path) == {}


def test_load_ignores_malformed_override_fields(tmp_path):
    write_log(
        tmp_path,
        [
            override_record(5, 100, "nitpick"),
            override_record(5, 150, "ERROR"),
            json.dumps(
                {
                    "event": OVERRIDE_EVENT,
                    "pr": 5,
                    "comment": "100",
                    "severity": "nit",
                }
            ),
            json.dumps({"event": OVERRIDE_EVENT, "pr": 5, "severity": "nit"}),
        ],
    )
    assert load_overrides(REPO, 5, base_dir=tmp_path) == {}


def test_load_reads_rotated_backups_and_first_write_wins(tmp_path):
    write_log(tmp_path, [override_record(5, 100, "nit")], name="shipit.log.1")
    write_log(tmp_path, [override_record(5, 100, "critical")])
    assert load_overrides(REPO, 5, base_dir=tmp_path) == {100: Severity.NIT}


def test_record_emits_the_registered_event_with_flat_identity(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        record_override(
            REPO, 5, 123, Severity.NIT, reason="wording only", base_dir=tmp_path
        )
    (rec,) = [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == OVERRIDE_EVENT
    ]
    assert rec.pr == 5
    assert rec.comment == 123
    assert rec.severity == "nit"
    assert rec.reason == "wording only"


def test_record_drops_a_whitespace_only_reason(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        record_override(REPO, 5, 123, Severity.NIT, reason="   ", base_dir=tmp_path)
    (rec,) = [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == OVERRIDE_EVENT
    ]
    assert not hasattr(rec, "reason")


def test_record_refuses_a_re_override(tmp_path):
    write_log(tmp_path, [override_record(5, 123, "nit")])
    with pytest.raises(PrStateError, match="already carries the severity"):
        record_override(REPO, 5, 123, Severity.MAJOR, base_dir=tmp_path)
    with pytest.raises(PrStateError, match="written once"):
        record_override(REPO, 5, 123, Severity.NIT, base_dir=tmp_path)


def test_record_scopes_the_write_once_guard_per_pr(tmp_path, caplog):
    write_log(tmp_path, [override_record(5, 123, "nit")])
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        record_override(REPO, 9, 123, Severity.MAJOR, base_dir=tmp_path)
    assert any(
        getattr(r, events.EXTRA_KEY, None) == OVERRIDE_EVENT for r in caplog.records
    )
