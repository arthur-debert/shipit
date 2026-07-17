"""Unit tests for ``tree.registry.scan`` — deriving the fleet from on-disk clones.

These assert EXTERNAL behavior (PRD Testing Decisions): given a fixture directory
layout under a central root, ``scan`` returns the right :class:`TreeRecord`s
(branch, base, dirty, ahead/behind, unpushed SHAs, activity signals) and IGNORES any
directory that is not itself a git clone. The ``git`` boundary is patched so no real
git runs — the fixture is the disk layout, the patch is the per-clone state. The scan
makes NO network call (ADR-0072): there is no ``gh`` boundary left to patch.
"""

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
    """A central root with two Tree clones and a non-Tree dir, with the git boundary
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
    # `unpushed_shas` is the reclaim rule's never-lose-work signal (ADR-0072): the
    # commits on NO remote at all, read independently of any upstream, by identity.
    # The `unpushed` count is derived from the same stored fact.
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
    # A path that looks like a clone nested INSIDE another clone must not be
    # reported as a second Tree — scan stops at the first .git it meets.
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
    """Patch every git boundary read with cheap stubs; ``branch_hook(cwd)`` runs first if given."""

    def _branch(*, cwd):
        if branch_hook is not None:
            branch_hook(cwd)
        return "issues/1/work"

    monkeypatch.setattr(git, "current_branch", _branch)
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))


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

    _patch_trivial_git(monkeypatch, branch_hook=_hook)
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

    _patch_trivial_git(monkeypatch, branch_hook=_hook)

    records = registry.scan(root)
    assert len(records) == n
    # All N ran simultaneously — concrete proof of the bounded fan-out.
    assert max_seen == n


def test_scan_workers_is_bounded_and_at_least_one():
    """The worker count is capped (never an unbounded thread-per-clone) and never < 1."""
    assert registry._scan_workers(1000) == registry._MAX_SCAN_WORKERS
    # Never exceeds the number of clones, and never drops below one.
    assert registry._scan_workers(3) == 3
    assert registry._scan_workers(0) == 1


def test_scan_workers_ignores_the_core_count(monkeypatch):
    """The fan-out is I/O-bound, so the core count must not enter into it at all.

    The pool used to be sized ``clamp(cpu_count, 4, 8)``, which throttled the scan on
    a small box for no reason: these tasks block on git subprocesses rather than burn
    CPU, so cores are not the scarce resource (issue #1011). A 1-core box gets the same
    fan-out a 64-core box does.
    """
    for cores in (1, 64, None):
        monkeypatch.setattr(registry.os, "cpu_count", lambda cores=cores: cores)
        assert registry._scan_workers(100) == registry._MAX_SCAN_WORKERS
        # Still bounded by the clone count.
        assert registry._scan_workers(2) == 2


# --- the reclaim activity signal, over REAL git (#1018, ADR-0072) --------------------
#
# The rest of this module patches the git boundary; these do NOT. The whole point of
# the activity signal is an empirical claim about the filesystem — that the clone
# ROOT's mtime does not observe an agent working, and that the newest file mtime does
# — and a test built on injected values cannot check that claim. So these drive real
# `git` against a real clone and read the record `scan` builds from it.


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
    """Deterministic identity for the child `git commit` (the scan makes no network call)."""
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)


def test_root_mtime_does_not_observe_work_but_newest_mtime_does(tmp_path, real_git):
    # The finding the signal exists for, reproduced end-to-end (#1018): an agent edits
    # and commits under `src/`, and the clone ROOT's mtime does not move — a
    # directory's mtime bumps only when an entry is added or removed in THAT
    # directory. That is the clock the old ladder read, and reading idleness from it
    # is what let gc delete a live agent's cwd (measured lag: up to 10 hours). The
    # record's `newest_mtime` sees the write wherever it lands.
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

    # ...while the record's `newest_mtime` — the signal gc reclaims on — reads FRESH,
    # so the rule sees the agent and keeps the Tree. (`last_commit` sees this
    # particular shape too, but only because the agent committed; it is a display
    # stamp now, blind to the uncommitted session #1018 actually deleted.)
    (record,) = registry.scan(tmp_path / "trees")
    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime < 60
    assert record.last_commit is not None


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


def test_a_deletion_only_commit_is_activity_the_walk_alone_cannot_see(
    tmp_path, real_git
):
    # The hole `last_commit` is maxed in to cover, driven end-to-end rather than
    # asserted from injected values — the claim is empirical, about what git and the
    # filesystem actually do.
    #
    # An agent working in an OLD Tree deletes a tracked file and commits. Every trace
    # of that work is somewhere `newest_mtime` cannot look: the removed file is gone,
    # its parent DIRECTORY's mtime bumped (dirs are not eligible), and the commit
    # itself landed in `.git` (pruned). No surviving file was written, so the walk
    # still reports the Tree's pre-deletion age. If idle read the walk alone, this
    # Tree — clean, fully pushed, and being worked in RIGHT NOW — is removable.
    clone = tmp_path / "trees" / "acme/widget/issues/3/work-cccc"
    _real_clone(clone)
    (clone / "src" / "doomed.py").write_text("delete me\n")
    _git(["add", "-A"], cwd=clone)
    _git(["commit", "-qm", "add"], cwd=clone)

    # A real remote, and the work PUSHED to it. Load-bearing, not scenery: without it
    # every commit is local-only, the never-lose-work floor keeps the Tree, and the
    # activity arm under test is never reached — the test would pass against the very
    # bug it exists to catch.
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(origin)], cwd=clone)
    _git(["push", "-q", "origin", "HEAD"], cwd=clone)

    # Age every file in the clone: the Tree was cut days ago and has sat quiet since.
    stale = time.time() - 10 * 86_400
    for path in clone.rglob("*"):
        if path.is_file():
            os.utime(path, (stale, stale))
    os.utime(clone, (stale, stale))

    # The work: a commit that only REMOVES. `git rm` deletes the file from disk.
    _git(["rm", "-q", "src/doomed.py"], cwd=clone)
    _git(["commit", "-qm", "drop the dead module"], cwd=clone)
    _git(["push", "-q", "origin", "HEAD"], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    # The floor is silent: nothing is dirty and every commit is on the remote, so the
    # Tree's fate rests entirely on the activity arm.
    assert record.dirty is False
    assert record.unpushed_shas == ()
    # The empirical claim, asserted rather than assumed: the walk saw nothing. Every
    # remaining file still carries its backdated stamp.
    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime > 9 * 86_400
    # ...while the commit stamp did see it.
    assert record.last_commit is not None
    assert time.time() - record.last_commit < 60

    # So the rule keeps the Tree. Reading the walk alone, it would be deleted seconds
    # after real work — #1018's shape, through a gap in the measurement.
    decision = classify([record], time.time())
    assert decision.removable == []
    assert [r.path for r in decision.keep] == [record.path]


def test_unreadable_last_commit_reads_as_none(tmp_path, real_git):
    # An unborn HEAD (a clone with no commits) cannot report a stamp. `None`, not 0:
    # the ladder must read unknown conservatively as ACTIVE, never as "ancient".
    clone = tmp_path / "trees" / "acme/widget/issues/3/work-cccc"
    clone.mkdir(parents=True)
    _git(["init", "-q", "."], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    assert record.last_commit is None


# --- the scan makes zero network calls (ADR-0072) ------------------------------------


def test_scan_makes_no_network_call(fleet, monkeypatch):
    # ADR-0072's headline at the scan seam: the per-repo `gh` PR batch that once fed the
    # reclaim ladder is deleted, so `scan` reads only local git. `registry` no longer
    # imports `gh` at all; belt-and-braces, sabotage every public `gh` entrypoint into a
    # fatal and confirm a full scan of the fixture fleet never trips one.
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
    # The #1018 session's exact shape, end to end through a real clone: the agent has
    # committed NOTHING (its work is external `gcloud` calls) and the root mtime is
    # ancient — so every signal the old ladder had reads "idle" or "not live". The one
    # true thing is that a file under `src/` was just written, and `scan` reports it.
    clone = tmp_path / "trees" / "acme/widget/ephemeral/sess-aaaa"
    _real_clone(clone)
    stale = time.time() - 10 * 86_400
    os.utime(clone, (stale, stale))
    for path in (clone / "src" / "a.py", clone / "src"):
        os.utime(path, (stale, stale))

    # A single scratch write under a subdir — no commit, no push, no PR.
    (clone / "src" / "scratch.log").write_text("provisioning bucket ...\n")

    (record,) = registry.scan(tmp_path / "trees")

    assert time.time() - record.mtime > 9 * 86_400  # the old clock: ancient
    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime < 60  # the measured one: just now


def test_scan_prunes_the_env_dirs_from_the_activity_signal(tmp_path, real_git):
    # `.pixi` is ~97% of a Tree's file count and its mtimes are an env solve, not an
    # agent: a fresh file there must not make an abandoned Tree look alive (and the
    # walk must not pay to descend it — 1.9ms pruned vs 191.7ms naive).
    clone = tmp_path / "trees" / "acme/widget/issues/9/work-cccc"
    _real_clone(clone)
    stale = time.time() - 10 * 86_400
    for path in (clone / "src" / "a.py", clone / "src", clone):
        os.utime(path, (stale, stale))
    (clone / ".pixi" / "envs" / "default").mkdir(parents=True)
    (clone / ".pixi" / "envs" / "default" / "lib.so").write_text("fresh env solve")

    (record,) = registry.scan(tmp_path / "trees")

    assert record.newest_mtime is not None
    assert time.time() - record.newest_mtime > 9 * 86_400  # still reads as abandoned
