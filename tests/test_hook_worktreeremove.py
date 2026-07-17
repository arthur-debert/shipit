"""``hook worktreeremove`` — the ephemeral fast-path teardown boundary (ADR-0027).

The contract under test: on a clean session exit the hook removes the ephemeral
session Tree, but ONLY behind the same never-lose-work floor the gc rule enforces
(ADR-0072) — a dirty Tree or one with commits on no remote is never auto-removed; a
non-ephemeral or out-of-root path is never touched; and the whole boundary fails OPEN
(exit 0, nothing removed) on any error, because the gc rule — not this hook, which
does not even fire headless — is the load-bearing cleanup.
"""

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
    """A central root the hook's under-the-root gate resolves against."""
    trees = tmp_path / "trees"
    trees.mkdir()
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(trees))
    return trees


#: A flat, self-describing session Tree leaf (ADR-0074): <repo>-<agent>-<timestamp>-<id>,
#: one segment below the central root, no kind segment.
FLAT_LEAF = "widget-claude-20260717-081333-619cf51a-f501-44dc-992f-74df773204aa"


@pytest.fixture
def ephemeral_tree(root):
    """A clean coordinator session Tree — a flat, self-describing .git-dir clone.

    The dir is the single flat leaf `<repo>-<agent>-<timestamp>-<id>` (ADR-0074) with
    no kind segment; the BRANCH is still `ephemeral/<id>`, but the hook no longer reads
    the path for a kind — the `.git` dir marks it a real clone the gates accept.
    """
    tree = root / FLAT_LEAF
    (tree / ".git").mkdir(parents=True)
    return tree


@pytest.fixture
def clean_git(monkeypatch):
    """A git boundary reporting a clean, fully-pushed, upstream-level clone."""
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
    # The WorktreeRemove payload contract is not spike-pinned yet; whichever field
    # carries the path, the gates (ephemeral + under-root + clone) decide safety.
    assert _run({field: str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()


def test_dirty_tree_is_never_auto_removed(ephemeral_tree, monkeypatch):
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [" M f.py"])
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: ())
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()  # on a refusal the hook touches NOTHING


def test_unpushed_tree_is_never_auto_removed(ephemeral_tree, monkeypatch):
    # Commits on NO remote (the upstream-independent count): the never-lose-work
    # floor holds on the fast path exactly as in the gc ladder.
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(
        git, "unpushed_shas", lambda *, cwd: (Sha("a" * 40), Sha("b" * 40))
    )
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_unreadable_unpushed_list_blocks_removal(ephemeral_tree, monkeypatch):
    # Unknown must never read as "nothing to lose": an unreadable local-only list keeps.
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: None)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_ahead_of_upstream_alone_no_longer_blocks(
    ephemeral_tree, clean_git, monkeypatch
):
    # ADR-0072/WS03: the fast path now mirrors gc's floor EXACTLY — dirty or unpushed
    # (commits on no remote) only. A clean, fully-pushed Tree that merely sits ahead of
    # its configured upstream (its commits pushed to some other branch, so recoverable)
    # is reclaimed, just as gc would. The old `ahead`-count block — a companion to the
    # retired provisioning carve-out — is gone.
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (2, 0))
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert not ephemeral_tree.exists()


def test_flat_tree_has_no_kind_carveout_and_is_reclaimed(root, clean_git):
    # ADR-0074 retired the kind segment, so the fast path no longer carves out a
    # "non-ephemeral" Tree by parsing its path: this event fires only for the worktree
    # the session itself adopted, and ANY clean clone under the root — whatever its flat
    # leaf — is reclaimed behind the never-lose-work floor. A re-added kind gate (only
    # removing `ephemeral`-named leaves) would wrongly KEEP this differently-named leaf,
    # so pin that it IS removed.
    other_leaf = (
        root / "widget-codex-20260101-000000-11111111-2222-4333-8444-555555555555"
    )
    (other_leaf / ".git").mkdir(parents=True)
    assert _run({"cwd": str(other_leaf)}) == 0
    assert not other_leaf.exists()


def test_path_outside_the_central_root_is_never_touched(tmp_path, root, clean_git):
    # A Tree-shaped path OUTSIDE the root (hostile or confused payload) fails the
    # under-root gate — the flat leaf shape does not matter, only that it is under root.
    outside = tmp_path / "elsewhere" / FLAT_LEAF
    (outside / ".git").mkdir(parents=True)
    assert _run({"cwd": str(outside)}) == 0
    assert outside.exists()


def test_non_clone_dir_is_never_touched(root, clean_git):
    not_a_clone = root / FLAT_LEAF
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
        raise ExecError(["gh"], rc=1, stderr="git went away")

    monkeypatch.setattr(git, "status_porcelain", boom)
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_misconfigured_central_root_fails_open(ephemeral_tree, clean_git, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/root")
    assert _run({"cwd": str(ephemeral_tree)}) == 0
    assert ephemeral_tree.exists()


def test_first_valid_candidate_field_wins(root, ephemeral_tree, clean_git):
    # `path` is tried before `cwd`: with both present, the valid `path` target is
    # removed and `cwd` is never evaluated — so its Tree is untouched even though it,
    # too, is a clean flat clone under the root (order decides, not kind).
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
    # The committed hook line (ADR-0012: thin wiring, logic in the package).
    settings = json.loads(
        (Path(__file__).parent.parent / ".claude" / "settings.json").read_text()
    )
    events = settings["hooks"]["WorktreeRemove"]
    commands = [h["command"] for entry in events for h in entry["hooks"]]
    # Rides the PINNED launcher `./bin/shipit` DIRECTLY (#481/#491, ADR-0033), not
    # a bare PATH `shipit` and no `pixi run` wrap (the launcher is pixi-independent).
    # The command `cd`s into `$CLAUDE_PROJECT_DIR` first so the relative launcher
    # resolves even when the hook runs from a foreign CWD, then a launcher-presence
    # guard fails open when the launcher is absent (#491).
    assert managed_cc_hook_command("worktreeremove") in commands
    assert all("pixi run" not in c for c in commands)
