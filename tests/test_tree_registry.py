"""Unit tests for ``tree.registry.scan`` — deriving the fleet from on-disk clones.

These assert EXTERNAL behavior (PRD Testing Decisions): given a fixture directory
layout under a central root, ``scan`` returns the right :class:`TreeRecord`s
(branch, base, dirty, ahead/behind, PR label) and IGNORES any directory that is not
itself a git clone. The ``gh`` boundary (branch/dirty/ahead-behind/PR reads) is
patched so no real git/gh runs — the fixture is the disk layout, the patch is the
per-clone state.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from shipit import gh
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
            "dirty": " M file.py\n",
            "ahead_behind": (2, 0),
            "unpushed": 2,
        },
        str(b): {
            "branch": "HAR02/WS02",
            "base": "origin/HAR02/umbrella",
            "dirty": "",
            "ahead_behind": (0, 3),
            "unpushed": 0,
        },
    }
    pr_by_branch = {
        "issues/123/work": {"number": 7, "state": "OPEN", "isDraft": True},
        "HAR02/WS02": {"number": 9, "state": "MERGED", "isDraft": False},
    }

    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: state[cwd]["branch"])
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: state[cwd]["base"])
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: state[cwd]["dirty"])
    monkeypatch.setattr(
        gh, "git_ahead_behind", lambda *, cwd: state[cwd]["ahead_behind"]
    )
    monkeypatch.setattr(gh, "git_unpushed_count", lambda *, cwd: state[cwd]["unpushed"])
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


def test_scan_reads_the_upstream_independent_unpushed_count(fleet):
    # The `unpushed` field is the ephemeral gc ladder's never-lose-work signal:
    # commits on NO remote at all, read independently of any upstream.
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}
    assert by_path[str(a)].unpushed == 2
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
    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: None)
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
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
    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
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

    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    records = registry.scan(root)
    assert [r.path for r in records] == [str(outer)]


def _patch_trivial_gh(monkeypatch, *, branch_hook=None):
    """Patch every gh read with cheap stubs; ``branch_hook(cwd)`` runs first if given."""

    def _branch(*, cwd):
        if branch_hook is not None:
            branch_hook(cwd)
        return "issues/1/work"

    monkeypatch.setattr(gh, "git_current_branch", _branch)
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
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
