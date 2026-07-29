"""``tree/activity`` — how long since anyone touched this Tree, measured by a walk.

See docs/adr/0072-tree-reclaim-is-activity-based.md.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("shipit.tree")

#: Directories the activity walk never descends into: they are enormous (``.pixi``
#: alone is ~97% of a Tree's file count) and their mtimes are build/env churn, not
#: agent activity.
PRUNE_DIRS = frozenset(
    {
        ".git",
        ".pixi",
        "node_modules",
        "target",
        ".venv",
        "dist",
        "build",
        "__pycache__",
    }
)


def _reraise(error: OSError) -> None:
    """``os.walk`` ``onerror`` hook: re-raise instead of silently skipping an
    unreadable directory, so a partial maximum never passes for a real answer."""
    raise error


def newest_mtime(path: str | Path) -> float | None:
    """The newest FILE mtime under ``path`` (epoch seconds) — ``None`` when unreadable.

    Walks with :data:`PRUNE_DIRS` pruned. Symlinks are read with ``lstat`` (the link's
    own stamp, never its target's). Directory mtimes are not considered. ``None`` —
    never a stale-looking number — when the path is not a directory, any read raises
    :class:`OSError`, or no eligible file was found; every caller reads ``None`` as
    "recently active, keep".
    """
    root = Path(path)
    newest: float | None = None
    try:
        if not root.is_dir():
            logger.debug("tree activity: %s is not a directory; idle unreadable", root)
            return None
        for dirpath, dirnames, filenames in os.walk(root, onerror=_reraise):
            dirnames[:] = [name for name in dirnames if name not in PRUNE_DIRS]
            for name in filenames:
                stamp = os.lstat(os.path.join(dirpath, name)).st_mtime
                if newest is None or stamp > newest:
                    newest = stamp
    except OSError:
        logger.debug("tree activity: %s could not be walked", root, exc_info=True)
        return None
    if newest is None:
        logger.debug(
            "tree activity: %s yielded no eligible file; idle unreadable", root
        )
    return newest
