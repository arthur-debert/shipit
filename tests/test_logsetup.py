"""Unit tests for the durable file sink + logging config (OBS01-WS01).

Asserts external behavior in line with shipit's conventions: the resolved sink
path, the rotation bound, the file handler's verbose level, and that the
dependency / single-source-of-truth constraints hold. The platformdirs base and
the ``(owner, repo)`` namespace are injected so nothing ever writes to a real
``$HOME``.
"""

from __future__ import annotations

import logging
import tomllib
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from shipit import logsetup


@pytest.fixture(autouse=True)
def _reset_package_logger():
    """Detach any handlers around each test so the process-lifetime ``shipit``
    logger never leaks state (configure_logging mutates a module-global logger)."""
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    saved = list(logger.handlers)
    saved_level, saved_prop = logger.level, logger.propagate
    logger.handlers.clear()
    try:
        yield
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = saved
        logger.setLevel(saved_level)
        logger.propagate = saved_prop


# --------------------------------------------------------------------------
# Path resolution — per-repo namespace under the injected platformdirs base
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# The file handler — rotating, bounded, verbose
# --------------------------------------------------------------------------


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
    for path in backups:
        assert path.stat().st_size <= 4096, f"{path} grew past the bound"


# --------------------------------------------------------------------------
# configure_logging — wiring + idempotence + boundary injection
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Dependency + single-source-of-truth constraints
# --------------------------------------------------------------------------


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
