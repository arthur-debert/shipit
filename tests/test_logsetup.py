from __future__ import annotations

import datetime
import json
import logging
import os
import tomllib
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import structlog

from shipit import cli, logsetup
from shipit.identity import repo_from_slug


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


@pytest.fixture(autouse=True)
def _no_ambient_ci(monkeypatch):
    for var in logsetup._CI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _clean_structlog_contextvars():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _emit(level: int, message: str) -> None:
    logging.getLogger(logsetup.LOGGER_NAME).log(level, message)


def test_console_quiet_by_default_drops_below_warning(capfd):
    logsetup.configure_logging(verbose=False, env={})
    _emit(logging.INFO, "info-detail")
    _emit(logging.DEBUG, "debug-detail")
    _emit(logging.WARNING, "warn-surfaced")
    err = capfd.readouterr().err
    assert "info-detail" not in err
    assert "debug-detail" not in err
    assert "warn-surfaced" in err


def test_default_cli_invocation_emits_nothing_below_warning(capfd):
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
    cli.main(["-v", "lint", "--help"])
    _emit(logging.INFO, "info-via-flag")
    err = capfd.readouterr().err
    assert "info-via-flag" in err


def test_ci_detected_logs_go_to_stderr(capfd):
    logsetup.configure_logging(verbose=False, env={"CI": "true"})
    _emit(logging.INFO, "ci-record")
    captured = capfd.readouterr()
    assert "ci-record" in captured.err
    assert "ci-record" not in captured.out


def test_ci_captures_debug(capfd):
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
    assert "pre-existing" in contents
    assert "summary-line" in contents


def test_unopenable_step_summary_does_not_crash(capfd, tmp_path):
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


def test_resolve_log_dir_is_per_repo_under_base(tmp_path):
    base = tmp_path / "Logs" / "shipit"
    path = logsetup.resolve_log_dir(
        repo_from_slug("octocat/hello-world"), base_dir=base
    )
    assert path == base / "octocat" / "hello-world"


def test_resolve_log_dir_uses_platformdirs_when_base_omitted(monkeypatch, tmp_path):
    captured = {}

    def fake_user_log_dir(appname):
        captured["appname"] = appname
        return str(tmp_path / "platform-base")

    monkeypatch.setattr(logsetup.platformdirs, "user_log_dir", fake_user_log_dir)
    path = logsetup.resolve_log_dir(repo_from_slug("acme/widgets"))
    assert captured["appname"] == "shipit"
    assert path == tmp_path / "platform-base" / "acme" / "widgets"


def test_file_handler_is_rotating_with_5mb_x_3_bound(tmp_path):
    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
    finally:
        handler.close()


def test_file_handler_writes_to_per_repo_path(tmp_path):
    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
    try:
        assert Path(handler.baseFilename) == tmp_path / "o" / "r" / "shipit.log"
        assert (tmp_path / "o" / "r").is_dir()
    finally:
        handler.close()


def test_file_handler_level_is_debug_independent_of_console(tmp_path):
    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
    try:
        assert handler.level == logging.DEBUG
    finally:
        handler.close()


def test_file_handler_rolls_over_rather_than_growing_unbounded(tmp_path, monkeypatch):
    monkeypatch.setattr(logsetup, "MAX_BYTES", 512)
    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
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
    assert (log_dir / "shipit.log") in backups
    assert any(p.name != "shipit.log" for p in backups), "never rolled over"
    assert len(backups) <= logsetup.BACKUP_COUNT + 1
    one_record_slack = 256
    for path in backups:
        assert path.stat().st_size <= logsetup.MAX_BYTES + one_record_slack, (
            f"{path} grew past the bound"
        )


def test_configure_logging_attaches_one_file_handler(tmp_path):
    logsetup.configure_logging(repo=repo_from_slug("o/r"), base_dir=tmp_path)
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert logger.level == logging.DEBUG


def test_configure_logging_survives_an_unopenable_file_sink(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permission bits do not bind as root")
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        logsetup.configure_logging(
            env={}, repo=repo_from_slug("o/r"), base_dir=read_only
        )
    finally:
        read_only.chmod(0o700)
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    assert any((h.name or "").startswith("shipit-") for h in logger.handlers)


def test_configure_logging_is_idempotent(tmp_path):
    logsetup.configure_logging(repo=repo_from_slug("o/r"), base_dir=tmp_path)
    logsetup.configure_logging(repo=repo_from_slug("o/r"), base_dir=tmp_path)
    logsetup.configure_logging(repo=repo_from_slug("o/r"), base_dir=tmp_path)
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1, "duplicate handler attached on repeat call"


def test_configure_logging_resolves_repo_via_identity_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        logsetup.identity,
        "resolve_repo",
        lambda cwd=".", **kw: repo_from_slug("arthur-debert/shipit"),
    )
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


