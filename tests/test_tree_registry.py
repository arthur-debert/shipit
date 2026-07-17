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
from shipit.execrun import ExecError
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


def _patch_repo(monkeypatch, prs, *, slug: str = "acme/widget", hook=None):
    """Point every fake clone at ``slug`` and give that repo the ``prs`` PR index.

    Patches the two boundary reads the batched scan makes per REPO (issue #1011):
    ``git.remote_url`` (which :func:`shipit.identity.resolve_repo` parses into the
    slug that groups clones) and ``gh.prs_by_head`` (the one-call-per-repo index).
    ``prs`` is a ``{branch: HeadPr}`` dict or ``gh.UNKNOWN``. ``hook(slug)`` runs
    inside the batch call when given, so a test can observe the calls.
    """

    def _prs_by_head(repo):
        if hook is not None:
            hook(repo)
        return prs

    monkeypatch.setattr(
        git, "remote_url", lambda *, cwd, remote="origin": f"git@github.com:{slug}.git"
    )
    monkeypatch.setattr(gh, "prs_by_head", _prs_by_head)


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
    _patch_repo(monkeypatch, pr_by_branch)
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
    # The repo was read cleanly and this branch is simply absent from its index:
    # a COMPLETE index makes absence provable, which is the "no PR" arm.
    _patch_repo(monkeypatch, {})

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.pr is None
    assert record.pr_state is None
    assert record.base is None
    assert (record.ahead, record.behind) == (0, 0)


def test_scan_unreadable_pr_renders_unknown_label(tmp_path: Path, monkeypatch):
    # An unreadable repo view (gh.prs_by_head -> UNKNOWN) renders as a bare "UNKNOWN"
    # label, distinct from the None a genuinely-PR-less Tree shows.
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-zzzz")
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/1/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    _patch_repo(monkeypatch, gh.UNKNOWN)

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.pr == "UNKNOWN"
    assert record.pr_state == "UNKNOWN"


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
    _patch_repo(monkeypatch, {})

    records = registry.scan(root)
    assert [r.path for r in records] == [str(outer)]


def _patch_trivial_gh(monkeypatch, *, branch_hook=None):
    """Patch every boundary read with cheap stubs; ``branch_hook(cwd)`` runs first if given."""

    def _branch(*, cwd):
        if branch_hook is not None:
            branch_hook(cwd)
        return "issues/1/work"

    monkeypatch.setattr(git, "current_branch", _branch)
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    _patch_repo(monkeypatch, {})


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
    """Deterministic identity for the child `git commit`, and no `gh` call."""
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)


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


def test_unreadable_last_commit_reads_as_none(tmp_path, real_git):
    # An unborn HEAD (a clone with no commits) cannot report a stamp. `None`, not 0:
    # the ladder must read unknown conservatively as ACTIVE, never as "ancient".
    clone = tmp_path / "trees" / "acme/widget/issues/3/work-cccc"
    clone.mkdir(parents=True)
    _git(["init", "-q", "."], cwd=clone)

    (record,) = registry.scan(tmp_path / "trees")
    assert record.last_commit is None


# --- the batched, one-call-per-repo PR read (#1011) -----------------------------------


def _patch_git_reads(monkeypatch, branch_by_path):
    """Trivial git stubs; the branch is per-clone so a test can vary it."""
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: branch_by_path[cwd])
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(git, "unpushed_shas", lambda *, cwd: ())
    monkeypatch.setattr(git, "head_committed_at", lambda *, cwd: 1_000.0)


