"""Unit tests for ``tree.registry.scan`` — deriving the fleet from on-disk clones.

These assert EXTERNAL behavior (PRD Testing Decisions): given a fixture directory
layout under a central root, ``scan`` returns the right :class:`TreeRecord`s
(branch, base, dirty, ahead/behind, PR label) and IGNORES any directory that is not
itself a git clone. The ``gh`` boundary (branch/dirty/ahead-behind/PR reads) is
patched so no real git/gh runs — the fixture is the disk layout, the patch is the
per-clone state.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from shipit import gh, git
from shipit.identity import Sha
from shipit.tree import registry


def _make_clone(root: Path, rel: str) -> Path:
    """Create ``root/<rel>`` as a fake clone (a dir carrying a ``.git`` marker)."""
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _make_plain_dir(root: Path, rel: str) -> Path:
    """Create ``root/<rel>`` as a NON-Tree directory (no ``.git`` marker)."""
    path = root / rel
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch):
    """A central root with two Tree clones and a non-Tree dir, with the gh boundary
    patched to report per-clone state keyed by directory path."""
    root = tmp_path / "trees"
    a = _make_clone(root, "acme/widget/issues/123/work-aaaa")
    b = _make_clone(root, "acme/widget/issues/456/work-bbbb")
    # A stray directory that is NOT a clone — must be ignored by scan.
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
    pr_by_branch = {
        "issues/123/work": gh.HeadPr(
            number=7, state="OPEN", is_draft=True, base_ref="main"
        ),
        "HAR02/WS02": gh.HeadPr(
            number=9, state="MERGED", is_draft=False, base_ref="HAR02/umbrella"
        ),
    }

    monkeypatch.setattr(git, "current_branch", lambda *, cwd: state[cwd]["branch"])
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: state[cwd]["base"])
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: state[cwd]["dirty"])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: state[cwd]["ahead_behind"])
    monkeypatch.setattr(
        git, "unpushed_shas", lambda *, cwd: state[cwd]["unpushed_shas"]
    )
    monkeypatch.setattr(git, "head_committed_at", lambda *, cwd: 1_000.0)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )
    return root, a, b


def test_scan_returns_one_record_per_clone_ignoring_non_trees(fleet):
    root, a, b = fleet
    records = registry.scan(root)

    # Exactly the two clones — the stray non-Tree dir is ignored.
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
    # `unpushed_shas` is the ephemeral gc ladder's never-lose-work signal: the
    # commits on NO remote at all, read independently of any upstream, by identity
    # (so the ladder can exclude exactly the recorded provisioning commit, #232).
    # The `unpushed` count is derived from the same stored fact.
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}
    assert by_path[str(a)].unpushed_shas == (Sha("a" * 40), Sha("b" * 40))
    assert by_path[str(a)].unpushed == 2
    assert by_path[str(b)].unpushed_shas == ()
    assert by_path[str(b)].unpushed == 0


def test_scan_renders_pr_state_label_with_draft(fleet):
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}

    # Draft open PR reads as DRAFT; a merged PR shows its GitHub state verbatim.
    assert by_path[str(a)].pr == "#7 DRAFT"
    assert by_path[str(b)].pr == "#9 MERGED"


def test_scan_branch_without_pr_has_none(tmp_path: Path, monkeypatch):
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-zzzz")
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: None)
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.pr is None
    assert record.base is None
    assert (record.ahead, record.behind) == (0, 0)


def test_scan_unreadable_pr_renders_unknown_label(tmp_path: Path, monkeypatch):
    # An unreadable PR state (gh.pr_for_head -> UNKNOWN) renders as a bare "UNKNOWN"
    # label, distinct from the None a genuinely-PR-less Tree shows.
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-zzzz")
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: gh.UNKNOWN)

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.pr == "UNKNOWN"


def test_scan_missing_root_yields_empty(tmp_path: Path):
    assert registry.scan(tmp_path / "does-not-exist") == []


def test_scan_does_not_descend_into_a_clone(tmp_path: Path, monkeypatch):
    # A path that looks like a clone nested INSIDE another clone must not be
    # reported as a second Tree — scan stops at the first .git it meets.
    root = tmp_path / "trees"
    outer = _make_clone(root, "acme/widget/issues/1/work-aaaa")
    (outer / "vendor" / "dep" / ".git").mkdir(parents=True)

    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    records = registry.scan(root)
    assert [r.path for r in records] == [str(outer)]


def _patch_trivial_gh(monkeypatch, *, branch_hook=None):
    """Patch every gh read with cheap stubs; ``branch_hook(cwd)`` runs first if given."""

    def _branch(*, cwd):
        if branch_hook is not None:
            branch_hook(cwd)
        return "issues/1/work"

    monkeypatch.setattr(git, "current_branch", _branch)
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)


def test_scan_output_order_is_deterministic_regardless_of_completion_order(
    tmp_path: Path, monkeypatch
):
    """Records come back sorted by path even when the per-clone reads finish in the
    opposite order — the post-gather sort, not completion order, fixes the listing."""
    root = tmp_path / "trees"
    rels = [
        "acme/widget/issues/1/work-aaaa",
        "acme/widget/issues/2/work-bbbb",
        "acme/widget/issues/3/work-cccc",
    ]
    clones = [_make_clone(root, rel) for rel in rels]
    want = sorted(str(c) for c in clones)

    # Make the lexicographically-LAST clone finish first and the first finish last, so
    # completion order is the reverse of path order. A barrier guarantees all tasks are
    # in flight (real concurrency) before any returns, so the delays actually reorder
    # completion rather than serializing.
    order = {str(c): i for i, c in enumerate(reversed(clones))}
    barrier = threading.Barrier(len(clones), timeout=10)

    def _hook(cwd):
        barrier.wait()
        # Stagger returns so completion order is reverse of path order.
        threading.Event().wait(0.01 * order[cwd])

    _patch_trivial_gh(monkeypatch, branch_hook=_hook)
    monkeypatch.setattr(registry.os, "cpu_count", lambda: 8)

    # Run repeatedly: a determinism claim must hold across runs, not by luck once.
    for _ in range(5):
        records = registry.scan(root)
        assert [r.path for r in records] == want


def test_scan_reads_clones_concurrently_via_bounded_pool(tmp_path: Path, monkeypatch):
    """The per-clone reads run on a bounded worker pool: with N clones and N workers all
    tasks are in flight at once. A barrier proves it — if reads were sequential the first
    task would block forever waiting for siblings that never start, and we'd time out."""
    root = tmp_path / "trees"
    n = 6
    for i in range(n):
        _make_clone(root, f"acme/widget/issues/{i}-cccc")

    # Pin cpu_count so the bounded pool definitely has a worker per clone for this test.
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
        # All N tasks must reach here together, or this raises BrokenBarrierError.
        barrier.wait()
        with lock:
            live -= 1

    _patch_trivial_gh(monkeypatch, branch_hook=_hook)

    records = registry.scan(root)
    assert len(records) == n
    # All N ran simultaneously — concrete proof of the bounded fan-out.
    assert max_seen == n