def test_configure_logging_rejects_unresolvable_identity(tmp_path, monkeypatch):
    def _raise(cwd=".", **kw):
        raise ValueError("cannot parse owner/name from remote url: 'not-a-url'")

    monkeypatch.setattr(logsetup.identity, "resolve_repo", _raise)
    with pytest.raises(ValueError, match="owner/name"):
        logsetup.configure_logging(base_dir=tmp_path)


def test_no_file_params_means_no_file_handler():
    logsetup.configure_logging(verbose=False, env={})
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)


def test_all_three_sinks_attach_together(capfd, tmp_path):
    logsetup.configure_logging(
        verbose=False, env={"CI": "true"}, repo=repo_from_slug("o/r"), base_dir=tmp_path
    )
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    names = {h.name for h in logger.handlers}
    assert "shipit-console" in names
    assert "shipit-ci" in names
    assert "shipit-file" in names


def test_repeated_configure_does_not_stack_handlers(tmp_path):
    logsetup.configure_logging(
        verbose=False, env={"CI": "true"}, repo=repo_from_slug("o/r"), base_dir=tmp_path
    )
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    first = len([h for h in logger.handlers if (h.name or "").startswith("shipit-")])
    logsetup.configure_logging(
        verbose=False, env={"CI": "true"}, repo=repo_from_slug("o/r"), base_dir=tmp_path
    )
    second = len([h for h in logger.handlers if (h.name or "").startswith("shipit-")])
    assert first == second


def test_console_level_independent_of_file_handler(capfd, tmp_path):
    logsetup.configure_logging(
        verbose=False, env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path
    )
    _emit(logging.INFO, "info-detail")
    assert "info-detail" not in capfd.readouterr().err
    log_file = tmp_path / "o" / "r" / "shipit.log"
    for h in logging.getLogger(logsetup.LOGGER_NAME).handlers:
        h.flush()
    assert "info-detail" in log_file.read_text()


