from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from shipit import logcontext, logsetup
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


def _emit(message: str) -> None:
    logging.getLogger(logsetup.LOGGER_NAME).info(message)


def test_bound_keys_land_on_every_subsequent_record(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(repo="acme/widget", pr=231)

    _emit("first")
    _emit("second")

    records = _records(tmp_path)
    assert len(records) == 2
    for record in records:
        assert record["repo"] == "acme/widget"
        assert record["pr"] == 231


def test_unbound_keys_are_absent_not_null(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(pr=7)

    _emit("hello")

    (record,) = _records(tmp_path)
    assert record["pr"] == 7
    for name in ("session", "tree", "run", "repo", "epic", "ws", "agent", "role"):
        assert name not in record


def test_unbind_removes_the_key_from_later_records(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(pr=7, repo="acme/widget")

    _emit("while-bound")
    logcontext.unbind("pr")
    _emit("after-unbind")

    while_bound, after = _records(tmp_path)
    assert while_bound["pr"] == 7
    assert "pr" not in after
    assert after["repo"] == "acme/widget"


def test_cleared_suppresses_a_key_for_the_block_then_restores_it(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(ws=3, epic="OLD01")

    with logcontext.cleared("ws"):
        _emit("inside")
    _emit("after")

    inside, after = _records(tmp_path)
    assert "ws" not in inside
    assert inside["epic"] == "OLD01"
    assert after["ws"] == 3


def test_cleared_of_an_unbound_key_is_a_noop_and_restores_nothing(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    with logcontext.cleared("ws"):
        pass
    assert "ws" not in logcontext.bound()


def test_cleared_restores_absence_when_the_block_binds_the_key(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    with logcontext.cleared("ws"):
        logcontext.bind(ws=9)
        assert logcontext.bound()["ws"] == 9
    assert "ws" not in logcontext.bound()


def test_cleared_in_block_rebind_still_restores_the_prior_value(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(ws=3)
    with logcontext.cleared("ws"):
        logcontext.bind(ws=9)
    assert logcontext.bound()["ws"] == 3


def test_cleared_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown domain key"):
        with logcontext.cleared("wsss"):
            pass


def test_bind_drops_none_values():
    logcontext.bind(pr=5, run=None)
    assert logcontext.bound() == {"pr": 5}


def test_the_key_set_is_closed():
    with pytest.raises(ValueError, match="unknown domain key"):
        logcontext.bind(sesion="oops")
    with pytest.raises(ValueError, match="unknown domain key"):
        logcontext.unbind("sesion")
    with pytest.raises(ValueError, match="unknown domain key"):
        logcontext.env_export({}, sesion="oops")


def test_bound_reports_only_domain_keys():
    import structlog

    structlog.contextvars.bind_contextvars(request_id="not-a-domain-key")
    logcontext.bind(tree="/trees/x")
    assert logcontext.bound() == {"tree": "/trees/x"}
    assert "SHIPIT_LOG_CTX_REQUEST_ID" not in logcontext.env_export({})


def test_env_export_carries_bound_keys_without_mutating_the_input():
    logcontext.bind(pr=231, repo="acme/widget")
    base = {"PATH": "/usr/bin"}

    child_env = logcontext.env_export(base)

    assert child_env["SHIPIT_LOG_CTX_PR"] == "231"
    assert child_env["SHIPIT_LOG_CTX_REPO"] == "acme/widget"
    assert child_env["PATH"] == "/usr/bin"
    assert base == {"PATH": "/usr/bin"}
    assert "SHIPIT_LOG_CTX_SESSION" not in child_env


def test_env_export_extra_reaches_the_child_without_binding_the_parent():
    logcontext.bind(pr=5)

    child_env = logcontext.env_export({}, run=555, session=None)

    assert child_env["SHIPIT_LOG_CTX_RUN"] == "555"
    assert "SHIPIT_LOG_CTX_SESSION" not in child_env
    assert logcontext.bound() == {"pr": 5}


def test_env_export_scrubs_inherited_ctx_vars_for_unbound_keys():
    inherited = {"SHIPIT_LOG_CTX_RUN": "111", "SHIPIT_LOG_CTX_PR": "9", "PATH": "/x"}

    child_env = logcontext.env_export(inherited)
    assert "SHIPIT_LOG_CTX_RUN" not in child_env
    assert "SHIPIT_LOG_CTX_PR" not in child_env
    assert child_env["PATH"] == "/x"

    child_env = logcontext.env_export(inherited, run=None)
    assert "SHIPIT_LOG_CTX_RUN" not in child_env

    logcontext.bind(pr=231)
    child_env = logcontext.env_export(inherited)
    assert child_env["SHIPIT_LOG_CTX_PR"] == "231"


def test_bind_from_env_round_trips_bound_keys_with_types():
    logcontext.bind(
        session="work",
        tree="/trees/x",
        pr=231,
        run=555,
        repo="acme/widget",
        epic="RVW01",
        ws=1,
        agent="a1b2c3",
        role="implementer",
    )
    exported = logcontext.env_export({})

    import structlog

    structlog.contextvars.clear_contextvars()
    logcontext.bind_from_env(exported)

    assert logcontext.bound() == {
        "session": "work",
        "tree": "/trees/x",
        "pr": 231,
        "run": 555,
        "repo": "acme/widget",
        "epic": "RVW01",
        "ws": 1,
        "agent": "a1b2c3",
        "role": "implementer",
    }


def test_ws_binds_and_records_as_int(tmp_path):
    logsetup.configure_logging(env={}, repo=REPO, base_dir=tmp_path)
    logcontext.bind(epic="LOG04", ws=1)

    _emit("workstream record")

    (record,) = _records(tmp_path)
    assert record["epic"] == "LOG04"
    assert record["ws"] == 1
    assert logcontext.env_export({})["SHIPIT_LOG_CTX_WS"] == "1"


def test_bind_from_env_ignores_absent_and_empty_vars():
    logcontext.bind_from_env({"SHIPIT_LOG_CTX_PR": "", "UNRELATED": "x"})
    assert logcontext.bound() == {}


def test_bind_from_env_degrades_malformed_numeric_to_string():
    logcontext.bind_from_env({"SHIPIT_LOG_CTX_PR": "not-a-number"})
    assert logcontext.bound() == {"pr": "not-a-number"}


def test_role_from_env_reads_the_exported_role_directly():
    exported = logcontext.env_export({}, role="implementer")
    assert logcontext.role_from_env(exported) == "implementer"
    assert (
        logcontext.role_from_env({"SHIPIT_LOG_CTX_ROLE": "  shepherd  "}) == "shepherd"
    )
    assert logcontext.role_from_env({"SHIPIT_LOG_CTX_ROLE": "   "}) is None
    assert logcontext.role_from_env({}) is None


def test_configure_logging_rebinds_parent_exported_keys(tmp_path):
    logsetup.configure_logging(
        env={"SHIPIT_LOG_CTX_PR": "231", "SHIPIT_LOG_CTX_REPO": "acme/widget"},
        repo=REPO,
        base_dir=tmp_path,
    )

    _emit("child-record")

    (record,) = _records(tmp_path)
    assert record["pr"] == 231
    assert record["repo"] == "acme/widget"


def test_cli_entry_binds_the_resolved_repo(monkeypatch):
    from shipit import cli
    from shipit.identity import Revision, WorkingDir
    from shipit.verbs._context import RootContext

    root_ctx = RootContext(
        working_dir=WorkingDir(path=".", repo=REPO, revision=Revision())
    )
    monkeypatch.setattr(cli, "resolve_root_context", lambda: root_ctx)
    seen: dict = {}
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: seen.update(kw))

    rc = cli.main(["lint", "--help"])

    assert rc == 0
    assert logcontext.bound()["repo"] == "acme/widget"
    assert seen["repo"] == REPO


def test_cli_entry_binds_nothing_outside_a_checkout(monkeypatch):
    from shipit import cli
    from shipit.verbs._context import RootContext

    monkeypatch.setattr(
        cli, "resolve_root_context", lambda: RootContext(working_dir=None)
    )
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: None)

    rc = cli.main(["lint", "--help"])

    assert rc == 0
    assert "repo" not in logcontext.bound()


def test_env_rebind_wins_over_an_earlier_best_effort_bind(tmp_path):
    logcontext.bind(repo="cwd/guess")
    logsetup.configure_logging(
        env={"SHIPIT_LOG_CTX_REPO": "parent/explicit"},
        repo=REPO,
        base_dir=tmp_path,
    )

    assert logcontext.bound()["repo"] == "parent/explicit"