def test_scan_workers_is_bounded_and_at_least_one(monkeypatch):
    """The worker count is capped (never an unbounded thread-per-clone) and never < 1."""
    monkeypatch.setattr(registry.os, "cpu_count", lambda: 64)
    # Capped by _MAX_SCAN_WORKERS even with many CPUs and many clones.
    assert registry._scan_workers(1000) == registry._MAX_SCAN_WORKERS
    # Never exceeds the number of clones, and never drops below one.
    assert registry._scan_workers(3) == 3
    assert registry._scan_workers(0) == 1


def test_scan_workers_keeps_floor_on_low_core_box(monkeypatch):
    """The fan-out does NOT collapse to the core count: the reads are I/O-bound, so a
    1-core box must still overlap several subprocesses (up to the clone count)."""
    monkeypatch.setattr(registry.os, "cpu_count", lambda: 1)
    # Many clones on a 1-core box still get the I/O floor, not a single serial worker.
    assert registry._scan_workers(100) == registry._MIN_SCAN_WORKERS
    # Still bounded by the clone count below the floor.
    assert registry._scan_workers(2) == 2
    # An unknown core count behaves like the floor, not like a single worker.
    monkeypatch.setattr(registry.os, "cpu_count", lambda: None)
    assert registry._scan_workers(100) == registry._MIN_SCAN_WORKERS


