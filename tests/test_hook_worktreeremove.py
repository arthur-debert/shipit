from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import managed_cc_hook_command

from shipit import git
from shipit.execrun import ExecError
from shipit.identity import Sha
from shipit.tree import layout
from shipit.verbs.hook import worktreeremove


@pytest.fixture
def root(tmp_path, monkeypatch):
    trees = tmp_path / "trees"
    trees.mkdir()
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(trees))
    return trees


FLAT_LEAF = "widget-claude-20260717-081333-619cf51a-f501-44dc-992f-74df773204aa"


@pytest.fixture
def ephemeral_tree(root):
    tree = root / FLAT_LEAF
    (tree / ".git").mkdir(parents=True)
    return tree


@pytest.fixture
def clean_git(monkeypatch):
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: ())
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))


def _run(payload) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return worktreeremove.run(stdin=io.StringIO(text))


def test_clean_ephemeral_tree_is_removed(ephemeral_tree, clean_git):
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()


@pytest.mark.parametrize("field", ["path", "worktree_path", "cwd"])
def test_any_plausible_payload_path_field_is_honored(ephemeral_tree, clean_git, field):
    assert _run({field: str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()


def test_dirty_tree_is_never_auto_removed(ephemeral_tree, monkeypatch):
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [" M f.py"])
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: ())
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_unpushed_tree_is_never_auto_removed(ephemeral_tree, monkeypatch):
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(
        git, "unpushed_shas", lambda *, cwd: (Sha("a" * 40), Sha("b" * 40))
    )
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_unreadable_unpushed_list_blocks_removal(ephemeral_tree, monkeypatch):
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: None)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_ahead_of_upstream_alone_no_longer_blocks(
    ephemeral_tree, clean_git, monkeypatch
):
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (2, 0))
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()


def test_flat_tree_has_no_kind_carveout_and_is_reclaimed(root, clean_git):
    other_leaf = (
        root / "widget-codex-20260101-000000-11111111-2222-4333-8444-555555555555"
    )
    (other_leaf / ".git").mkdir(parents=True)
    assert _run({"cwd": str(other_leaf)}) == 0
    assert not other_leaf.exists()


def test_the_central_root_itself_is_never_targeted(root, clean_git):
    (root / ".git").mkdir(parents=True)
    assert _run({"cwd": str(root)}) == 0
    assert root.exists() and (root / ".git").is_dir()


def test_path_outside_the_central_root_is_never_touched(tmp_path, root, clean_git):
    outside = tmp_path / "elsewhere" / FLAT_LEAF
    (outside / ".git").mkdir(parents=True)
    assert _run({"cwd": str(outside)}) == 0
    assert outside.exists()


def test_non_clone_dir_is_never_touched(root, clean_git):
    not_a_clone = root / FLAT_LEAF
    not_a_clone.mkdir(parents=True)
    assert _run({"cwd": str(not_a_clone)}) == 0
    assert not_a_clone.exists()


def test_nested_clone_under_a_tree_is_never_targeted(root, ephemeral_tree, clean_git):
    nested = ephemeral_tree / "submodule"
    (nested / ".git").mkdir(parents=True)
    assert _run({"cwd": str(nested)}) == 0
    assert nested.exists()
    assert ephemeral_tree.exists()


def test_non_conforming_direct_child_is_never_targeted(root, clean_git):
    stray = root / "just-some-repo"
    (stray / ".git").mkdir(parents=True)
    assert layout.parse_flat_leaf(stray.name) is None
    assert _run({"cwd": str(stray)}) == 0
    assert stray.exists()


def test_bad_payload_fails_open(root):
    assert _run("{not json") == 0
    assert _run(json.dumps(["not", "an", "object"])) == 0
    assert _run({}) == 0


def test_git_read_failure_fails_open(ephemeral_tree, monkeypatch):
    def boom(*, cwd):
        raise ExecError(["gh"], rc=1, stderr="git went away")

    monkeypatch.setattr(git, "status_porcelain", boom)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_misconfigured_central_root_fails_open(ephemeral_tree, clean_git, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/root")
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_first_valid_candidate_field_wins(root, ephemeral_tree, clean_git):
    other = root / "widget-codex-20260101-000000-99999999-8888-4777-8666-555555555555"
    (other / ".git").mkdir(parents=True)
    assert _run({"path": str(ephemeral_tree), "cwd": str(other)}) == 0
    assert not ephemeral_tree.exists()
    assert other.exists()


def test_cli_command_is_registered():
    from shipit.verbs.hook import hook

    assert "worktreeremove" in hook.commands
    assert isinstance(worktreeremove.cmd.name, str)


def test_repo_settings_wire_the_hook():
    settings = json.loads(
        (Path(__file__).parent.parent / ".claude" / "settings.json").read_text()
    )
    events = settings["hooks"]["WorktreeRemove"]
    commands = [h["command"] for entry in events for h in entry["hooks"]]
    assert managed_cc_hook_command("worktreeremove") in commands
    assert all("pixi run" not in c for c in commands)
