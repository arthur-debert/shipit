from __future__ import annotations

import io
import json
import logging

import pytest

from shipit.verbs.hook import eval as hook_eval
from shipit.verbs.hook import pretooluse, sessionstart, worktreecreate, worktreeremove

HOOK_LOGGER = "shipit.hook"


def _records(caplog, level):
    return [r for r in caplog.records if r.name == HOOK_LOGGER and r.levelno == level]


class _ExplodingStdin:
    def read(self):
        raise OSError("forced stdin failure")


def test_fail_closed_failure_logs_error_with_exception(monkeypatch, caplog):
    monkeypatch.setattr(worktreecreate.git, "repo_root", lambda: None)
    payload = {"session_id": "sess-123", "name": "x"}
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        rc = worktreecreate.run(
            stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO()
        )
    assert rc == 1
    errors = _records(caplog, logging.ERROR)
    assert errors, "a fail-closed abort must produce an ERROR record"
    assert all(r.exc_info for r in errors)


def test_fail_closed_error_record_carries_derivable_domain_keys(monkeypatch, caplog):
    monkeypatch.setattr(worktreecreate.git, "repo_root", lambda: None)
    payload = {"session_id": "sess-123", "name": "x"}
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        worktreecreate.run(stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO())
    errors = _records(caplog, logging.ERROR)
    assert any(getattr(r, "session", None) == "sess-123" for r in errors)


def test_fail_closed_pre_parse_failure_still_logs_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        rc = worktreecreate.run(stdin=io.StringIO("{not json"), stdout=io.StringIO())
    assert rc == 1
    errors = _records(caplog, logging.ERROR)
    assert errors and all(r.exc_info for r in errors)


def _force_pretooluse_failure():
    return pretooluse.run(stdin=io.StringIO("{not json"), stdout=io.StringIO())


def _force_sessionstart_failure():
    return sessionstart.run(stdin=_ExplodingStdin(), environ={})


def _force_worktreeremove_failure():
    return worktreeremove.run(stdin=io.StringIO("[1, 2]"))


def _force_eval_failure():
    return hook_eval.run(stdin=io.StringIO("{not json"))


@pytest.mark.parametrize(
    "force_failure",
    [
        _force_pretooluse_failure,
        _force_sessionstart_failure,
        _force_worktreeremove_failure,
        _force_eval_failure,
    ],
    ids=["pretooluse", "sessionstart", "worktreeremove", "eval"],
)
def test_fail_open_failure_logs_warning_and_continues(force_failure, caplog):
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        rc = force_failure()
    assert rc == 0
    warnings = _records(caplog, logging.WARNING)
    assert warnings, "a swallowed fail-open failure must produce a WARNING record"
    assert any(r.exc_info for r in warnings)
    assert not _records(caplog, logging.ERROR)


def test_sessionstart_malformed_payload_warns_with_exception_on_each_arm(
    monkeypatch, tmp_path, caplog
):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        rc = sessionstart.run(stdin=io.StringIO("{not json"), environ={})
    assert rc == 0
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 5, (
        "exactly the five parse-fallback arms (4× cwd + 1× session-id) produce "
        "a WARNING — the event step's own swallow is DEBUG by design"
    )
    assert all(r.exc_info for r in warnings)
    assert not _records(caplog, logging.ERROR)
