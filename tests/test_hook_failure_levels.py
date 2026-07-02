"""LOG03-WS02 (#311): hook failure-arm log levels match each hook's fail-mode.

The canon lives in :mod:`shipit.verbs.hook`: a fail-CLOSED hook's failure arm is
a propagating failure (the process exits non-zero) → ERROR with the exception
attached; a fail-OPEN hook's swallow is a degraded-but-continuing outcome →
WARNING with the exception attached, applied uniformly across all fail-open
hooks.

Convention-level assertions ONLY (PRD glassbox, Testing Decisions): matched by
level + ``exc_info`` presence + field presence — never per-message string
assertions — so wording can evolve without breaking the pin.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from shipit.verbs.hook import eval as hook_eval
from shipit.verbs.hook import pretooluse, sessionstart, worktreecreate, worktreeremove

HOOK_LOGGER = "shipit.hook"


def _records(caplog, level):
    """The captured ``shipit.hook`` records at exactly ``level``."""
    return [r for r in caplog.records if r.name == HOOK_LOGGER and r.levelno == level]


class _ExplodingStdin:
    """A stdin whose read raises — forces the hook-level failure arm."""

    def read(self):
        raise OSError("forced stdin failure")


# --------------------------------------------------------------------------
# fail-CLOSED — worktreecreate: the abort is a propagating failure → ERROR
# --------------------------------------------------------------------------


def test_fail_closed_failure_logs_error_with_exception(monkeypatch, caplog):
    # A parsed payload whose Tree creation fails (not in a checkout) exercises
    # the abort arm PAST the parse — the planning-failure shape #311 names.
    monkeypatch.setattr(worktreecreate.git, "repo_root", lambda: None)
    payload = {"session_id": "sess-123", "name": "x"}  # no prompt_id → coordinator
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        rc = worktreecreate.run(
            stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO()
        )
    assert rc == 1  # fail-closed: the spawn aborts
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
    # A payload-parse failure never reaches tree.create() — exactly the arm
    # that used to leave only a debug line for a spawn-aborting failure.
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        rc = worktreecreate.run(stdin=io.StringIO("{not json"), stdout=io.StringIO())
    assert rc == 1
    errors = _records(caplog, logging.ERROR)
    assert errors and all(r.exc_info for r in errors)


# --------------------------------------------------------------------------
# fail-OPEN — the swallow is degraded-but-continuing → WARNING, uniformly
# --------------------------------------------------------------------------


def _force_pretooluse_failure():
    return pretooluse.run(stdin=io.StringIO("{not json"), stdout=io.StringIO())


def _force_sessionstart_failure():
    return sessionstart.run(stdin=_ExplodingStdin(), environ={})


def _force_worktreeremove_failure():
    return worktreeremove.run(stdin=io.StringIO("[1, 2]"))  # payload not an object


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
    assert rc == 0  # fail-open: the operation continues
    warnings = _records(caplog, logging.WARNING)
    assert warnings, "a swallowed fail-open failure must produce a WARNING record"
    assert any(r.exc_info for r in warnings)
    # Uniform calibration: a swallowed failure is degraded, never a propagating
    # failure — no ERROR record for a fail-open arm.
    assert not _records(caplog, logging.ERROR)
