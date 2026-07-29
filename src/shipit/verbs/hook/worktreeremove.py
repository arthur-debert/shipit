"""``shipit hook worktreeremove`` — fast-path reclaim of a clean ephemeral Tree.

Best-effort and fail-open; ``tree gc`` is the load-bearing cleanup.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TextIO

import click

from ... import git
from ...tree.layout import central_root, parse_flat_leaf
from ...tree.readonly import remove_tree

logger = logging.getLogger("shipit.hook")

_PATH_FIELDS = ("path", "worktree_path", "cwd")


@click.command(name="worktreeremove")
def cmd() -> None:
    """Reclaim a clean ephemeral session Tree on session exit (best-effort)."""
    raise SystemExit(run())


def run(stdin: TextIO | None = None) -> int:
    """Parse stdin → gate → remove the Tree. Returns 0 always."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"WorktreeRemove payload is not an object: {payload!r}")
        tree = _target_tree(payload)
        if tree is None:
            logger.debug(
                "worktreeremove: no ephemeral Tree under the central root in the "
                "payload — nothing to reclaim"
            )
            return 0
        blocker = _removal_blocker(tree)
        if blocker is not None:
            logger.debug(
                "worktreeremove: %s has %s — left for the gc ladder", tree, blocker
            )
            return 0
        remove_tree(tree)
        logger.debug("worktreeremove: reclaimed %s", tree)
    except Exception:  # noqa: BLE001 — fail-open: the gc ladder is the load-bearing cleanup.
        logger.warning(
            "worktreeremove hook failed open (nothing removed)", exc_info=True
        )
    return 0


def _target_tree(payload: dict[str, object]) -> Path | None:
    """The Tree the payload names, or None when it names no reclaimable Tree: only a direct child of the central root whose name parses as a flat leaf and whose ``.git`` is a directory qualifies."""
    root = central_root().resolve()
    for field in _PATH_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value).resolve()
        if candidate.parent != root or parse_flat_leaf(candidate.name) is None:
            continue
        if not (candidate / ".git").is_dir():
            continue
        return candidate
    return None


def _removal_blocker(tree: Path) -> str | None:
    """Why ``tree`` must not be fast-path removed, or None when it is safe; an unreadable unpushed list blocks too."""
    cwd = str(tree)
    if git.status_porcelain(cwd=cwd):
        return "uncommitted changes"
    unpushed = git.unpushed_shas(cwd=cwd)
    if unpushed is None:
        return "an unreadable unpushed-commit list"
    if unpushed:
        plural = "s" if len(unpushed) != 1 else ""
        return f"{len(unpushed)} unpushed commit{plural}"
    return None
