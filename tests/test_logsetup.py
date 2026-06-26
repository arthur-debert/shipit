"""Behavioural tests for the console + CI surface sinks (OBS01-WS02).

These assert external behaviour — what reaches stderr / stdout / the step-summary
file — not the handler wiring, in line with shipit's testing convention.
"""

from __future__ import annotations

import logging

import pytest
from shipit import cli, logsetup


@pytest.fixture(autouse=True)
def _reset_shipit_logger():
    """Detach this module's handlers around every test so a configured logger
    from one test never leaks its sinks into the next."""
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    logsetup._clear_own_handlers(logger)
    yield
    logsetup._clear_own_handlers(logger)


def _emit(level: int, message: str) -> None:
    logging.getLogger(logsetup.LOGGER_NAME).log(level, message)


# --- console: quiet by default -------------------------------------------------


def test_console_quiet_by_default_drops_below_warning(capfd):
    # No CI env, so only the console sink is attached.
    logsetup.configure_logging(verbose=False, env={})
    _emit(logging.INFO, "info-detail")
    _emit(logging.DEBUG, "debug-detail")
    _emit(logging.WARNING, "warn-surfaced")
    err = capfd.readouterr().err
    assert "info-detail" not in err
    assert "debug-detail" not in err
    assert "warn-surfaced" in err


def test_default_cli_invocation_emits_nothing_below_warning(capfd):
    # A normal command run must not change the user-facing surface: nothing
    # below WARNING reaches the console. Routed through a real subcommand so the
    # root group callback (which calls configure_logging) actually fires — a
    # bare top-level --help is eager and short-circuits before the callback.
    rc = cli.main(["lint", "--help"])
    assert rc == 0
    _emit(logging.INFO, "info-after-cli")
    err = capfd.readouterr().err
    assert "info-after-cli" not in err


# --- console: -v raises the level ---------------------------------------------


def test_verbose_raises_console_level(capfd):
    logsetup.configure_logging(verbose=True, env={})
    _emit(logging.INFO, "info-detail")
    _emit(logging.DEBUG, "debug-detail")
    err = capfd.readouterr().err
    assert "info-detail" in err
    assert "debug-detail" in err


def test_cli_verbose_flag_raises_console_level(capfd):
    # Drive it through the real CLI flag, not just configure_logging directly.
    # `-v` is a root-group option, so it precedes the subcommand; the subcommand
    # is present so the group callback fires before lint's own --help exits.
    cli.main(["-v", "lint", "--help"])
    _emit(logging.INFO, "info-via-flag")
    err = capfd.readouterr().err
    assert "info-via-flag" in err


# --- CI sink ------------------------------------------------------------------


def test_ci_detected_logs_go_to_stdout(capfd):
    logsetup.configure_logging(verbose=False, env={"CI": "true"})
    _emit(logging.INFO, "ci-record")
    captured = capfd.readouterr()
    assert "ci-record" in captured.out
    # The CI record lands on stdout, not on the quiet stderr console.
    assert "ci-record" not in captured.err


def test_github_step_summary_is_appended(tmp_path):
    summary = tmp_path / "step_summary.md"
    summary.write_text("pre-existing\n")
    logsetup.configure_logging(
        verbose=False,
        env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)},
    )
    _emit(logging.INFO, "summary-line")
    contents = summary.read_text()
    # Appended, not truncated.
    assert "pre-existing" in contents
    assert "summary-line" in contents


def test_no_ci_means_no_stdout_handler(capfd):
    logsetup.configure_logging(verbose=False, env={})
    _emit(logging.INFO, "info-detail")
    _emit(logging.WARNING, "warn-detail")
    out = capfd.readouterr().out
    assert "info-detail" not in out
    assert "warn-detail" not in out


def test_is_ci_injectable_and_ignores_falsey_values():
    assert logsetup.is_ci({"CI": "true"}) is True
    assert logsetup.is_ci({"GITHUB_ACTIONS": "true"}) is True
    assert logsetup.is_ci({"CI": "false"}) is False
    assert logsetup.is_ci({"CI": ""}) is False
    assert logsetup.is_ci({}) is False


# --- independence + idempotency ------------------------------------------------


def test_console_and_ci_levels_independent_of_file_handler(capfd):
    # WS02 owns only console + CI. Simulate a sibling file handler at DEBUG on
    # the same logger and confirm it does not change what the console shows: the
    # console stays quiet (WARNING+) regardless of the file sink's level.
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    file_like = _Capture()
    file_like.setLevel(logging.DEBUG)
    # A non-"shipit-" name so configure_logging's own-handler sweep leaves it be,
    # mimicking the sibling file sink that the coordinator merges in later.
    file_like.set_name("file-sink-stub")
    logger.addHandler(file_like)
    try:
        logsetup.configure_logging(verbose=False, env={})
        _emit(logging.INFO, "info-detail")

        # File-like sink saw the INFO record (it is at DEBUG)...
        assert any(r.getMessage() == "info-detail" for r in records)
        # ...but the console did not surface it (still WARNING+).
        assert "info-detail" not in capfd.readouterr().err
    finally:
        logger.removeHandler(file_like)


def test_repeated_configure_does_not_stack_handlers():
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    logsetup.configure_logging(verbose=False, env={"CI": "true"})
    first = len([h for h in logger.handlers if (h.name or "").startswith("shipit-")])
    logsetup.configure_logging(verbose=False, env={"CI": "true"})
    second = len([h for h in logger.handlers if (h.name or "").startswith("shipit-")])
    assert first == second
