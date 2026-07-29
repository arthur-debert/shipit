"""``tree/readonly`` — the per-Run, read-only (reviewer) Tree.

Clone + checkout + submodules only — no ``.treeinclude``, no provisioning — then the
working tree is ``chmod``'d read-only as a guardrail, not a security boundary.
See docs/adr/0018-read-only-trees.md.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import events, git
from ..identity import Repo
from .create import Tree
from .layout import tree_dir

logger = logging.getLogger("shipit.tree")

#: The ``.git`` marker dir, skipped when ``chmod``-ing: git needs its own metadata
#: writable even for reads.
_GIT_DIR = ".git"

#: Bits removed to make a path read-only — the owner/group/other WRITE bits only.
_WRITE_BITS = 0o222


@dataclass(frozen=True)
class ReadOnlyPlan:
    """Where a per-Run read-only Tree goes and what branch it checks out.

    There is no ``base``: a reviewer checks out an EXISTING remote branch rather than
    cutting a new one.
    """

    dir: Path
    branch: str


def readonly_plan(
    *,
    repo: Repo,
    branch: str,
    agent: str,
    created: str,
    tree_id: str,
    root: Path | None = None,
) -> ReadOnlyPlan:
    """Resolve a per-Run read-only Tree's ``(dir, branch)``. Pure.

    The dir is the same flat per-Run leaf every Tree uses, so two reviewers on one
    head never resolve to the same directory. ``branch`` is kept verbatim for the
    checkout; empty/whitespace raises :class:`ValueError`.
    """
    if not branch or not branch.strip():
        raise ValueError(
            "tree.readonly.readonly_plan: branch must be a non-empty remote branch "
            f"name (the reviewer checks out an existing head); got {branch!r}."
        )
    directory = tree_dir(repo, agent, created, tree_id, root)
    return ReadOnlyPlan(dir=directory, branch=branch)


def create_readonly(plan: ReadOnlyPlan, *, source_repo: str, github_url: str) -> Tree:
    """Materialize the per-Run read-only Tree ``plan`` and return its summary.

    Clone (dissociated), fetch, check out the existing branch, populate submodules,
    then ``chmod`` read-only. Atomic from the caller's view: the clone is built in a
    sibling ``*.tmp-<pid>`` path and renamed into the leaf, and a failure removes the
    temp before propagating. A pre-existing ``dest`` means a reused id and raises
    :class:`FileExistsError` rather than being cloned into or deleted.
    """
    dest = plan.dir
    if dest.exists():
        raise FileExistsError(
            f"read-only tree dir already exists: {dest}; a per-Run reviewer leaf "
            "carries a fresh UUID, so a collision means a reused id — refusing to "
            "clone into or delete an existing directory."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    remove_tree(tmp)  # clear any leftover temp from a crashed prior Run
    started = time.monotonic()
    try:
        git.clone_dissociated(github_url, str(tmp), reference=source_repo)
        git.fetch(cwd=str(tmp))
        git.checkout(plan.branch, cwd=str(tmp))
        # Before the read-only chmod, while git can still write the submodule trees.
        git.submodule_update_init(cwd=str(tmp))
        chmod_readonly(tmp)
    except BaseException:
        logger.error(
            "read-only tree create failed after %dms; removing temp clone %s",
            int((time.monotonic() - started) * 1000),
            tmp,
            exc_info=True,
            extra={"tree": str(dest)},
        )
        remove_tree(tmp)
        raise

    try:
        os.rename(tmp, dest)
    except OSError:
        remove_tree(tmp)
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    events.emit(
        logger,
        "tree.created",
        "read-only tree created at %s (branch %s) in %dms",
        dest,
        plan.branch,
        duration_ms,
        extra={"tree": str(dest), "duration_ms": duration_ms},
    )
    return _summary(dest, plan.branch)


def _summary(dest: Path, branch: str) -> Tree:
    """The READY summary for a read-only Tree: its ``origin/<branch>`` is the base."""
    return Tree(path=str(dest), branch=branch, base=f"origin/{branch}")


def chmod_readonly(tree_dir: str | os.PathLike[str]) -> None:
    """Strip the WRITE bits from every working dir AND file under ``tree_dir``.

    Directories are covered too, not just files: on Unix the right to create or delete
    an entry is governed by the containing directory's mode. ``.git`` and symlinks are
    skipped — ``chmod`` follows a link, which could re-permission a target outside the
    Tree. Read and execute bits are preserved. Best-effort: a path that vanishes
    mid-walk is skipped, not fatal.
    """
    for path in _guarded_paths(tree_dir):
        try:
            path.chmod(path.stat().st_mode & ~_WRITE_BITS)
        except OSError:
            continue


def _guarded_paths(tree_dir: str | os.PathLike[str]) -> list[Path]:
    """The dirs + files the read-only guard covers, excluding ``.git`` and symlinks.

    Root-first, so a caller restoring write bits re-permissions a directory before its
    entries.
    """
    root = Path(tree_dir)
    paths: list[Path] = []
    if not root.is_symlink():
        paths.append(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if _GIT_DIR in dirnames:
            dirnames.remove(_GIT_DIR)
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            paths.append(path)
    return paths


def remove_tree(tree_dir: str | os.PathLike[str]) -> bool:
    """``rmtree`` a Tree, restoring write perms on any read-only dir/file as it goes.

    Returns ``True`` when a Tree was present and is now off disk, ``False`` when the
    path was already gone, so callers count only what they actually reclaimed. This is
    the one funnel every Tree reclaim passes through.
    """
    if not os.path.lexists(tree_dir):
        return False
    shutil.rmtree(tree_dir, onexc=_chmod_then_retry)
    logger.info("tree removed: %s", tree_dir, extra={"tree": str(tree_dir)})
    return True


def _chmod_then_retry(func, path, _exc):  # type: ignore[no-untyped-def]
    """``rmtree`` error handler: re-grant write on the failed path + its parent, retry.

    A symlink path is not de-referenced — only its parent dir's mode governs deleting
    it.
    """
    parent = os.path.dirname(path)
    try:
        os.chmod(parent, os.stat(parent).st_mode | _WRITE_BITS)
    except OSError:
        pass
    if not os.path.islink(path):
        try:
            os.chmod(path, os.stat(path).st_mode | _WRITE_BITS)
        except OSError:
            pass
    func(path)
