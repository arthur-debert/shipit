"""``tree/registry`` — derive the Tree fleet by scanning the central root.

There is no manifest: a Tree is any directory under the root that is itself a git
clone, and every field is read off disk. The scan makes zero network calls.
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

#: Upper bound on the per-clone read fan-out. The tasks block on local ``git``
#: subprocesses rather than burn CPU, so the cap is about subprocess/fd pressure and
#: carries no core-derived term.
_MAX_SCAN_WORKERS = 32


def _scan_workers(clone_count: int) -> int:
    """A bounded worker count for ``clone_count`` clones (always ``>= 1``)."""
    return max(1, min(_MAX_SCAN_WORKERS, clone_count))


#: The marker that makes a directory a Tree; a directory without one is skipped.
_GIT_MARKER = ".git"


@dataclass(frozen=True)
class TreeRecord:
    """A snapshot of one Tree's on-disk state — the row the ``list`` verb renders.

    - ``branch`` — ``None`` on a detached/unborn HEAD; ``base`` — the branch's
      upstream tracking ref, ``None`` when it has none.
    - ``ahead`` / ``behind`` — against the upstream (``0`` each when there is none).
    - ``unpushed_shas`` — commits on ``HEAD`` that exist on NO remote, or ``None``
      when unreadable. Distinct from ``ahead``, which reads ``0`` for a branch with no
      upstream. The count is the derived :attr:`unpushed` property.
    - ``mtime`` — the clone ROOT directory's mtime, a DISPLAY signal only: it does not
      observe an edit under ``src/``.
    - ``newest_mtime`` — the newest mtime of any FILE in the clone, build/env dirs
      pruned; the reclaim signal. ``None`` means unreadable, which reads as ACTIVE.
    - ``last_commit`` — ``HEAD``'s committer timestamp, or ``None`` when unreadable.
      A reclaim input, but only ever a KEEPING one.
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
        """How many commits exist on no remote — ``None`` when unreadable."""
        return None if self.unpushed_shas is None else len(self.unpushed_shas)


def scan(root: str | Path) -> list[TreeRecord]:
    """Walk ``root`` and return a :class:`TreeRecord` for every Tree clone under it.

    The walk does not descend into a clone once found; non-clone directories are
    skipped and a missing or empty root yields ``[]``. The per-clone reads fan out
    across a bounded thread pool (each is a local ``git`` subprocess) and the results
    are sorted by path, so the listing is deterministic regardless of completion
    order. Nothing is mutated.
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
            dirnames[:] = []
            continue

    if not clone_dirs:
        return []

    with ThreadPoolExecutor(max_workers=_scan_workers(len(clone_dirs))) as pool:
        records = list(pool.map(_read_record, clone_dirs))
    records.sort(key=lambda record: record.path)
    logger.debug(
        "tree scan read %d Tree(s) under %s in %dms",
        len(records),
        base,
        int((time.monotonic() - started) * 1000),
    )
    return records


def _read_record(path: Path) -> TreeRecord:
    """Snapshot one clone at ``path`` — a purely local read, no network call."""
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
