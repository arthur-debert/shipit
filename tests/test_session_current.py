"""Unit tests for current-session resolution (LOG04-WS04, ADR-0027).

:func:`shipit.session.current.current_session_id` is the ONE reader of "the
current session": the session environment first (the SessionStart hook's
``SHIPIT_LOG_CTX_SESSION`` export), the ephemeral Tree leaf of the cwd second,
``None`` when this process is in no session. Both boundaries are injected —
no test reads the real environment or working directory.
"""

from __future__ import annotations

from pathlib import Path

from shipit import logcontext
from shipit.session import current
from shipit.tree import layout

SESSION_LEAF = "sess-20260703-41649"


def _ephemeral_tree(root: Path, leaf: str = SESSION_LEAF) -> Path:
    """An ephemeral session-Tree dir under ``root`` (the path IS the signal)."""
    tree = root / "org" / "repo" / "ephemeral" / leaf
    tree.mkdir(parents=True)
    return tree


def test_environment_export_wins(tmp_path):
    # The hook's exported var is the strongest signal: it works from ANY cwd
    # the session wanders to, so it is consulted before the path.
    env = {logcontext.ENV_PREFIX + "SESSION": SESSION_LEAF}
    assert current.current_session_id(env, cwd=tmp_path) == SESSION_LEAF


def test_ephemeral_tree_leaf_resolves_without_the_export(tmp_path, monkeypatch):
    # The hook-less case (a bare shell cd'd into the Tree): the dir leaf IS the
    # per-launch session id (ADR-0027) — the same value tree/create.py binds.
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=tree) == SESSION_LEAF


def test_no_session_resolves_to_none(tmp_path, monkeypatch):
    # A plain checkout outside the central root is in no session.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    assert current.current_session_id({}, cwd=tmp_path) is None


def test_wrong_tree_kind_resolves_to_none(tmp_path, monkeypatch):
    # Under the central root but not the ephemeral kind: an epics Tree's leaf
    # is a Work Stream dir, never a session id.
    root = tmp_path / "trees"
    tree = root / "org" / "repo" / "epics" / "LOG04-WS04"
    tree.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=tree) is None


def test_broken_root_env_degrades_to_none_never_raises(tmp_path, monkeypatch):
    # central_root raises on a relative SHIPIT_TREES_ROOT; identification is
    # best-effort, so the resolver answers "no session", not a traceback.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/root")
    assert current.current_session_id({}, cwd=tmp_path) is None


def test_empty_export_falls_through_to_the_path(tmp_path, monkeypatch):
    # An empty var is no session id (absent-not-null crosses this seam too);
    # the path check still gets its turn.
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env = {logcontext.ENV_PREFIX + "SESSION": ""}
    assert current.current_session_id(env, cwd=tree) == SESSION_LEAF