def _file_records(base_dir: Path) -> list[dict]:
    for handler in logging.getLogger(logsetup.LOGGER_NAME).handlers:
        handler.flush()
    text = (base_dir / "o" / "r" / "shipit.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_file_sink_emits_one_json_object_per_record(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    _emit(logging.INFO, "first record")
    _emit(logging.WARNING, "second record")
    records = _file_records(tmp_path)
    assert [r["msg"] for r in records] == ["first record", "second record"]


def test_file_record_contract_flat_ts_level_logger_msg(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    logging.getLogger("shipit.sub.module").warning("contract %s", "check")
    (record,) = _file_records(tmp_path)
    assert record["level"] == "warning"
    assert record["logger"] == "shipit.sub.module"
    assert record["msg"] == "contract check"
    ts = datetime.datetime.fromisoformat(record["ts"])
    assert ts.utcoffset() == datetime.timedelta(0)
    assert all(not isinstance(v, (dict, list)) for v in record.values()), record


def test_unbound_fields_are_absent_not_null(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    _emit(logging.INFO, "bare record")
    (record,) = _file_records(tmp_path)
    assert set(record) == {"ts", "level", "logger", "msg"}
    assert None not in record.values()


def test_bound_domain_keys_land_flat_and_leave_on_unbind(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    structlog.contextvars.bind_contextvars(pr=231, session="work")
    _emit(logging.INFO, "bound record")
    structlog.contextvars.clear_contextvars()
    _emit(logging.INFO, "unbound record")
    bound, unbound = _file_records(tmp_path)
    assert bound["pr"] == 231
    assert bound["session"] == "work"
    assert "pr" not in unbound
    assert "session" not in unbound


def test_only_domain_keys_merge_from_context_never_rogue_contextvars(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    structlog.contextvars.bind_contextvars(pr=231, request_id="rogue-77")
    _emit(logging.INFO, "vocabulary record")
    (record,) = _file_records(tmp_path)
    assert record["pr"] == 231
    assert "request_id" not in record


def test_stdlib_extra_lands_as_flat_event_extras(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    logging.getLogger("shipit.spawn").info(
        "child launched", extra={"phase": "spawn", "attempt": 2}
    )
    (record,) = _file_records(tmp_path)
    assert record["msg"] == "child launched"
    assert record["phase"] == "spawn"
    assert record["attempt"] == 2


def test_container_extras_degrade_to_repr_never_nest(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
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
    assert all(not isinstance(v, (dict, list)) for v in record.values()), record


def test_foreign_stdlib_records_flow_through_the_chain(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    structlog.contextvars.bind_contextvars(tree="WS01")
    logging.getLogger("shipit.foreign").info("plain %d sites", 2)
    (record,) = _file_records(tmp_path)
    assert record["msg"] == "plain 2 sites"
    assert record["logger"] == "shipit.foreign"
    assert record["tree"] == "WS01"


def test_exception_is_flattened_to_a_string_field(tmp_path):
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
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
    logsetup.configure_logging(env={}, repo=repo_from_slug("o/r"), base_dir=tmp_path)
    _emit(logging.WARNING, "surfaced warning")
    err = capfd.readouterr().err
    assert "WARNING shipit: surfaced warning" in err
    assert not err.lstrip().startswith("{")


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
    source = Path(logsetup.__file__).read_text()
    assert "sys.platform" not in source
    assert "platform.system" not in source
    assert "SHIPIT_LOG_DIR" not in source


def test_case_divergent_sources_land_one_log_dir(tmp_path):
    class _CaseyGit:
        def remote_url(self, *, cwd, remote="origin"):
            return "git@github.com:AcMe/WiDgEt.git"

    from shipit.identity import resolve_repo

    from_origin = resolve_repo("/checkout", boundary=_CaseyGit())
    from_api_slug = repo_from_slug("ACME/Widget")

    dir_from_origin = logsetup.resolve_log_dir(from_origin, base_dir=tmp_path)
    dir_from_slug = logsetup.resolve_log_dir(from_api_slug, base_dir=tmp_path)
    assert dir_from_origin == dir_from_slug == tmp_path / "acme" / "widget"


COORDINATOR_LEAF = "shipit-claude-20260729-233341-297e983f-54ce-4a8f-8afa-c9dea2a23c8b"
SUBAGENT_LEAF = "shipit-claude-20260730-015230-141f2c5a-1ca0-4170-b86d-b3a10f3e8c3a"


@pytest.fixture
def two_trees(monkeypatch, tmp_path):
    """A Trees root with a spawning session's Tree and a spawned Run's, as `(coordinator, subagent)`."""
    root = tmp_path / "trees"
    coordinator, subagent = root / COORDINATOR_LEAF, root / SUBAGENT_LEAF
    for path in (coordinator, subagent):
        path.mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(root))
    return coordinator, subagent


def test_a_spawned_run_is_attributed_to_its_own_tree(two_trees, monkeypatch):
    """A native subagent inherits the spawner's `SHIPIT_LOG_CTX_TREE`, so without this its records read as the coordinator's (#1179)."""
    coordinator, subagent = two_trees
    monkeypatch.chdir(subagent)

    logsetup.rebind_own_tree({"SHIPIT_LOG_CTX_TREE": str(coordinator)})

    bound = structlog.contextvars.get_contextvars()
    assert bound["tree"] == str(subagent)
    assert bound["agent"] == "141f2c5a-1ca0-4170-b86d-b3a10f3e8c3a"


def test_the_spawning_session_itself_is_left_alone(two_trees, monkeypatch):
    coordinator, _subagent = two_trees
    monkeypatch.chdir(coordinator)

    logsetup.rebind_own_tree({"SHIPIT_LOG_CTX_TREE": str(coordinator)})

    assert "agent" not in structlog.contextvars.get_contextvars()


@pytest.mark.parametrize("env", [{}, {"SHIPIT_LOG_CTX_TREE": ""}])
def test_nothing_is_rebound_without_an_inherited_tree(two_trees, monkeypatch, env):
    """The mismatch IS the discriminator; with nothing to differ from there is nothing to correct."""
    _coordinator, subagent = two_trees
    monkeypatch.chdir(subagent)

    logsetup.rebind_own_tree(env)

    assert structlog.contextvars.get_contextvars() == {}


def test_a_cwd_outside_the_trees_root_is_left_alone(two_trees, monkeypatch, tmp_path):
    coordinator, _subagent = two_trees
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.chdir(ambient)

    logsetup.rebind_own_tree({"SHIPIT_LOG_CTX_TREE": str(coordinator)})

    assert structlog.contextvars.get_contextvars() == {}


def test_a_misconfigured_trees_root_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SHIPIT_TREES_ROOT", "relative/not-absolute")
    monkeypatch.chdir(tmp_path)

    logsetup.rebind_own_tree({"SHIPIT_LOG_CTX_TREE": "/trees/whatever"})

    assert structlog.contextvars.get_contextvars() == {}
