"""Tests for ``tree.activity.newest_mtime`` — the measured reclaim signal (ADR-0072).

These drive a REAL directory tree on disk (the module's whole job is to ask the
filesystem), and assert external behaviour: the newest file mtime, the prune set that
makes the walk both cheap and truthful, and — the load-bearing half — that every
unreadable answer is ``None`` rather than a number, because ``None`` is what keeps a
Tree and a number is what deletes one.
"""

from __future__ import annotations

import os

import pytest

from shipit.tree.activity import PRUNE_DIRS, newest_mtime

OLD = 1_000_000.0
NEW = 2_000_000.0


def _write(path, *, mtime: float, content: str = "x") -> None:
    """Write a file with an exact mtime, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.utime(path, (mtime, mtime))


def test_reports_the_newest_file_mtime(tmp_path):
    _write(tmp_path / "old.txt", mtime=OLD)
    _write(tmp_path / "new.txt", mtime=NEW)
    assert newest_mtime(tmp_path) == NEW


def test_sees_a_file_written_under_a_subdirectory(tmp_path):
    # The 10h-lag bug (#1018) at its source: an agent edits under `src/`, which leaves
    # the clone ROOT's mtime untouched. The walk sees the write wherever it lands —
    # this is the whole reason the signal exists.
    _write(tmp_path / "README.md", mtime=OLD)
    _write(tmp_path / "src" / "deep" / "nested" / "mod.py", mtime=NEW)
    os.utime(tmp_path, (OLD, OLD))  # root dir stat is stale, as in the real bug

    assert tmp_path.stat().st_mtime == OLD
    assert newest_mtime(tmp_path) == NEW


@pytest.mark.parametrize("pruned", sorted(PRUNE_DIRS))
def test_activity_inside_a_pruned_dir_is_not_activity(tmp_path, pruned):
    # The prune set is part of the DECISION, not a speed knob: an env solve, a build or
    # a fetch writes thousands of files that say nothing about whether anyone is working
    # in this Tree. A fresh file under any pruned dir must not refresh the signal.
    _write(tmp_path / "src" / "mod.py", mtime=OLD)
    _write(tmp_path / pruned / "junk", mtime=NEW)
    assert newest_mtime(tmp_path) == OLD


def test_pruned_dirs_are_not_descended_into(tmp_path):
    # Pruning is by NAME at any depth, and it stops the descent (the 100× cost gap:
    # ~1.9ms pruned vs ~191.7ms naive, `.pixi` alone being ~97% of the file count).
    _write(tmp_path / "src" / "mod.py", mtime=OLD)
    _write(tmp_path / ".pixi" / "envs" / "default" / "lib" / "thing.so", mtime=NEW)
    _write(tmp_path / "src" / "__pycache__" / "mod.cpython-312.pyc", mtime=NEW)
    assert newest_mtime(tmp_path) == OLD


def test_a_file_named_like_a_pruned_dir_still_counts(tmp_path):
    # The prune set names DIRECTORIES. A file that happens to share the name is a file.
    _write(tmp_path / "src" / "mod.py", mtime=OLD)
    _write(tmp_path / "build", mtime=NEW)
    assert newest_mtime(tmp_path) == NEW


def test_a_symlink_reports_its_own_stamp_not_its_targets(tmp_path):
    # lstat, never stat: a link out of the Tree must not import foreign activity, and a
    # BROKEN link must not raise (which would blank the whole signal to None).
    _write(tmp_path / "real.txt", mtime=OLD)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "does-not-exist")
    os.utime(link, (OLD, OLD), follow_symlinks=False)

    assert newest_mtime(tmp_path) == OLD


# --- unreadable is NOT idle (ADR-0072) ---------------------------------------------
#
# Every arm below returns None, which every caller reads as KEEP. A wrongly-kept Tree
# costs disk until the next sweep; a wrongly-deleted one costs work that no longer
# exists.


def test_a_tree_with_no_eligible_file_is_unreadable_not_idle(tmp_path):
    # Nothing but pruned content: the walk completes having measured nothing. That is
    # "I could not tell", not "idle since the epoch" — and certainly not a delete.
    _write(tmp_path / ".pixi" / "envs" / "thing", mtime=NEW)
    assert newest_mtime(tmp_path) is None


def test_an_empty_dir_is_unreadable_not_idle(tmp_path):
    assert newest_mtime(tmp_path) is None


def test_a_missing_path_is_unreadable(tmp_path):
    assert newest_mtime(tmp_path / "gone") is None


def test_a_file_instead_of_a_dir_is_unreadable(tmp_path):
    target = tmp_path / "file.txt"
    _write(target, mtime=NEW)
    assert newest_mtime(target) is None


def test_a_stat_failure_blanks_the_signal_rather_than_reporting_a_partial_max(
    tmp_path, monkeypatch
):
    # A file vanishing mid-walk (a concurrent gc, a build cleaning up) must not yield
    # the max of whatever was read BEFORE the failure: a partial maximum is a number,
    # and a number can license a delete. The whole answer becomes None.
    _write(tmp_path / "recent.txt", mtime=NEW)
    _write(tmp_path / "old.txt", mtime=OLD)

    def _boom(path, *args, **kwargs):
        raise OSError("vanished mid-walk")

    monkeypatch.setattr(os, "lstat", _boom)
    assert newest_mtime(tmp_path) is None


def test_an_unreadable_directory_blanks_the_signal(tmp_path, monkeypatch):
    _write(tmp_path / "src" / "mod.py", mtime=NEW)

    def _boom(*args, **kwargs):
        raise PermissionError("cannot read")

    monkeypatch.setattr(os, "walk", _boom)
    assert newest_mtime(tmp_path) is None
