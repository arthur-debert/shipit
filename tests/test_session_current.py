from __future__ import annotations

from pathlib import Path

import pytest

from shipit import logcontext
from shipit.session import current
from shipit.tree import layout

SESSION_ID = "619cf51a-f501-44dc-992f-74df773204aa"

SESSION_LEAF = f"shipit-claude-20260703-041649-{SESSION_ID}"


def _flat_tree(root: Path, leaf: str = SESSION_LEAF) -> Path:
    tree = root / leaf
    tree.mkdir(parents=True)
    return tree


def test_environment_export_wins(tmp_path):
    env = {logcontext.ENV_PREFIX + "SESSION": SESSION_ID}
    assert current.current_session_id(env, cwd=tmp_path) == SESSION_ID


def test_flat_tree_leaf_resolves_without_the_export(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=tree) == SESSION_ID


def test_no_session_resolves_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    assert current.current_session_id({}, cwd=tmp_path) is None


def test_old_nested_leaf_without_a_trailing_uuid_resolves_to_none(
    tmp_path, monkeypatch
):
    root = tmp_path / "trees"
    tree = root / "shipit-old-nested-thing"
    tree.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=tree) is None


def test_subdirectory_within_the_tree_still_resolves(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    subdir = tree / "src" / "shipit"
    subdir.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=subdir) == SESSION_ID


def test_a_deeper_uuid_bearing_dir_never_wins_over_the_tree_root(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    decoy = root / "shipit-old-nested-thing" / SESSION_LEAF
    decoy.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.current_session_id({}, cwd=decoy) is None


def test_broken_root_env_degrades_to_none_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/root")
    assert current.current_session_id({}, cwd=tmp_path) is None


def test_empty_export_falls_through_to_the_path(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env = {logcontext.ENV_PREFIX + "SESSION": ""}
    assert current.current_session_id(env, cwd=tree) == SESSION_ID


def test_containing_tree_returns_the_dir_for_a_conforming_leaf(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _flat_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.containing_tree(tree) == tree


def test_containing_tree_is_none_for_an_old_nested_path(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    nested = root / "acme" / "widget" / "epics" / "TRE03"
    nested.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.containing_tree(nested) is None


@pytest.mark.parametrize(
    "leaf",
    [
        f"shipit-claude-20261317-041649-{SESSION_ID}",
        "shipit-claude-20260703-041649-notauuid",
        f"shipit-Claude-20260703-041649-{SESSION_ID}",
    ],
)
def test_uuid_tailed_but_malformed_leaf_is_not_a_tree(tmp_path, monkeypatch, leaf):
    root = tmp_path / "trees"
    tree = root / leaf
    tree.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    assert current.containing_tree(tree) is None
    assert current.current_session_id({}, cwd=tree) is None
