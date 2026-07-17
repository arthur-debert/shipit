"""Unit tests for current-session resolution (LOG04-WS04, ADR-0027 / ADR-0074).

:func:`shipit.session.current.current_session_id` is the ONE reader of "the
current session": the session environment first (the SessionStart hook's
``SHIPIT_LOG_CTX_SESSION`` export), the containing flat Tree's trailing ``<id>``
second, ``None`` when this process is in no session. Since ADR-0074 every Tree is
ONE flat directory below the central root — ``<repo>-<agent>-<timestamp>-<id>`` —
so the path signal is pure containment plus a single truncation to ``parts[0]``
(no depth arithmetic, no ``tree_kind``), and the session id is the leaf's trailing
UUID. Both boundaries are injected — no test reads the real environment or cwd.
"""

from __future__ import annotations

from pathlib import Path

from shipit import logcontext
from shipit.session import current
from shipit.tree import layout

#: A coordinator session Tree's ``<id>`` is a full UUID (ADR-0074 / naming.lex §4),
#: and that UUID IS the resume/log-context session id — never a pid, never truncated.
SESSION_ID = "619cf51a-f501-44dc-992f-74df773204aa"

#: The single flat leaf shape: ``<repo>-<agent>-<timestamp>-<id>``, one segment
#: below the central root.
SESSION_LEAF = f"shipit-claude-20260703-041649-{SESSION_ID}"


def _flat_tree(root: Path, leaf: str = SESSION_LEAF) -> Path:
    """A flat session-Tree dir directly under ``root`` (the path IS the signal)."""
    tree = root / leaf
    tree.mkdir(parents=True)
    return tree


def test_environment_export_wins(tmp_path):
    # The hook's exported var is the strongest signal: it works from ANY cwd
    # the session wanders to, so it is consulted before the path.
    env = {logcontext.ENV_PREFIX + "SESSION": SESSION_ID}
    assert current.current_session_id(env, cwd=tmp_path) == SESSION_ID


def test_flat_tree_leaf_resolves_without_the_export(tmp_path, monkeypatch):
    # The hook-less case (a bare shell cd'd into the Tree): the leaf's trailing UUID
    # IS the per-launch session id (ADR-0074) — the same value the hook binds.
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=tree) == SESSION_ID


def test_no_session_resolves_to_none(tmp_path, monkeypatch):
    # A plain checkout outside the central root is in no session.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    assert current.current_session_id({}, cwd=tmp_path) is None


def test_old_nested_leaf_without_a_trailing_uuid_resolves_to_none(
    tmp_path, monkeypatch
):
    # An OLD nested Tree still coexisting under the root (WS02 reclaims those by
    # attrition) has no flat-leaf trailing UUID, so it is not a resolvable session id —
    # the column/resolver reads None rather than a fabricated one.
    root = tmp_path / "trees"
    tree = root / "shipit-old-nested-thing"
    tree.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=tree) is None


def test_subdirectory_within_the_tree_still_resolves(tmp_path, monkeypatch):
    # cwd may be DEEPER than the Tree root — a bare shell cd'd into src/. The leaf is
    # the FIRST segment below the central root, so resolution truncates to it rather
    # than demanding an exact depth.
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    subdir = tree / "src" / "shipit"
    subdir.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=subdir) == SESSION_ID


def test_a_deeper_uuid_bearing_dir_never_wins_over_the_tree_root(tmp_path, monkeypatch):
    # Truncation to parts[0] means the FIRST segment below the root decides. A flat
    # Tree whose own leaf carries no session id (an old nested clone) is NOT rescued by
    # a deeper decoy dir that happens to look like a flat leaf — the decoy never wins.
    root = tmp_path / "trees"
    decoy = root / "shipit-old-nested-thing" / SESSION_LEAF
    decoy.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=decoy) is None


def test_broken_root_env_degrades_to_none_never_raises(tmp_path, monkeypatch):
    # central_root raises on a relative SHIPIT_TREES_ROOT; identification is
    # best-effort, so the resolver answers "no session", not a traceback.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/root")
    assert current.current_session_id({}, cwd=tmp_path) is None


def test_empty_export_falls_through_to_the_path(tmp_path, monkeypatch):
    # An empty var is no session id (absent-not-null crosses this seam too);
    # the path check still gets its turn.
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env = {logcontext.ENV_PREFIX + "SESSION": ""}
    assert current.current_session_id(env, cwd=tree) == SESSION_ID
