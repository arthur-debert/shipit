"""``tree/registry`` — derive the Tree fleet by SCANNING the central root.

``scan(root) -> [TreeRecord]`` walks the central root and reads each clone's state
straight off disk — branch, base (upstream tracking ref), dirty flag, ahead/behind,
and the reclaim signals (the activity walk, HEAD's commit stamp, the unpushed SHAs).
There is deliberately **NO manifest file**: the clones on disk are the whole store,
consistent with shipit's stateless ethos (cf. the PR engine's ``prstate`` — snapshot →
record, never a side database). A Tree is a directory that is itself a git clone (it
contains a ``.git``); any other directory under the root is ignored.

The module mirrors ``prstate``'s "snapshot → record" idiom: :func:`scan` is the I/O
seam (it reads each clone through the :mod:`shipit.git` boundary, so tests patch that
one module), and :class:`TreeRecord` is the plain, frozen snapshot the ``list`` verb
renders. ``scan`` does NOT mutate anything — it is a pure read of the fleet.

**The scan makes ZERO network calls** (ADR-0072). Every signal its two consumers need —
``tree list``'s display fields and ``gc``'s reclaim rule — rides a local ``git`` read,
so a fleet-wide sweep no longer pays a per-Tree (nor a per-repo) GitHub round-trip. The
``gh`` PR read this module once made ONE CALL PER REPO (a batched ``PrIndex``) fed a
reclaim signal ADR-0072 deleted; it is gone with the ladder that consulted it — and the
batch adapter with it — that per-repo fan-out was the >10-minute sweep of #1011.
The per-clone reads still fan out across a bounded pool so a large fleet overlaps their
subprocess latency instead of paying for it serially.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import git
from . import activity

if TYPE_CHECKING:
    from ..identity import Sha

logger = logging.getLogger("shipit.tree")

#: Upper bound on the per-clone read fan-out. Each task is I/O-bound — it blocks on
#: local ``git`` subprocesses, not on the GIL — so threads (not processes) are the right
#: tool, and the cap exists only to keep a large fleet from spawning hundreds of
#: concurrent subprocesses (fd/process pressure). Because the tasks block on subprocesses
#: rather than burn CPU, we do NOT scale the pool down to the core count — a 1-2 core box
#: should still overlap subprocess latency, so the width is this flat max bounded only by
#: the clone count (:func:`_scan_workers`), with no core-derived term and no floor.
#:
#: 32, not a smaller cap, because every task here is now a handful of purely LOCAL
#: ``git`` subprocesses. It was raised from 8 when the scan still made a ``gh pr view``
#: network round-trip per task and the cap throttled concurrent GitHub calls; ADR-0072
#: deleted the PR read entirely (:func:`scan` makes zero network calls), so the only
#: limit left is subprocess/fd pressure and the band can stay wide.
_MAX_SCAN_WORKERS = 32


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
      level while carrying local-only commits. The reclaim rule (ADR-0072) keys its
      never-lose-work floor off this
      (:func:`~shipit.tree.cleanup._has_local_only_work`). The count is the derived
      :attr:`unpushed` property.
    - ``mtime`` — the clone ROOT directory's mtime (epoch seconds); the verb renders
      it as age. Note what this does and does not observe: a directory's mtime bumps
      only when an entry is added or removed in THAT directory, so it catches
      root-level churn and checkout activity but NOT an edit or commit under ``src/``.
      It is a DISPLAY signal, not the reclaim one: ``gc`` measured age from it and
      deleted a live session's Tree out from under it (#1018), because against the
      live fleet it lags real activity by up to 10 hours. :attr:`newest_mtime` is what
      reclaim reads.
    - ``newest_mtime`` — the newest mtime of any FILE in the clone, over a walk with
      the build/env dirs pruned (:func:`shipit.tree.activity.newest_mtime`), or
      ``None`` when it could not be established. **The reclaim signal** (ADR-0072):
      unlike ``mtime`` it observes an agent editing under ``src/``, and unlike a
      commit stamp or a PR read it observes a session that has committed nothing.
      ``None`` means unreadable, which reads as ACTIVE downstream — an unreadable
      signal must never license a delete (``unpushed_shas``' precedent).
    - ``last_commit`` — ``HEAD``'s COMMITTER timestamp (epoch seconds), or ``None``
      when it could not be read (:func:`shipit.git.head_committed_at`). It moves on
      every commit, amend and rebase. A reclaim input, but only ever a KEEPING one:
      ``gc`` maxes it into idle (:func:`shipit.tree.cleanup._idle_seconds`) and never
      decides on it. It earns that place by covering the one thing ``newest_mtime``
      structurally cannot — a commit that only DELETES files writes no file whose
      mtime survives it, so the walk alone reads such a Tree at its pre-deletion age.
      ``None`` means unreadable, which — like ``newest_mtime``'s — reads as ACTIVE and
      BLANKS idle rather than deferring to the walk: the two cover each other's blind
      spots, so an unknown half is a hole, not a lesser answer (ADR-0072).
    """

    path: str
    branch: str | None
    base: str | None
    dirty: bool
    ahead: int
    behind: int
    mtime: float
    unpushed_shas: tuple[Sha, ...] | None = None
    last_commit: float | None = None
    newest_mtime: float | None = None

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

    The cheap walk (just locating ``.git`` markers) runs sequentially. Then the
    per-clone reads — branch/base/dirty/ahead-behind and the reclaim signals, each a
    local ``git`` subprocess through the :mod:`shipit.git` boundary — fan out across a
    bounded :class:`~concurrent.futures.ThreadPoolExecutor` so a large fleet overlaps
    that subprocess latency instead of paying for it serially. There is NO network I/O:
    the per-repo ``gh`` PR batch this scan once ran was deleted with the reclaim signal
    it fed (ADR-0072).

    Each task builds and RETURNS its own :class:`TreeRecord` (no shared mutable
    accumulator written from threads), and the results are SORTED by path afterward, so
    ``scan``'s output is identical regardless of task completion order — a stable,
    deterministic listing.
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

    # Fan the per-clone reads out; each task returns its own record (race-free), then
    # we sort for a deterministic order independent of completion order. No network I/O:
    # the per-repo `gh` PR batch this scan once ran was deleted with the reclaim signal
    # it fed (ADR-0072), so every task is purely local `git` subprocesses.
    with ThreadPoolExecutor(max_workers=_scan_workers(len(clone_dirs))) as pool:
        records = list(pool.map(_read_record, clone_dirs))
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


def _read_record(path: Path) -> TreeRecord:
    """Snapshot one clone at ``path`` — a purely LOCAL read.

    All git reads go through the :mod:`shipit.git` boundary so tests patch that one
    module; the activity walk goes through :func:`shipit.tree.activity.newest_mtime`
    (~1.9ms, build/env dirs pruned — the reclaim signal, ADR-0072). This function holds
    only the mapping from those reads to a :class:`TreeRecord`, and issues NO network
    call: the per-repo ``gh`` PR read this scan once made was deleted with the reclaim
    signal it fed (ADR-0072).
    """
    cwd = str(path)
    branch = git.current_branch(cwd=cwd)
    base = git.upstream_ref(cwd=cwd)
    dirty = bool(git.status_porcelain(cwd=cwd))
    ahead, behind = git.ahead_behind(cwd=cwd)
    unpushed_shas = git.unpushed_shas(cwd=cwd)
    mtime = path.stat().st_mtime
    last_commit = git.head_committed_at(cwd=cwd)
    newest = activity.newest_mtime(path)
    return TreeRecord(
        path=cwd,
        branch=branch,
        base=base,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        mtime=mtime,
        unpushed_shas=unpushed_shas,
        last_commit=last_commit,
        newest_mtime=newest,
    )
