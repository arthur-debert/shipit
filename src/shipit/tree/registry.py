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
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .. import gh

#: Upper bound on the per-clone read fan-out. Each task is I/O-bound — it blocks on
#: ``git``/``gh`` subprocesses through the :mod:`shipit.gh` boundary, not on the GIL —
#: so threads (not processes) are the right tool and a small cap keeps a large fleet
#: from spawning hundreds of concurrent subprocesses (fd/process pressure). We cap at
#: the CPU count but never below a useful floor so even a 1-2 core box still overlaps
#: subprocess latency.
_MAX_SCAN_WORKERS = 8


def _scan_workers(clone_count: int) -> int:
    """Pick a bounded worker count for ``clone_count`` clones (always ``>= 1``)."""
    cap = min(_MAX_SCAN_WORKERS, os.cpu_count() or 4)
    return max(1, min(cap, clone_count))


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
    - ``pr`` — a short PR-state label (``"#123 OPEN"``, ``"#123 MERGED"``,
      ``"#123 DRAFT"``…), or ``None`` when the branch has no PR.
    - ``mtime`` — the directory's mtime (epoch seconds); the verb renders it as age.
    """

    path: str
    branch: str | None
    base: str | None
    dirty: bool
    ahead: int
    behind: int
    pr: str | None
    mtime: float


def scan(root: str | Path) -> list[TreeRecord]:
    """Walk ``root`` and return a :class:`TreeRecord` for every Tree clone under it.

    A Tree is any directory that is itself a git clone (contains a ``.git``); the
    walk does NOT descend into a clone once found (a clone's own ``.git`` and nested
    paths are not separate Trees). Directories that are not clones — namespace dirs
    and stray non-Tree dirs alike — are simply skipped, so the fleet view reflects
    only real Trees. A missing or empty root yields ``[]``.

    The cheap walk (just locating ``.git`` markers) runs sequentially; the EXPENSIVE
    per-clone reads — branch/base/dirty/ahead-behind/PR, each a ``git``/``gh``
    subprocess through the :mod:`shipit.gh` boundary — are fanned out across a bounded
    :class:`~concurrent.futures.ThreadPoolExecutor` so ``list``/``gc`` over a large
    fleet overlap that subprocess latency instead of paying for it serially. Each task
    builds and RETURNS its own :class:`TreeRecord` (no shared mutable accumulator
    written from threads), and the results are SORTED by path afterward, so ``scan``'s
    output is identical regardless of task completion order — a stable, deterministic
    listing.
    """
    base = Path(root)
    if not base.is_dir():
        return []

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
    # we sort for a deterministic order independent of completion order.
    with ThreadPoolExecutor(max_workers=_scan_workers(len(clone_dirs))) as pool:
        records = list(pool.map(_read_record, clone_dirs))
    records.sort(key=lambda record: record.path)
    return records


def _read_record(path: Path) -> TreeRecord:
    """Snapshot one clone at ``path`` by reading the :mod:`shipit.gh` boundary.

    All git/gh reads go through ``gh`` so tests patch that single module; this
    function holds only the mapping from those reads to a :class:`TreeRecord`.
    """
    cwd = str(path)
    branch = gh.git_current_branch(cwd=cwd)
    base = gh.git_upstream_ref(cwd=cwd)
    dirty = bool(gh.git_status_porcelain(cwd=cwd).strip())
    ahead, behind = gh.git_ahead_behind(cwd=cwd)
    pr = _pr_label(gh.pr_for_head(branch, cwd=cwd)) if branch else None
    mtime = path.stat().st_mtime
    return TreeRecord(
        path=cwd,
        branch=branch,
        base=base,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        pr=pr,
        mtime=mtime,
    )


def _pr_label(pr: dict | None) -> str | None:
    """A short ``"#<n> <STATE>"`` label for a PR snapshot, or ``None`` when there is none.

    A draft open PR reads as ``DRAFT`` (the turn-signal the dev cycle hinges on);
    otherwise the GitHub state (``OPEN`` / ``MERGED`` / ``CLOSED``) is shown verbatim.
    """
    if not pr:
        return None
    number = pr.get("number")
    state = (pr.get("state") or "").upper()
    if state == "OPEN" and pr.get("isDraft"):
        state = "DRAFT"
    head = f"#{number}" if number is not None else "#?"
    return f"{head} {state}".strip()
