from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from shipit import git
from shipit.identity import Sha
from shipit.tree import registry
from shipit.tree.cleanup import classify


def _make_clone(root: Path, rel: str) -> Path:
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _make_plain_dir(root: Path, rel: str) -> Path:
    path = root / rel
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch):
    root = tmp_path / "trees"
    a = _make_clone(root, "acme/widget/issues/123/work-aaaa")
    b = _make_clone(root, "acme/widget/issues/456/work-bbbb")
    _make_plain_dir(root, "acme/widget/issues/scratch-notatree")

    state = {
        str(a): {
            "branch": "issues/123/work",
            "base": "origin/main",
            "dirty": [" M file.py"],
            "ahead_behind": (2, 0),
            "unpushed_shas": (Sha("a" * 40), Sha("b" * 40)),
        },
        str(b): {
            "branch": "HAR02/WS02",
            "base": "origin/HAR02/umbrella",
            "dirty": [],
            "ahead_behind": (0, 3),
            "unpushed_shas": (),
        },
    }

    monkeypatch.setattr(git, "current_branch", lambda *, cwd: state[cwd]["branch"])
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: state[cwd]["base"])
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: state[cwd]["dirty"])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: state[cwd]["ahead_behind"])
    monkeypatch.setattr(
        git, "unpushed_shas", lambda *, cwd: state[cwd]["unpushed_shas"]
    )
    monkeypatch.setattr(git, "head_committed_at", lambda *, cwd: 1_000.0)
    return root, a, b


def test_scan_returns_one_record_per_clone_ignoring_non_trees(fleet):
    root, a, b = fleet
    records = registry.scan(root)

    assert [r.path for r in records] == sorted([str(a), str(b)])


def test_scan_reads_branch_base_dirty_and_ahead_behind(fleet):
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}

    ra = by_path[str(a)]
    assert ra.branch == "issues/123/work"
    assert ra.base == "origin/main"
    assert ra.dirty is True
    assert (ra.ahead, ra.behind) == (2, 0)

    rb = by_path[str(b)]
    assert rb.branch == "HAR02/WS02"
    assert rb.base == "origin/HAR02/umbrella"
    assert rb.dirty is False
    assert (rb.ahead, rb.behind) == (0, 3)


def test_scan_reads_the_upstream_independent_unpushed_shas(fleet):
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}
    assert by_path[str(a)].unpushed_shas == (Sha("a" * 40), Sha("b" * 40))
    assert by_path[str(a)].unpushed == 2
    assert by_path[str(b)].unpushed_shas == ()
    assert by_path[str(b)].unpushed == 0


def test_scan_branch_without_upstream_reads_none_base(tmp_path: Path, monkeypatch):
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-zzzz")
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: None)
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.base is None
    assert (record.ahead, record.behind) == (0, 0)


def test_scan_missing_root_yields_empty(tmp_path: Path):
    assert registry.scan(tmp_path / "does-not-exist") == []


def test_scan_does_not_descend_into_a_clone(tmp_path: Path, monkeypatch):
    root = tmp_path / "trees"
    outer = _make_clone(root, "acme/widget/issues/1/work-aaaa")
    (outer / "vendor" / "dep" / ".git").mkdir(parents=True)

    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))

    records = registry.scan(root)
    assert [r.path for r in records] == [str(outer)]


def _patch_trivial_git(monkeypatch, *, branch_hook=None):

    def _branch(*, cwd):
        if branch_hook is not None:
            branch_hook(cwd)
        return "issues/1/work"

    monkeypatch.setattr(git, "current_branch", _branch)
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: ())
    monkeypatch.setattr(git, "head_committed_at", lambda *, cwd: 1_000.0)


def test_scan_output_order_is_deterministic_regardless_of_completion_order(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "trees"
    rels = [
        "acme/widget/issues/1/work-aaaa",
        "acme/widget/issues/2/work-bbbb",
        "acme/widget/issues/3/work-cccc",
    ]
    clones = [_make_clone(root, rel) for rel in rels]
    want = sorted(str(c) for c in clones)

    order = {str(c): i for i, c in enumerate(reversed(clones))}
    barrier = threading.Barrier(len(clones), timeout=10)

    def _hook(cwd):
        barrier.wait()
        threading.Event().wait(0.01 * order[cwd])

    _patch_trivial_git(monkeypatch, branch_hook=_hook)
    monkeypatch.setattr(registry.os, "cpu_count", lambda: 8)

    for _ in range(5):
        records = registry.scan(root)
        assert [r.path for r in records] == want


def test_scan_reads_clones_concurrently_via_bounded_pool(tmp_path: Path, monkeypatch):
    root = tmp_path / "trees"
    n = 6
    for i in range(n):
        _make_clone(root, f"acme/widget/issues/{i}-cccc")

    monkeypatch.setattr(registry.os, "cpu_count", lambda: 8)
    assert registry._scan_workers(n) == n

    barrier = threading.Barrier(n, timeout=5)
    max_seen = 0
    live = 0
    lock = threading.Lock()

    def _hook(cwd):
        nonlocal max_seen, live
        with lock:
            live += 1
            max_seen = max(max_seen, live)
        barrier.wait()
        with lock:
            live -= 1

    _patch_trivial_git(monkeypatch, branch_hook=_hook)

    records = registry.scan(root)
    assert len(records) == n
    assert max_seen == n


def test_scan_workers_is_bounded_and_at_least_one():
    assert registry._scan_workers(1000) == registry._MAX_SCAN_WORKERS
    assert registry._scan_workers(3) == 3
    assert registry._scan_workers(0) == 1


def test_scan_workers_ignores_the_core_count(monkeypatch):
    for cores in (1, 64, None):
        monkeypatch.setattr(registry.os, "cpu_count", lambda cores=cores: cores)
        assert registry._scan_workers(100) == registry._MAX_SCAN_WORKERS
        assert registry._scan_workers(2) == 2


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _real_clone(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "."], cwd=path)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("one\n")
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-qm", "init"], cwd=path)


