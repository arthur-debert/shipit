"""``hook worktreeremove`` — the ephemeral fast-path teardown boundary (ADR-0027).

The contract under test: on a clean session exit the hook removes the ephemeral
session Tree AND its liveness pidfile, but ONLY behind the same never-lose-work
floor the gc ladder enforces — a dirty Tree or one with commits on no remote is
never auto-removed; a non-ephemeral or out-of-root path is never touched; and the
whole boundary fails OPEN (exit 0, nothing removed) on any error, because the gc
ladder — not this hook, which does not even fire headless — is the load-bearing
cleanup.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from shipit import gh
from shipit.session import liveness
from shipit.tree import layout
from shipit.verbs.hook import worktreeremove

SESSION_RECORD = liveness.LivenessRecord(
    pid=100, session_id="sess-abc", create_time=1_750_000_000.0
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A central root the hook's under-the-root gate resolves against."""
    trees = tmp_path / "trees"
    trees.mkdir()
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(trees))
    return trees


@pytest.fixture
def ephemeral_tree(root):
    """A clean ephemeral session Tree (a .git-dir clone) with a pidfile."""
    tree = root / "acme" / "widget" / "ephemeral" / "sess-1"
    (tree / ".git").mkdir(parents=True)
    liveness.write_pidfile(tree, SESSION_RECORD)
    return tree


@pytest.fixture
def clean_git(monkeypatch):
    """A gh boundary reporting a clean, fully-pushed clone."""
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_unpushed_count", lambda *, cwd: 0)


def _run(payload) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return worktreeremove.run(stdin=io.StringIO(text))


def test_clean_ephemeral_tree_and_pidfile_are_removed(ephemeral_tree, clean_git):
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()  # pidfile lives in .git — gone with the Tree


@pytest.mark.parametrize("field", ["path", "worktree_path", "cwd"])
def test_any_plausible_payload_path_field_is_honored(ephemeral_tree, clean_git, field):
    # The WorktreeRemove payload contract is not spike-pinned yet; whichever field
    # carries the path, the gates (ephemeral + under-root + clone) decide safety.
    assert _run({field: str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()


def test_dirty_tree_is_never_auto_removed(ephemeral_tree, monkeypatch):
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: " M f.py\n")
    monkeypatch.setattr(gh, "git_unpushed_count", lambda *, cwd: 0)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()
    # The pidfile stays too: on a refusal the hook touches NOTHING.
    assert liveness.read_pidfile(ephemeral_tree) == SESSION_RECORD


def test_unpushed_tree_is_never_auto_removed(ephemeral_tree, monkeypatch):
    # Commits on NO remote (the upstream-independent count): the never-lose-work
    # floor holds on the fast path exactly as in the gc ladder.
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_unpushed_count", lambda *, cwd: 2)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_unreadable_unpushed_count_blocks_removal(ephemeral_tree, monkeypatch):
    # Unknown must never read as "nothing to lose".
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_unpushed_count", lambda *, cwd: None)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_non_ephemeral_tree_is_never_touched(root, clean_git):
    # A write Tree fires the same event when a helper spawn ends; its reclaim
    # belongs to the standard gc ladder, not the ephemeral fast path.
    write_tree = root / "acme" / "widget" / "branches" / "feat-x-deadbeef"
    (write_tree / ".git").mkdir(parents=True)
    assert _run({"cwd": str(write_tree)}) == 0
    assert write_tree.exists()


def test_path_outside_the_central_root_is_never_touched(tmp_path, root, clean_git):
    # An `ephemeral`-shaped path OUTSIDE the root (hostile or confused payload)
    # fails the under-root gate.
    outside = tmp_path / "elsewhere" / "ephemeral" / "sess-1"
    (outside / ".git").mkdir(parents=True)
    assert _run({"cwd": str(outside)}) == 0
    assert outside.exists()


def test_non_clone_dir_is_never_touched(root, clean_git):
    not_a_clone = root / "acme" / "widget" / "ephemeral" / "sess-1"
    not_a_clone.mkdir(parents=True)  # no .git dir
    assert _run({"cwd": str(not_a_clone)}) == 0
    assert not_a_clone.exists()


def test_bad_payload_fails_open(root):
    assert _run("{not json") == 0
    assert _run(json.dumps(["not", "an", "object"])) == 0
    assert _run({}) == 0


def test_git_read_failure_fails_open(ephemeral_tree, monkeypatch):
    # An unreadable dirty state must refuse the removal, never crash the exit.
    def boom(*, cwd):
        raise gh.GhError("git went away")

    monkeypatch.setattr(gh, "git_status_porcelain", boom)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_misconfigured_central_root_fails_open(ephemeral_tree, clean_git, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/root")
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_first_valid_candidate_field_wins(root, ephemeral_tree, clean_git):
    # `path` is tried before `cwd`: with both present, the valid `path` target is
    # removed and the (non-ephemeral) cwd is untouched.
    other = root / "acme" / "widget" / "branches" / "x-aa"
    (other / ".git").mkdir(parents=True)
    assert _run({"path": str(ephemeral_tree), "cwd": str(other)}) == 0
    assert not ephemeral_tree.exists()
    assert other.exists()


def test_cli_command_is_registered():
    from shipit.verbs.hook import hook

    assert "worktreeremove" in hook.commands
    assert isinstance(worktreeremove.cmd.name, str)


def test_repo_settings_wire_the_hook():
    # The committed hook line (ADR-0012: thin wiring, logic in the package).
    settings = json.loads(
        (Path(__file__).parent.parent / ".claude" / "settings.json").read_text()
    )
    events = settings["hooks"]["WorktreeRemove"]
    commands = [h["command"] for entry in events for h in entry["hooks"]]
    assert "pixi run shipit hook worktreeremove" in commands
