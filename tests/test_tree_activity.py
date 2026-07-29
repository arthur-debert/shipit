from __future__ import annotations

import os

import pytest

from shipit.tree.activity import PRUNE_DIRS, newest_mtime

OLD = 1_000_000.0
NEW = 2_000_000.0


def _write(path, *, mtime: float, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.utime(path, (mtime, mtime))


def test_reports_the_newest_file_mtime(tmp_path):
    _write(tmp_path / "old.txt", mtime=OLD)
    _write(tmp_path / "new.txt", mtime=NEW)
    assert newest_mtime(tmp_path) == NEW


def test_sees_a_file_written_under_a_subdirectory(tmp_path):
    _write(tmp_path / "README.md", mtime=OLD)
    _write(tmp_path / "src" / "deep" / "nested" / "mod.py", mtime=NEW)
    os.utime(tmp_path, (OLD, OLD))

    assert tmp_path.stat().st_mtime == OLD
    assert newest_mtime(tmp_path) == NEW


@pytest.mark.parametrize("pruned", sorted(PRUNE_DIRS))
def test_activity_inside_a_pruned_dir_is_not_activity(tmp_path, pruned):
    _write(tmp_path / "src" / "mod.py", mtime=OLD)
    _write(tmp_path / pruned / "junk", mtime=NEW)
    assert newest_mtime(tmp_path) == OLD


def test_pruned_dirs_are_not_descended_into(tmp_path):
    _write(tmp_path / "src" / "mod.py", mtime=OLD)
    _write(tmp_path / ".pixi" / "envs" / "default" / "lib" / "thing.so", mtime=NEW)
    _write(tmp_path / "src" / "__pycache__" / "mod.cpython-312.pyc", mtime=NEW)
    assert newest_mtime(tmp_path) == OLD


def test_a_file_named_like_a_pruned_dir_still_counts(tmp_path):
    _write(tmp_path / "src" / "mod.py", mtime=OLD)
    _write(tmp_path / "build", mtime=NEW)
    assert newest_mtime(tmp_path) == NEW


def test_a_symlink_reports_its_own_stamp_not_its_targets(tmp_path):
    _write(tmp_path / "real.txt", mtime=OLD)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "does-not-exist")
    os.utime(link, (OLD, OLD), follow_symlinks=False)

    assert newest_mtime(tmp_path) == OLD


def test_a_tree_with_no_eligible_file_is_unreadable_not_idle(tmp_path):
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


@pytest.mark.skipif(
    not hasattr(os, "getuid") or os.getuid() == 0,
    reason="needs POSIX non-root: os.getuid is absent on Windows; root reads unreadable dirs anyway",
)
def test_a_really_unreadable_subdir_blanks_the_signal_rather_than_reporting_the_rest(
    tmp_path,
):
    _write(tmp_path / "old.txt", mtime=OLD)
    _write(tmp_path / "secret" / "recent.txt", mtime=NEW)
    (tmp_path / "secret").chmod(0o000)
    try:
        assert newest_mtime(tmp_path) is None
    finally:
        (tmp_path / "secret").chmod(0o755)


@pytest.mark.skipif(
    not hasattr(os, "getuid") or os.getuid() == 0,
    reason="needs POSIX non-root: os.getuid is absent on Windows; root reads unreadable dirs anyway",
)
def test_an_unreadable_dir_inside_a_pruned_dir_does_not_blank_the_signal(tmp_path):
    _write(tmp_path / "src" / "mod.py", mtime=NEW)
    (tmp_path / ".pixi" / "envs").mkdir(parents=True)
    (tmp_path / ".pixi" / "envs").chmod(0o000)
    try:
        assert newest_mtime(tmp_path) == NEW
    finally:
        (tmp_path / ".pixi" / "envs").chmod(0o755)