def test_scan_makes_one_pr_call_per_repo_not_per_tree(tmp_path, monkeypatch):
    """THE fix (#1011): the PR read scales with the REPO count, not the Tree count.

    Six Trees across two repos must cost two ``gh`` calls. The old shape made one per
    Tree — ~512 on the observed fleet, which both dominated `list` and exhausted the
    hourly GraphQL budget mid-`gc`.
    """
    root = tmp_path / "trees"
    widget = [_make_clone(root, f"acme/widget/issues/{i}/work-a") for i in range(4)]
    gadget = [_make_clone(root, f"acme/gadget/issues/{i}/work-b") for i in range(2)]
    branches = {str(c): f"issues/{i}/work" for i, c in enumerate(widget + gadget)}
    _patch_git_reads(monkeypatch, branches)

    slug_of = {str(c): "acme/widget" for c in widget} | {
        str(c): "acme/gadget" for c in gadget
    }
    monkeypatch.setattr(
        git,
        "remote_url",
        lambda *, cwd, remote="origin": f"git@github.com:{slug_of[cwd]}.git",
    )
    calls = []
    monkeypatch.setattr(gh, "prs_by_head", lambda repo: calls.append(repo) or {})
    # A per-Tree read would go through pr_for_head; nothing may call it any more.
    monkeypatch.delattr(gh, "pr_for_head")

    records = registry.scan(root)

    assert len(records) == 6
    assert sorted(calls) == ["acme/gadget", "acme/widget"]  # two repos, two calls


def test_scan_collapses_hash_named_roots_onto_their_real_repo(tmp_path, monkeypatch):
    """A hash-named root (``<root>/2f86/shipit/…``) is grouped by its REMOTE, not its path.

    Real fleets carry these, and their first segment is no GitHub owner at all. Parsing
    the path would either drop them from the batch or build a bogus ``2f86/shipit`` slug;
    reading the origin remote makes them ordinary — and collapses every hash root of one
    repo onto a SINGLE call.
    """
    root = tmp_path / "trees"
    clones = [
        _make_clone(root, "2f86/shipit/issues/1/work-a"),
        _make_clone(root, "5c08/shipit/issues/2/work-b"),
        _make_clone(root, "arthur-debert/shipit/issues/3/work-c"),
    ]
    _patch_git_reads(monkeypatch, {str(c): "issues/1/work" for c in clones})
    _patch_repo(
        monkeypatch,
        {
            "issues/1/work": gh.HeadPr(
                number=4, state="OPEN", is_draft=False, base_ref="main"
            )
        },
        slug="arthur-debert/shipit",
    )
    calls = []
    inner = gh.prs_by_head
    monkeypatch.setattr(
        gh, "prs_by_head", lambda repo: calls.append(repo) or inner(repo)
    )

    records = registry.scan(root)

    # All three hash/owner roots are ONE repo -> one call, and none is dropped.
    assert calls == ["arthur-debert/shipit"]
    assert [r.pr for r in records] == ["#4 OPEN"] * 3


def test_scan_failed_batch_makes_only_that_repos_trees_unknown(tmp_path, monkeypatch):
    """A repo whose batch call fails yields UNKNOWN for ITS Trees — and only those.

    The blast radius is one repo: a healthy sibling repo still reports real states, so a
    single rate-limited repo cannot blind the whole fleet.
    """
    root = tmp_path / "trees"
    broken = _make_clone(root, "acme/broken/issues/1/work-a")
    healthy = _make_clone(root, "acme/healthy/issues/2/work-b")
    _patch_git_reads(
        monkeypatch, {str(broken): "issues/1/work", str(healthy): "issues/2/work"}
    )
    slug_of = {str(broken): "acme/broken", str(healthy): "acme/healthy"}
    monkeypatch.setattr(
        git,
        "remote_url",
        lambda *, cwd, remote="origin": f"git@github.com:{slug_of[cwd]}.git",
    )
    monkeypatch.setattr(
        gh,
        "prs_by_head",
        lambda repo: (
            gh.UNKNOWN
            if repo == "acme/broken"
            else {
                "issues/2/work": gh.HeadPr(
                    number=2, state="MERGED", is_draft=False, base_ref="main"
                )
            }
        ),
    )

    by_path = {r.path: r for r in registry.scan(root)}

    assert by_path[str(broken)].pr == "UNKNOWN"
    assert by_path[str(broken)].pr_state == "UNKNOWN"
    assert by_path[str(healthy)].pr == "#2 MERGED"
    assert by_path[str(healthy)].pr_state == "MERGED"


