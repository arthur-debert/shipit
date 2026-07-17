"""``tree/registry`` — derive the Tree fleet by SCANNING the central root.

``scan(root) -> [TreeRecord]`` walks the central root and reads each clone's state
straight off disk — branch, base (upstream tracking ref), dirty flag, ahead/behind,
and (via :mod:`shipit.gh`) the PR state. There is deliberately **NO manifest file**:
the clones on disk are the whole store, consistent with shipit's stateless ethos
(cf. the PR engine's ``prstate`` — snapshot → record, never a side database). A Tree
is a directory that is itself a git clone (it contains a ``.git``); any other
directory under the root is ignored.

The module mirrors ``prstate``'s "snapshot → record" idiom: :func:`scan` is the I/O
seam (it reads each clone through the :mod:`shipit.gh` boundary, so tests patch that
one module), and :class:`TreeRecord` is the plain, frozen snapshot the ``list`` verb
renders. ``scan`` does NOT mutate anything — it is a pure read of the fleet.

The PR state is read ONE CALL PER REPO, not per Tree (:func:`_pr_index` →
:func:`shipit.gh.prs_by_head`), before the per-clone fan-out. This is the module's
load-bearing performance shape, not an optimization detail: a Tree-sized fan-out of
network calls both dominated ``tree list``'s runtime and exhausted GitHub's hourly
GraphQL budget mid-``gc``, which — because an unreadable PR state means *keep* — made
``gc`` exit 0 having swept nothing while reporting success (issue #1011). Everything
downstream of the batch is local: the per-clone tasks read git only.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import gh, git, identity
from ..execrun import ExecError

if TYPE_CHECKING:
    from ..identity import Sha

logger = logging.getLogger("shipit.tree")

#: Upper bound on the per-clone read fan-out. Each task is I/O-bound — it blocks on
#: local ``git`` subprocesses, not on the GIL — so threads (not processes) are the right
#: tool, and the cap exists only to keep a large fleet from spawning hundreds of
#: concurrent subprocesses (fd/process pressure). Because the tasks block on subprocesses
#: rather than burn CPU, we do NOT scale the pool down to the core count: we keep a floor
#: (:data:`_MIN_SCAN_WORKERS`) so even a 1-2 core box overlaps subprocess latency, then
#: bound that by this max and the clone count.
#:
#: Raised from 8 to 32 with the per-repo PR batch (issue #1011). The old value was set
#: when each task also made a NETWORK round-trip (``gh pr view``, hundreds of ms); the
#: cap on concurrent in-flight GitHub calls was doing real work. Now that the only
#: network read happens ONCE per repo before the fan-out, every task here is a handful of
#: local git subprocesses, so the band can widen to overlap far more of them at once.
_MAX_SCAN_WORKERS = 32

#: Upper bound on the concurrent per-repo PR batch calls. These are the scan's ONLY
#: network reads — one ``gh pr list`` per distinct repo, a handful even for a large
#: fleet — and they are pure latency, so they run in parallel rather than serially
#: (a dozen repos at ~3s each would otherwise re-import the very wall-clock cost the
#: batch removes). Kept modest: this is concurrent load on GitHub's API, the resource
#: whose exhaustion started all this.
_MAX_PR_BATCH_WORKERS = 8


def _scan_workers(clone_count: int) -> int:
    """Pick a bounded worker count for ``clone_count`` clones (always ``>= 1``).

    Flat :data:`_MAX_SCAN_WORKERS`, capped at the clone count so we never spawn idle
    workers. The core count deliberately does NOT appear: every task here blocks on a
    git subprocess rather than burning CPU, so cores are not the scarce resource and
    deriving the pool from them only throttled the scan on small boxes for no reason
    (issue #1011). The bound that remains is about subprocess/fd pressure — a real
    limit — not about parallelism the machine can "afford".
    """
    return max(1, min(_MAX_SCAN_WORKERS, clone_count))


#: The marker that makes a directory a Tree: an independent clone has a ``.git``
#: (a dir in a normal clone). A directory under the central root WITHOUT one is not
#: a Tree (a namespace dir like ``<org>/<repo>/issues`` or a stray dir) and is skipped.
_GIT_MARKER = ".git"


@dataclass(frozen=True)
class TreeRecord:
    """A snapshot of one Tree's on-disk state — the row the ``list`` verb renders.

    Every field is derived purely from the clone on disk (no manifest):

    - ``path``  — the clone's absolute directory.
    - ``branch`` — its current branch, or ``None`` on a detached/unborn HEAD.
    - ``base`` — the branch's upstream tracking ref (e.g. ``origin/main``), or
      ``None`` when the branch has no upstream. This is the only durable record of
      what the Tree is measured against, so ``scan`` reports what git tracks.
    - ``dirty`` — ``True`` when the working tree has uncommitted/untracked changes.
    - ``ahead`` / ``behind`` — commits ahead of / behind the upstream (``0`` each
      when there is no upstream).
    - ``unpushed_shas`` — the :class:`~shipit.identity.Sha`\\s of commits on ``HEAD``
      that exist on NO remote at all, or ``None`` when they could not be read.
      Distinct from ``ahead``, which is measured against the upstream and reads ``0``
      for a branch with no upstream — a fresh ``ephemeral/<id>`` branch would look
      level while carrying local-only commits. The ephemeral gc ladder (ADR-0027)
      keys its never-lose-work floor off this, and the typed SHAs (not a bare count)
      are what lets it exclude exactly the recorded provisioning commit (#232). The
      count is the derived :attr:`unpushed` property.
    - ``pr`` — a short PR-state label (``"#123 OPEN"``, ``"#123 MERGED"``,
      ``"#123 DRAFT"``…), or ``None`` when the branch has no PR.
    - ``pr_state`` — the same PR's state WITHOUT the number (``"OPEN"``, ``"MERGED"``,
      ``"DRAFT"``, ``"CLOSED"``, ``"UNKNOWN"``), or ``None`` for no branch / no PR.
      Not a second read: ``pr`` and ``pr_state`` are two views of ONE PR snapshot
      (``unpushed_shas``/``unpushed``'s precedent), so they cannot disagree. It exists
      because ``gc`` branches on the STATE and used to re-read every Tree's PR itself to
      get it — a second per-Tree round-trip on top of the scan's (issue #1011). Reading
      it off the record is what lets a gc sweep cost the same one-call-per-repo the
      scan already paid.

      ``"UNKNOWN"`` stays distinct from ``None`` — but NOT because they bucket
      differently. They do not: ``classify`` sends both to the same non-deleting
      bucket on all three ladders (write → stale, review → keep, ephemeral → decided
      on liveness/age without consulting the PR at all). The distinction is load-
      bearing for gc's HONESTY rather than its safety: ``plan.unknown`` counts
      ``"UNKNOWN"``, and that count is what makes a sweep announce it saw only part of
      the root and exit non-zero. Report ``None`` where the truth was unreadable and
      the count reads 0 — so gc claims a complete view of a fleet it could not read
      and exits 0 having swept nothing, which IS the #1011 failure that let 526 Trees
      accumulate. The lie is the bug; the delete never happens.

      **Deliberately has no default, unlike every other optional-looking field here.**
      There is no correct default, because the field means *what the scan learned* and
      a constructor that did not read cannot claim to have learned anything: ``None``
      would assert "provably no PR" and ``"UNKNOWN"`` would assert "we tried and
      failed", and both are a claim to knowledge the caller does not have. ``None``
      would additionally be the silent one — zeroing the count above. So requiring it
      turns "forgot to think about the PR state" into a ``TypeError`` at construction.
      It is also simply the same fact as ``pr``, which is required too: one read, two
      views, so exactly one of them defaulting would be incoherent.
    - ``mtime`` — the clone ROOT directory's mtime (epoch seconds); the verb renders
      it as age. Note what this does and does not observe: a directory's mtime bumps
      only when an entry is added or removed in THAT directory, so it catches
      root-level churn and checkout activity but NOT an edit or commit under ``src/``.
      It is an activity signal only in combination with ``last_commit``.
    - ``last_commit`` — ``HEAD``'s COMMITTER timestamp (epoch seconds), or ``None``
      when it could not be read. The signal that actually observes an agent working
      (:func:`shipit.git.head_committed_at`): it moves on every commit, amend and
      rebase, none of which touch ``mtime``. The write gc ladder takes the NEWEST of
      the two as the Tree's last activity, and reads ``None`` conservatively as
      ACTIVE — an unreadable timestamp must never license a delete (``unpushed_shas``'
      precedent).
    """

    path: str
    branch: str | None
    base: str | None
    dirty: bool
    ahead: int
    behind: int
    pr: str | None
    pr_state: str | None
    mtime: float
    unpushed_shas: tuple[Sha, ...] | None = None
    last_commit: float | None = None

    @property
    def unpushed(self) -> int | None:
        """How many commits exist on no remote — ``None`` when unreadable.

        Derived from :attr:`unpushed_shas` so the count and the identities can
        never disagree: one stored fact, two views.
        """
        return None if self.unpushed_shas is None else len(self.unpushed_shas)


def scan(root: str | Path) -> list[TreeRecord]:
    """Walk ``root`` and return a :class:`TreeRecord` for every Tree clone under it.

    A Tree is any directory that is itself a git clone (contains a ``.git``); the
    walk does NOT descend into a clone once found (a clone's own ``.git`` and nested
    paths are not separate Trees). Directories that are not clones — namespace dirs
    and stray non-Tree dirs alike — are simply skipped, so the fleet view reflects
    only real Trees. A missing or empty root yields ``[]``.

    The cheap walk (just locating ``.git`` markers) runs sequentially. Then the two
    expensive halves run CONCURRENTLY, which is the module's whole performance shape
    (issue #1011):

    - the fleet's PR state, read in ONE ``gh`` call per REPO
      (:func:`_start_pr_batches`) — the scan's only network I/O, hoisted out of the
      per-clone work so its cost tracks the repo count, not the Tree count, and
      *started first* because it is pure latency;
    - the per-clone reads — branch/base/dirty/ahead-behind, each a local ``git``
      subprocess through the :mod:`shipit.git` boundary — fanned out across a bounded
      :class:`~concurrent.futures.ThreadPoolExecutor` so a large fleet overlaps that
      subprocess latency instead of paying for it serially.

    Each per-clone task joins its repo's batch only after its own git reads
    (:func:`_read_record`), so the network wait hides behind local work rather than
    adding to it. Each task builds and RETURNS its own :class:`TreeRecord` (no shared
    mutable accumulator written from threads), and the results are SORTED by path
    afterward, so ``scan``'s output is identical regardless of task completion order —
    a stable, deterministic listing.
    """
    base = Path(root)
    if not base.is_dir():
        logger.debug("tree scan found no central root at %s; empty fleet", base)
        return []

    started = time.monotonic()
    clone_dirs: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(base):
        here = Path(dirpath)
        if (here / _GIT_MARKER).exists():
            clone_dirs.append(here)
            # A clone is a leaf for scanning purposes — never descend into it.
            dirnames[:] = []
            continue

    if not clone_dirs:
        return []

    # ONE PR read per repo instead of one per Tree (issue #1011), STARTED FIRST and
    # joined last: the calls are pure network latency and the per-clone git reads do
    # not depend on them, so the batch flies while the local work happens and its cost
    # largely disappears behind it. Each task blocks on its own repo's result only at
    # the very end (:func:`_read_record`).
    with ThreadPoolExecutor(max_workers=_MAX_PR_BATCH_WORKERS) as batch_pool:
        pending = _start_pr_batches(clone_dirs, batch_pool)

        # Fan the per-clone reads out; each task returns its own record (race-free),
        # then we sort for a deterministic order independent of completion order.
        with ThreadPoolExecutor(max_workers=_scan_workers(len(clone_dirs))) as pool:
            records = list(pool.map(lambda d: _read_record(d, pending[d]), clone_dirs))
    records.sort(key=lambda record: record.path)
    # Mechanics at DEBUG (spray convention): the fleet-read's size + cost, so a
    # slow `list`/`gc` is attributable to the scan from the durable record.
    logger.debug(
        "tree scan read %d Tree(s) under %s in %dms",
        len(records),
        base,
        int((time.monotonic() - started) * 1000),
    )
    return records


#: One repo's PR index as the scan consumes it: ``{head branch -> HeadPr}`` when the
#: repo was read cleanly, or :data:`~shipit.gh.UNKNOWN` when its view is undetermined.
#: The dict's COMPLETENESS is the contract (:func:`shipit.gh.prs_by_head`) — it is what
#: lets an absent branch mean "provably no PR" rather than "unread".
PrIndex = dict[str, gh.HeadPr] | gh.UnknownPr


def _repo_slug(path: Path) -> str | None:
    """The ``owner/name`` slug of the clone at ``path``, or ``None`` when unreadable.

    Resolved from the clone's own ORIGIN REMOTE (:func:`shipit.identity.resolve_repo`)
    — an offline, local read — not from the clone's position under the central root.
    The path shape is *usually* ``<root>/<owner>/<repo>/…`` (:func:`tree.layout.repo_dir`
    builds it), but it is not a reliable identity: real fleets carry hash-named roots
    (``<root>/2f86/shipit/…``) whose first segment is no GitHub owner at all. Parsing
    those would either drop them from the batch or build a bogus ``2f86/shipit`` slug;
    reading the remote makes them ordinary — and collapses every such root onto the ONE
    real repo, so five hash roots of ``shipit`` share a single ``gh`` call rather than
    provoking five.

    ``None`` (no origin remote, or an unparseable URL) is not an error: the same clone
    would have failed ``gh pr view`` too. The caller maps it to :data:`~shipit.gh.UNKNOWN`
    — undetermined, never "no PR".
    """
    try:
        return identity.resolve_repo(str(path)).slug
    except (ExecError, ValueError):
        return None


def _start_pr_batches(
    clone_dirs: list[Path], batch_pool: ThreadPoolExecutor
) -> dict[Path, Future[PrIndex] | None]:
    """Kick off ONE ``gh`` PR read per repo and return each clone's in-flight result.

    Resolves every clone's repo (local, parallel — :func:`_repo_slug`), then submits
    one :func:`shipit.gh.prs_by_head` per DISTINCT repo to ``batch_pool``. Returns a
    ``{clone -> Future}`` map, keyed by clone so the per-clone tasks stay ignorant of
    repos, and ``None`` for a clone whose repo could not be resolved. Nothing is waited
    on here: the calls run while the caller does its local git work, and each task joins
    only its own repo's future (:func:`_read_record`).

    Why this exists (issue #1011): the previous shape made one ``gh pr view`` per Tree.
    On a 526-Tree fleet that is ~512 sequential round-trips — 70% of ``tree list``'s
    runtime, and enough to exhaust the hourly GraphQL budget mid-``gc``, at which point
    every remaining Tree read as ``UNKNOWN``, the ladder kept them all, and ``gc`` exited
    0 having swept nothing while reporting success. Batching per repo removes both the
    latency and the budget exhaustion, and stops each scaling with fleet size: the cost
    is now set by how many repos the fleet spans, not how many Trees it holds.

    A clone whose repo is unresolvable (``None`` here), and every clone of a repo whose
    batch call fails, end up :data:`~shipit.gh.UNKNOWN` — the undetermined arm, never a
    silent "no PR". That distinction does not change any ladder's bucket (both are
    non-deleting everywhere); it is what keeps ``gc`` HONEST. ``plan.unknown`` counts
    the UNKNOWNs, and that count is the whole basis of the incomplete-view warning and
    the non-zero exit. Answering "no PR" for a repo we failed to read would zero it and
    let ``gc`` report a clean bill of health for Trees it never saw — #1011's silent
    success, rebuilt one repo at a time.
    """
    with ThreadPoolExecutor(max_workers=_scan_workers(len(clone_dirs))) as pool:
        slugs = dict(zip(clone_dirs, pool.map(_repo_slug, clone_dirs), strict=True))

    futures = {
        repo: batch_pool.submit(gh.prs_by_head, repo)
        for repo in sorted({slug for slug in slugs.values() if slug is not None})
    }
    return {
        clone: futures[slug] if slug is not None else None
        for clone, slug in slugs.items()
    }


def _await_pr_index(pending: Future[PrIndex] | None) -> PrIndex:
    """Join one clone's in-flight repo batch — :data:`~shipit.gh.UNKNOWN` if it has none.

    ``None`` means the clone's repo never resolved (no origin remote), so there was no
    batch to join and its state is undetermined — the same answer the per-Tree ``gh``
    read gave, since it would have failed on that clone too.
    """
    return gh.UNKNOWN if pending is None else pending.result()


def _pr_for_branch(prs: PrIndex, branch: str) -> gh.HeadPr | None | gh.UnknownPr:
    """One branch's PR out of its repo's index — the three-way :func:`shipit.gh.pr_for_head`
    vocabulary, reconstructed from the batch.

    An UNKNOWN index stays UNKNOWN for every branch in it. Otherwise the index is
    complete by contract, so a MISS is a provable ``None`` — the branch genuinely has
    no PR — exactly what the per-branch read returned on ``gh``'s no-PR message.
    """
    if prs is gh.UNKNOWN:
        return gh.UNKNOWN
    return prs.get(branch)


def _read_record(path: Path, pending: Future[PrIndex] | None) -> TreeRecord:
    """Snapshot one clone at ``path``, joining its repo's in-flight PR batch LAST.

    All git reads go through the :mod:`shipit.git` / :mod:`shipit.gh` boundaries so
    tests patch those modules; this function holds only the mapping from those reads to
    a :class:`TreeRecord`. This task issues NO network call of its own (the PR read is
    one per repo, not per Tree — issue #1011); it only waits on the shared batch its
    repo already has in flight, and does so AFTER its local git reads so the two
    overlap.
    """
    cwd = str(path)
    branch = git.current_branch(cwd=cwd)
    base = git.upstream_ref(cwd=cwd)
    dirty = bool(git.status_porcelain(cwd=cwd))
    ahead, behind = git.ahead_behind(cwd=cwd)
    unpushed_shas = git.unpushed_shas(cwd=cwd)
    mtime = path.stat().st_mtime
    last_commit = git.head_committed_at(cwd=cwd)
    # Last: the only blocking wait, after every local read has had its chance to run.
    head_pr = _pr_for_branch(_await_pr_index(pending), branch) if branch else None
    return TreeRecord(
        path=cwd,
        branch=branch,
        base=base,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        pr=_pr_label(head_pr),
        pr_state=_pr_display_state(head_pr),
        mtime=mtime,
        unpushed_shas=unpushed_shas,
        last_commit=last_commit,
    )


def _pr_display_state(pr: gh.HeadPr | None | gh.UnknownPr) -> str | None:
    """The PR's state alone (``"OPEN"`` / ``"DRAFT"`` / ``"MERGED"`` / ``"CLOSED"`` /
    ``"UNKNOWN"``), or ``None`` when there is no PR — the view ``gc``'s ladder branches on.

    The state half of the same snapshot :func:`_pr_label` renders, so the label and the
    state can never disagree: one read, two views (:attr:`TreeRecord.unpushed`'s
    precedent). ``gc`` reading this off the record is what keeps it from re-reading every
    Tree's PR itself.
    """
    if pr is gh.UNKNOWN:
        return "UNKNOWN"
    if pr is None:
        return None
    return pr.display_state


def _pr_label(pr: gh.HeadPr | None | gh.UnknownPr) -> str | None:
    """A short ``"#<n> <STATE>"`` label for a PR snapshot, or ``None`` when there is none.

    The state vocabulary is the snapshot's own
    (:attr:`~shipit.gh.HeadPr.display_state`): a draft open PR reads as ``DRAFT``
    (the turn-signal the dev cycle hinges on); otherwise the GitHub state
    (``OPEN`` / ``MERGED`` / ``CLOSED``) is shown verbatim. An
    :data:`~shipit.gh.UNKNOWN` snapshot (the state could not be read) renders as a
    bare ``UNKNOWN`` so an unreadable Tree is visible in ``list``, distinct from the
    ``-`` a genuinely-PR-less Tree shows.
    """
    if pr is gh.UNKNOWN:
        return "UNKNOWN"
    if pr is None:
        return None
    return f"#{pr.number} {pr.display_state}"