@pytest.fixture
def real_git(monkeypatch):
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)


def test_root_mtime_does_not_observe_work_but_newest_mtime_does(tmp_path, real_git):
    clone = tmp_path / "trees" / "acme/widget/issues/1/work-aaaa"
    _real_clone(clone)
    stale = time.time() - 10 * 86_400
    os.utime(clone, (stale, stale))
    mtime_before = clone.stat().st_mtime

    (clone / "src" / "a.py").write_text("two\n")
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-qm", "work"], cwd=clone)

    assert clone.stat().st_mtime == mtime_before
    assert time.time() - clone.stat().st_mtime > 9 * 86_400

    (record,) = registry.scan(tmp_path / "trees")
    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime < 60
    assert record.last_commit is not None


def test_last_commit_is_committer_time_so_a_rebase_refreshes_it(tmp_path, real_git):
    clone = tmp_path / "trees" / "acme/widget/issues/2/work-bbbb"
    _real_clone(clone)
    old = "2020-01-01T00:00:00"
    (clone / "src" / "a.py").write_text("three\n")
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-qm", "replayed", "--date", old], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    author_at = float(_git(["log", "-1", "--format=%at"], cwd=clone).stdout.strip())
    assert time.time() - author_at > 365 * 86_400
    assert record.last_commit is not None
    assert time.time() - record.last_commit < 60


def test_a_deletion_only_commit_is_activity_the_walk_alone_cannot_see(
    tmp_path, real_git
):
    clone = tmp_path / "trees" / "acme/widget/issues/3/work-cccc"
    _real_clone(clone)
    (clone / "src" / "doomed.py").write_text("delete me\n")
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-qm", "add"], cwd=clone)

    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(origin)], cwd=clone)
    _git(["push", "-q", "origin", "HEAD"], cwd=clone)

    stale = time.time() - 10 * 86_400
    for path in clone.rglob("*"):
        if path.is_file():
            os.utime(path, (stale, stale))
    os.utime(clone, (stale, stale))

    _git(["rm", "-q", "src/doomed.py"], cwd=clone)
    _git(["commit", "-qm", "drop the dead module"], cwd=clone)
    _git(["push", "-q", "origin", "HEAD"], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    assert record.dirty is False
    assert record.unpushed_shas == ()
    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime > 9 * 86_400
    assert record.last_commit is not None
    assert time.time() - record.last_commit < 60

    decision = classify([record], time.time())
    assert decision.removable == []
    assert [r.path for r in decision.keep] == [record.path]


def test_unreadable_last_commit_reads_as_none(tmp_path, real_git):
    clone = tmp_path / "trees" / "acme/widget/issues/3/work-cccc"
    clone.mkdir(parents=True)
    _git(["init", "-q", "."], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    assert record.last_commit is None


def test_scan_makes_no_network_call(fleet, monkeypatch):
    from shipit import gh

    def _explode(*_a, **_k):
        raise AssertionError("scan made a network (gh) call")

    for name in dir(gh):
        obj = getattr(gh, name)
        if callable(obj) and not isinstance(obj, type) and not name.startswith("__"):
            monkeypatch.setattr(gh, name, _explode, raising=False)

    root, a, b = fleet
    records = registry.scan(root)
    assert {r.path for r in records} == {str(a), str(b)}


def test_scan_reads_newest_mtime_from_an_uncommitted_edit(tmp_path, real_git):
    clone = tmp_path / "trees" / "acme/widget/ephemeral/sess-aaaa"
    _real_clone(clone)
    stale = time.time() - 10 * 86_400
    os.utime(clone, (stale, stale))
    for path in (clone / "src" / "a.py", clone / "src"):
        os.utime(path, (stale, stale))

    (clone / "src" / "scratch.log").write_text("provisioning bucket ...\n")

    (record,) = registry.scan(tmp_path / "trees")

    assert time.time() - record.mtime > 9 * 86_400
    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime < 60


def test_scan_prunes_the_env_dirs_from_the_activity_signal(tmp_path, real_git):
    clone = tmp_path / "trees" / "acme/widget/issues/9/work-cccc"
    _real_clone(clone)
    stale = time.time() - 10 * 86_400
    for path in (clone / "src" / "a.py", clone / "src", clone):
        os.utime(path, (stale, stale))
    (clone / ".pixi" / "envs" / "default").mkdir(parents=True)
    (clone / ".pixi" / "envs" / "default" / "lib.so").write_text("fresh env solve")

    (record,) = registry.scan(tmp_path / "trees")

    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime > 9 * 86_400