def test_scan_unresolvable_remote_is_unknown_never_no_pr(tmp_path, monkeypatch):
    """A clone with no origin remote reads UNKNOWN — the honest arm.

    Its repo cannot be resolved, so it joins no batch. It must NOT fall through to "no
    PR", which would be a positive claim we cannot make. Not because the bucket would
    change — it wouldn't; no-PR and UNKNOWN bucket identically on every ladder — but
    because only UNKNOWN is counted by `plan.unknown`, and that count is what makes gc
    admit it saw part of the root. Saying "no PR" here would hide this Tree inside a
    confidently-complete sweep. (The old per-Tree read also returned UNKNOWN for this
    clone — gh would have failed on it the same way.)
    """
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-a")
    _patch_git_reads(monkeypatch, {str(clone): "issues/1/work"})

    def no_remote(*, cwd, remote="origin"):
        raise ExecError(("git", "remote", "get-url", "origin"), rc=128)

    monkeypatch.setattr(git, "remote_url", no_remote)
    monkeypatch.setattr(gh, "prs_by_head", lambda repo: {})

    (record,) = registry.scan(root)
    assert record.pr == "UNKNOWN"
    assert record.pr_state == "UNKNOWN"


def test_scan_detached_head_has_no_pr_without_consulting_the_index(
    tmp_path, monkeypatch
):
    # No branch -> no PR question to ask, even when its repo's index is UNKNOWN.
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-a")
    _patch_git_reads(monkeypatch, {str(clone): None})
    _patch_repo(monkeypatch, gh.UNKNOWN)

    (record,) = registry.scan(root)
    assert record.branch is None
    assert record.pr is None and record.pr_state is None


def test_scan_pr_label_and_state_are_two_views_of_one_read(tmp_path, monkeypatch):
    # The label gc must never re-parse and the state it branches on come from ONE
    # snapshot, so they cannot disagree (unpushed_shas/unpushed's precedent).
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1/work-a")
    _patch_git_reads(monkeypatch, {str(clone): "issues/1/work"})
    _patch_repo(
        monkeypatch,
        {
            "issues/1/work": gh.HeadPr(
                number=8, state="OPEN", is_draft=True, base_ref="main"
            )
        },
    )

    (record,) = registry.scan(root)
    assert record.pr == "#8 DRAFT"
    assert record.pr_state == "DRAFT"


def test_pr_state_is_a_required_field_so_a_forgetful_caller_cannot_fake_a_read():
    """``pr_state`` has NO default, deliberately — there is no correct thing to default to.

    The field means *what the scan learned about this PR*, and a constructor that never
    read one has learned nothing: ``None`` would assert "provably no PR" and
    ``"UNKNOWN"`` would assert "we tried and failed". Both claim knowledge the caller
    does not have.

    ``None`` would be the more dangerous of the two — not because it deletes anything
    (no-PR and UNKNOWN bucket identically on every ladder) but because it is SILENT:
    ``plan.unknown`` counts only ``"UNKNOWN"``, so a forgotten field would zero the
    count and let gc report a complete view of a fleet it never read — #1011's
    exit-0-on-success-it-didn't-have, reintroduced through a default argument.
    Requiring the field makes that a TypeError at construction instead. (#1011)
    """
    complete = dict(
        path="/t",
        branch="b",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr=None,
        pr_state=None,
        mtime=0.0,
    )
    assert registry.TreeRecord(**complete).pr_state is None  # explicit no-PR is fine

    with pytest.raises(TypeError, match="pr_state"):
        registry.TreeRecord(**{k: v for k, v in complete.items() if k != "pr_state"})

    # The fields whose None genuinely means "unreadable" keep their fail-SAFE defaults:
    # omitting them reads as keep, so they are not the same hazard.
    record = registry.TreeRecord(**complete)
    assert record.unpushed_shas is None and record.last_commit is None


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