# --- the write ladder's activity signal, over REAL git (#1009, codex review) ---------
#
# The rest of this module patches the git boundary; these two do NOT. The whole point
# of the `last_commit` signal is an empirical claim about the filesystem — that root
# mtime does not observe an agent working, and that a commit timestamp does — and a
# test built on injected values cannot check that claim. So these drive real `git`
# against a real clone and read the record `scan` builds from it.


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    """Run a real ``git`` in ``cwd``, failing the test on a nonzero exit."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _real_clone(path: Path) -> None:
    """A real git clone at ``path`` with one commit, whose work lives under ``src/``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "."], cwd=path)
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("one\n")
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-qm", "init"], cwd=path)


@pytest.fixture
def real_git(monkeypatch):
    """Deterministic identity for the child `git commit`, and no `gh` call."""
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)


def test_root_mtime_does_not_observe_work_but_last_commit_does(tmp_path, real_git):
    # The finding this signal exists for, reproduced end-to-end: an agent edits and
    # commits under `src/`, and the clone ROOT's mtime does not move — a directory's
    # mtime bumps only when an entry is added or removed in THAT directory. Reading
    # idleness from mtime alone would call this actively-worked Tree idle and let gc
    # delete a live agent's cwd. The HEAD committer stamp is what sees the work.
    clone = tmp_path / "trees" / "acme/widget/issues/1/work-aaaa"
    _real_clone(clone)
    # Backdate the root dir to simulate a Tree cut days ago: nothing has been added to
    # or removed from the root since, which is the ordinary case for a working Tree.
    stale = time.time() - 10 * 86_400
    os.utime(clone, (stale, stale))
    mtime_before = clone.stat().st_mtime

    # Ordinary agent work: edit an existing file under `src/`, stage it, commit it.
    (clone / "src" / "a.py").write_text("two\n")
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-qm", "work"], cwd=clone)

    # The empirical claim, asserted rather than assumed: real work left mtime stale...
    assert clone.stat().st_mtime == mtime_before
    assert time.time() - clone.stat().st_mtime > 9 * 86_400

    # ...while the record's `last_commit` reads FRESH, so the ladder sees the agent.
    (record,) = registry.scan(tmp_path / "trees")
    assert record.last_commit is not None
    assert time.time() - record.last_commit < 60


def test_last_commit_is_committer_time_so_a_rebase_refreshes_it(tmp_path, real_git):
    # COMMITTER time (%ct), not AUTHOR time (%at): they agree on an ordinary commit,
    # but only the committer stamp moves on amend/rebase — which is an agent working
    # in the Tree right now. Author time would read as idle straight through a rebase.
    clone = tmp_path / "trees" / "acme/widget/issues/2/work-bbbb"
    _real_clone(clone)
    # A commit whose AUTHOR time is ancient but which is being committed NOW — exactly
    # what an amend or rebase of old work produces.
    old = "2020-01-01T00:00:00"
    (clone / "src" / "a.py").write_text("three\n")
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-qm", "replayed", "--date", old], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    author_at = float(_git(["log", "-1", "--format=%at"], cwd=clone).stdout.strip())
    # The author stamp is years stale; the record tracks the committer stamp instead.
    assert time.time() - author_at > 365 * 86_400
    assert record.last_commit is not None
    assert time.time() - record.last_commit < 60


def test_unreadable_last_commit_reads_as_none(tmp_path, real_git):
    # An unborn HEAD (a clone with no commits) cannot report a stamp. `None`, not 0:
    # the ladder must read unknown conservatively as ACTIVE, never as "ancient".
    clone = tmp_path / "trees" / "acme/widget/issues/3/work-cccc"
    clone.mkdir(parents=True)
    _git(["init", "-q", "."], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    assert record.last_commit is None
