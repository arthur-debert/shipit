"""Behavioural tests for shipit's logging configuration (OBS01).

Asserts external behaviour in line with shipit's conventions — what reaches
stderr / stdout / the step-summary file, the resolved file-sink path, the
rotation bound, the handler levels, and the dependency / single-source-of-truth
constraints. The platformdirs base and the ``(owner, repo)`` namespace are
injected so nothing ever writes to a real ``$HOME``.
"""

from __future__ import annotations

import datetime
import json
import logging
import tomllib
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import structlog
from shipit import cli, logsetup
from shipit.execrun import ExecError


@pytest.fixture(autouse=True)
def _reset_package_logger():
    """Fully reset the process-lifetime ``shipit`` logger around each test, so a
    configured logger from one test never leaks its sinks/level into the next."""
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


@pytest.fixture(autouse=True)
def _no_ambient_ci(monkeypatch):
    """Strip the runner's own CI signals so a unit test exercises the genuine
    default path, never inheriting the environment shipit happens to run under.

    The CLI-driven tests call ``configure_logging`` with the real ``os.environ``
    (the CLI passes no ``env=``); under GitHub Actions the ambient ``CI`` /
    ``GITHUB_ACTIONS`` would otherwise attach the CI sink and change what reaches
    the console. Tests that want the CI path inject ``env=`` explicitly, so this
    leaves them untouched."""
    for var in logsetup._CI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _clean_structlog_contextvars():
    """No bound domain key may leak between tests — the absent-when-unbound
    contract is only assertable from a clean context."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _emit(level: int, message: str) -> None:
    logging.getLogger(logsetup.LOGGER_NAME).log(level, message)


# ==========================================================================
# Console sink — quiet by default, raised by -v
# ==========================================================================


def test_console_quiet_by_default_drops_below_warning(capfd):
    # No CI env and no file params, so only the console sink is attached.
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


# ==========================================================================
# CI sink — stderr job log + optional step summary
# ==========================================================================


def test_ci_detected_logs_go_to_stderr(capfd):
    logsetup.configure_logging(verbose=False, env={"CI": "true"})
    _emit(logging.INFO, "ci-record")
    captured = capfd.readouterr()
    # The CI record lands on stderr (still captured in the Actions job log),
    # leaving stdout clean for command / --json output it must not corrupt.
    assert "ci-record" in captured.err
    assert "ci-record" not in captured.out


def test_ci_captures_debug(capfd):
    # In CI the job log is the durable run record, so DEBUG must land there.
    logsetup.configure_logging(verbose=False, env={"CI": "true"})
    _emit(logging.DEBUG, "ci-debug-record")
    captured = capfd.readouterr()
    assert "ci-debug-record" in captured.err
    assert "ci-debug-record" not in captured.out


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


def test_unopenable_step_summary_does_not_crash(capfd, tmp_path):
    # An unwritable $GITHUB_STEP_SUMMARY (here: a path under a non-existent dir)
    # must not fail the command; the CI sink still works.
    bad_path = tmp_path / "missing-dir" / "summary.md"
    logsetup.configure_logging(
        verbose=False,
        env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(bad_path)},
    )
    _emit(logging.INFO, "still-running")
    assert "still-running" in capfd.readouterr().err
    assert not bad_path.exists()


def test_no_ci_means_no_ci_handler(capfd):
    logsetup.configure_logging(verbose=False, env={})
    _emit(logging.INFO, "info-detail")
    _emit(logging.WARNING, "warn-detail")
    captured = capfd.readouterr()
    # No CI sink: INFO is dropped everywhere; only the quiet console (WARNING+
    # on stderr) speaks, and stdout stays empty.
    assert "info-detail" not in captured.err
    assert "info-detail" not in captured.out
    assert "warn-detail" in captured.err
    assert "warn-detail" not in captured.out


def test_is_ci_injectable_and_ignores_falsey_values():
    assert logsetup.is_ci({"CI": "true"}) is True
    assert logsetup.is_ci({"GITHUB_ACTIONS": "true"}) is True
    assert logsetup.is_ci({"CI": "false"}) is False
    assert logsetup.is_ci({"CI": ""}) is False
    assert logsetup.is_ci({}) is False


# ==========================================================================
# File sink — path resolution (per-repo under the injected platformdirs base)
# ==========================================================================


def test_resolve_log_dir_is_per_repo_under_base(tmp_path):
    base = tmp_path / "Logs" / "shipit"
    path = logsetup.resolve_log_dir(("octocat", "hello-world"), base_dir=base)
    assert path == base / "octocat" / "hello-world"


def test_resolve_log_dir_uses_platformdirs_when_base_omitted(monkeypatch, tmp_path):
    captured = {}

    def fake_user_log_dir(appname):
        captured["appname"] = appname
        return str(tmp_path / "platform-base")

    monkeypatch.setattr(logsetup.platformdirs, "user_log_dir", fake_user_log_dir)
    path = logsetup.resolve_log_dir(("acme", "widgets"))
    # platformdirs owns the base, queried for the "shipit" app, then per-repo.
    assert captured["appname"] == "shipit"
    assert path == tmp_path / "platform-base" / "acme" / "widgets"


# ==========================================================================
# File sink — the handler is rotating, bounded, verbose
# ==========================================================================


def test_file_handler_is_rotating_with_5mb_x_3_bound(tmp_path):
    handler = logsetup.build_file_handler(("o", "r"), base_dir=tmp_path)
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
    finally:
        handler.close()


def test_file_handler_writes_to_per_repo_path(tmp_path):
    handler = logsetup.build_file_handler(("o", "r"), base_dir=tmp_path)
    try:
        assert Path(handler.baseFilename) == tmp_path / "o" / "r" / "shipit.log"
        assert (tmp_path / "o" / "r").is_dir()
    finally:
        handler.close()


def test_file_handler_level_is_debug_independent_of_console(tmp_path):
    handler = logsetup.build_file_handler(("o", "r"), base_dir=tmp_path)
    try:
        assert handler.level == logging.DEBUG
    finally:
        handler.close()


def test_file_handler_rolls_over_rather_than_growing_unbounded(tmp_path, monkeypatch):
    # Shrink the bound so a handful of records forces a rollover, then assert the
    # backups exist and no single file exceeds the cap (it rolled, not grew).
    monkeypatch.setattr(logsetup, "MAX_BYTES", 512)
    handler = logsetup.build_file_handler(("o", "r"), base_dir=tmp_path)
    log = logging.getLogger("shipit.test.rollover")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.propagate = False
    try:
        for i in range(200):
            log.debug("record %03d %s", i, "x" * 40)
    finally:
        handler.close()
        log.removeHandler(handler)

    log_dir = tmp_path / "o" / "r"
    backups = sorted(log_dir.glob("shipit.log*"))
    # Active file + at least one rolled backup, capped at backupCount + active.
    assert (log_dir / "shipit.log") in backups
    assert any(p.name != "shipit.log" for p in backups), "never rolled over"
    assert len(backups) <= logsetup.BACKUP_COUNT + 1
    # RotatingFileHandler rolls BEFORE a write that would cross maxBytes, so a
    # file can carry the one final record past the cap. Tie the bound to
    # MAX_BYTES (not a bare number) plus one record's worth of slack.
    one_record_slack = 256
    for path in backups:
        assert path.stat().st_size <= logsetup.MAX_BYTES + one_record_slack, (
            f"{path} grew past the bound"
        )


# ==========================================================================
# configure_logging — file-sink wiring + idempotence + boundary injection
# ==========================================================================


def test_configure_logging_attaches_one_file_handler(tmp_path):
    logsetup.configure_logging(owner_repo=("o", "r"), base_dir=tmp_path)
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert logger.level == logging.DEBUG


def test_configure_logging_is_idempotent(tmp_path):
    logsetup.configure_logging(owner_repo=("o", "r"), base_dir=tmp_path)
    logsetup.configure_logging(owner_repo=("o", "r"), base_dir=tmp_path)
    logsetup.configure_logging(owner_repo=("o", "r"), base_dir=tmp_path)
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1, "duplicate handler attached on repeat call"


def test_configure_logging_resolves_owner_repo_via_gh_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(logsetup.gh, "current_repo", lambda: "arthur-debert/shipit")
    logsetup.configure_logging(base_dir=tmp_path)
    handler = next(
        h
        for h in logging.getLogger(logsetup.LOGGER_NAME).handlers
        if isinstance(h, RotatingFileHandler)
    )
    try:
        assert (
            Path(handler.baseFilename)
            == tmp_path / "arthur-debert" / "shipit" / "shipit.log"
        )
    finally:
        handler.close()


def test_configure_logging_rejects_non_slug_from_gh(tmp_path, monkeypatch):
    # A boundary value that is not an 'owner/repo' slug must fail loud rather than
    # silently target an empty/incorrect log directory.
    monkeypatch.setattr(logsetup.gh, "current_repo", lambda: "not-a-slug")
    with pytest.raises(ValueError, match="owner/repo"):
        logsetup.configure_logging(base_dir=tmp_path)


def test_no_file_params_means_no_file_handler():
    # The surface-only call style (console/CI, used by WS02 tests and the CLI when
    # outside a repo): with neither owner_repo nor base_dir, no file sink and no
    # gh boundary call.
    logsetup.configure_logging(verbose=False, env={})
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)


def test_resolve_current_owner_repo_is_best_effort(monkeypatch):
    monkeypatch.setattr(
        logsetup.gh,
        "current_repo",
        lambda: (_ for _ in ()).throw(ExecError(["gh"], rc=1, stderr="no repo")),
    )
    assert logsetup.resolve_current_owner_repo() is None


def test_all_three_sinks_attach_together(capfd, tmp_path):
    # The merged shape: console + CI + file all on the logger at once.
    logsetup.configure_logging(
        verbose=False, env={"CI": "true"}, owner_repo=("o", "r"), base_dir=tmp_path
    )
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    names = {h.name for h in logger.handlers}
    assert "shipit-console" in names
    assert "shipit-ci" in names
    assert "shipit-file" in names


def test_repeated_configure_does_not_stack_handlers(tmp_path):
    logsetup.configure_logging(
        verbose=False, env={"CI": "true"}, owner_repo=("o", "r"), base_dir=tmp_path
    )
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    first = len([h for h in logger.handlers if (h.name or "").startswith("shipit-")])
    logsetup.configure_logging(
        verbose=False, env={"CI": "true"}, owner_repo=("o", "r"), base_dir=tmp_path
    )
    second = len([h for h in logger.handlers if (h.name or "").startswith("shipit-")])
    assert first == second


def test_console_level_independent_of_file_handler(capfd, tmp_path):
    # File sink at DEBUG must not change what the quiet console surfaces.
    logsetup.configure_logging(
        verbose=False, env={}, owner_repo=("o", "r"), base_dir=tmp_path
    )
    _emit(logging.INFO, "info-detail")
    # Console (stderr) stays quiet...
    assert "info-detail" not in capfd.readouterr().err
    # ...while the file sink (DEBUG) captured it.
    log_file = tmp_path / "o" / "r" / "shipit.log"
    for h in logging.getLogger(logsetup.LOGGER_NAME).handlers:
        h.flush()
    assert "info-detail" in log_file.read_text()


# ==========================================================================
# File sink — the JSONL record contract (LOG01-WS01, ADR-0029)
# ==========================================================================


def _file_records(base_dir: Path) -> list[dict]:
    """Parse the file sink's JSONL under the injected base — flushed first, one
    JSON object per non-empty line."""
    for handler in logging.getLogger(logsetup.LOGGER_NAME).handlers:
        handler.flush()
    text = (base_dir / "o" / "r" / "shipit.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_file_sink_emits_one_json_object_per_record(tmp_path):
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    _emit(logging.INFO, "first record")
    _emit(logging.WARNING, "second record")
    records = _file_records(tmp_path)
    assert [r["msg"] for r in records] == ["first record", "second record"]


def test_file_record_contract_flat_ts_level_logger_msg(tmp_path):
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    logging.getLogger("shipit.sub.module").warning("contract %s", "check")
    (record,) = _file_records(tmp_path)
    # The flat core fields, with %-style args resolved into msg.
    assert record["level"] == "warning"
    assert record["logger"] == "shipit.sub.module"
    assert record["msg"] == "contract check"
    # ts is ISO-8601 UTC (fromisoformat accepts both 'Z' and '+00:00').
    ts = datetime.datetime.fromisoformat(record["ts"])
    assert ts.utcoffset() == datetime.timedelta(0)
    # Flat: no nested objects or arrays anywhere in the record.
    assert all(not isinstance(v, (dict, list)) for v in record.values()), record


def test_unbound_fields_are_absent_not_null(tmp_path):
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    _emit(logging.INFO, "bare record")
    (record,) = _file_records(tmp_path)
    assert set(record) == {"ts", "level", "logger", "msg"}
    assert None not in record.values()


def test_bound_domain_keys_land_flat_and_leave_on_unbind(tmp_path):
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    structlog.contextvars.bind_contextvars(pr=231, session="work")
    _emit(logging.INFO, "bound record")
    structlog.contextvars.clear_contextvars()
    _emit(logging.INFO, "unbound record")
    bound, unbound = _file_records(tmp_path)
    # Bound keys are flat, top-level, jq-sliceable (`select(.pr==231)`)...
    assert bound["pr"] == 231
    assert bound["session"] == "work"
    # ...and absent (not null) once unbound.
    assert "pr" not in unbound
    assert "session" not in unbound


def test_only_domain_keys_merge_from_context_never_rogue_contextvars(tmp_path):
    # The correlation vocabulary is CLOSED (ADR-0029): the pipeline merges
    # exactly logcontext's domain keys. A contextvar bound around the
    # logcontext seam (a direct structlog bind — i.e. a typo or an unsanctioned
    # correlation key) never mints a top-level JSONL field.
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    structlog.contextvars.bind_contextvars(pr=231, request_id="rogue-77")
    _emit(logging.INFO, "vocabulary record")
    (record,) = _file_records(tmp_path)
    assert record["pr"] == 231
    assert "request_id" not in record


def test_stdlib_extra_lands_as_flat_event_extras(tmp_path):
    # The supported call-site idiom is untouched stdlib logging, so per-event
    # extras arrive the stdlib way — `extra={...}` — and must land as flat
    # top-level fields in the JSONL record (ExtraAdder in the pipeline;
    # ProcessorFormatter alone would silently drop them).
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    logging.getLogger("shipit.spawn").info(
        "child launched", extra={"phase": "spawn", "attempt": 2}
    )
    (record,) = _file_records(tmp_path)
    assert record["msg"] == "child launched"
    assert record["phase"] == "spawn"
    assert record["attempt"] == 2


def test_container_extras_degrade_to_repr_never_nest(tmp_path):
    # The flat contract is ENFORCED, not assumed: a container extra (dict,
    # list, tuple — all JSON-serializable, so a bare JSONRenderer would nest
    # them) degrades to its repr string, and a non-serializable object does
    # too, without crashing the log call.
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    logging.getLogger("shipit.containers").info(
        "container record",
        extra={
            "mapping": {"a": 1},
            "sequence": [1, 2],
            "pair": (3, 4),
            "opaque": object(),
        },
    )
    (record,) = _file_records(tmp_path)
    assert record["mapping"] == "{'a': 1}"
    assert record["sequence"] == "[1, 2]"
    assert record["pair"] == "(3, 4)"
    assert record["opaque"].startswith("<object object at ")
    # Nothing nested anywhere in the record.
    assert all(not isinstance(v, (dict, list)) for v in record.values()), record


def test_foreign_stdlib_records_flow_through_the_chain(tmp_path):
    # An untouched stdlib call site — logging.getLogger + %-args, no structlog
    # import — must yield the same contract record, INCLUDING bound context
    # (the foreign_pre_chain is the same pipeline).
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    structlog.contextvars.bind_contextvars(tree="WS01")
    logging.getLogger("shipit.foreign").info("plain %d sites", 2)
    (record,) = _file_records(tmp_path)
    assert record["msg"] == "plain 2 sites"
    assert record["logger"] == "shipit.foreign"
    assert record["tree"] == "WS01"


def test_exception_is_flattened_to_a_string_field(tmp_path):
    # exc_info must not break the one-line JSON contract: the traceback is a
    # single flat string field, the record still parses line-per-record.
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("shipit.err").exception("it failed")
    (record,) = _file_records(tmp_path)
    assert record["msg"] == "it failed"
    assert record["level"] == "error"
    assert isinstance(record["exception"], str)
    assert "RuntimeError: boom" in record["exception"]


def test_console_stays_human_not_json(capfd, tmp_path):
    # Hard cutover applies to the FILE format only: stderr keeps the human
    # `LEVEL logger: message` shape, not JSON.
    logsetup.configure_logging(env={}, owner_repo=("o", "r"), base_dir=tmp_path)
    _emit(logging.WARNING, "surfaced warning")
    err = capfd.readouterr().err
    assert "WARNING shipit: surfaced warning" in err
    assert not err.lstrip().startswith("{")


# ==========================================================================
# Dependency + single-source-of-truth constraints
# ==========================================================================


def test_structlog_declared_as_dependency():
    root = Path(__file__).resolve().parents[1]
    meta = tomllib.loads((root / "pyproject.toml").read_text())
    deps = meta["project"]["dependencies"]
    assert any(d.lower().startswith("structlog") for d in deps), deps


def test_platformdirs_declared_as_dependency():
    root = Path(__file__).resolve().parents[1]
    meta = tomllib.loads((root / "pyproject.toml").read_text())
    deps = meta["project"]["dependencies"]
    assert any(d.lower().startswith("platformdirs") for d in deps), deps


def test_no_hand_rolled_platform_branch_or_env_override():
    # platformdirs is the single source of truth: the module must not sniff the
    # platform itself nor read a bespoke log-dir env var.
    source = Path(logsetup.__file__).read_text()
    assert "sys.platform" not in source
    assert "platform.system" not in source
    assert "SHIPIT_LOG_DIR" not in source
