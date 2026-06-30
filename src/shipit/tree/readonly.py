"""``tree/readonly`` — the shared, read-only (reviewer) Tree (ADR-0018).

A Tree comes in two modes. The write Tree (:mod:`shipit.tree.create`) is one per
write-Run: ``clone --reference --dissociate`` + ``.treeinclude`` + pixi/sccache,
read-write. A **read-only Tree** is the cheap reviewer variant: **clone +
``git checkout`` only** — NO ``.treeinclude``, NO pixi/provisioning — then the
working files are ``chmod``'d read-only. It is **shared per ``(repo, branch)``**:
N reviewers on one PR head share ONE clone (safe precisely because none mutate it),
so the dir leaf is deterministic — ``<root>/<org>/<repo>/review/<branch>``, with NO
agent hash — and a second reviewer on the same head REUSES the clone instead of
re-cloning.

This is a clean variant of the write path, not a fork: it reuses the same
:mod:`shipit.gh` git boundary and the :class:`~shipit.tree.create.Tree` summary,
and skips exactly the two write-only steps (include + provision). The
``chmod``-read-only is a guardrail, not the security boundary (ADR-0018): it
catches an accidental write and keeps a shared clone trustworthy for its co-tenants.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import gh
from .create import Tree
from .layout import REVIEW_KIND, central_root, sanitize_slug

#: The ``.git`` marker dir, skipped when ``chmod``-ing the working files read-only:
#: git needs its own metadata writable for reads, and the read-only guardrail is
#: about the *working* files a reviewer might accidentally edit, not the repo db.
_GIT_DIR = ".git"

#: Bits removed to make a file read-only — the owner/group/other WRITE bits. The
#: read bits (and any execute bits) are left untouched, so a reviewer can still
#: read every file and run a tracked script while a write fails fast.
_WRITE_BITS = 0o222


@dataclass(frozen=True)
class ReadOnlyPlan:
    """The resolved coordinates for a shared read-only Tree: where, and on what branch.

    Unlike a write :class:`~shipit.tree.layout.TreePlan` there is no ``base``: a
    reviewer checks out an EXISTING remote branch (the PR head), it does not cut a
    new one. ``dir`` is shared per ``(repo, branch)`` (no agent hash), so two
    reviewers on the same head resolve to the same leaf.
    """

    dir: Path
    branch: str


def readonly_plan(
    *, org: str, repo: str, branch: str, root: Path | None = None
) -> ReadOnlyPlan:
    """Resolve a shared read-only Tree's ``(dir, branch)`` for ``(org, repo, branch)``. Pure.

    The dir is ``<root>/<org>/<repo>/review/<sanitized-branch>`` — the ``review``
    kind (:data:`~shipit.tree.layout.REVIEW_KIND`) with a leaf derived ONLY from the
    branch, so it is **deterministic and hash-free**: every reviewer on the same
    ``(repo, branch)`` resolves to the identical leaf and thus shares one clone. The
    ``branch`` is kept VERBATIM for the checkout (it is the real remote branch name,
    e.g. ``TRE03/WS03``); only the dir leaf is sanitized (``/`` → ``-``, lowercased)
    so an arbitrary branch maps to one safe path segment.

    A branch that sanitizes to nothing (empty / whitespace / all-separators) is
    rejected with :class:`ValueError`, the same invariant the write planner pins:
    it would yield an unusable empty checkout target and a bare ``review/`` leaf.
    """
    leaf = sanitize_slug(branch)
    if not leaf:
        raise ValueError(
            "tree.readonly.readonly_plan: branch must contain at least one "
            f"alphanumeric character (it becomes the review-Tree dir leaf); got "
            f"{branch!r}, which sanitizes to an empty name."
        )
    base_root = root if root is not None else central_root()
    directory = Path(base_root) / org / repo / REVIEW_KIND / leaf
    return ReadOnlyPlan(dir=directory, branch=branch)


def create_readonly(plan: ReadOnlyPlan, *, source_repo: str, github_url: str) -> Tree:
    """Materialize (or REUSE) the shared read-only Tree ``plan`` and return its summary.

    The reviewer-Run substrate (ADR-0018): if the shared leaf already holds a clone
    (a reviewer is, or was, already on this ``(repo, branch)``), it is **reused** —
    returned as-is, NOT re-cloned — which is what makes a second reviewer on the same
    head cheap. Otherwise the leaf is provisioned the read-only way: clone
    ``--reference --dissociate`` (ADR-0014), ``git fetch``, ``git checkout`` the
    EXISTING branch (no ``-b``, no base), then ``chmod`` the working files read-only.
    The two write-only steps — ``.treeinclude`` copy and pixi/sccache provisioning —
    are deliberately skipped: a reviewer reads, it never builds.

    Materialization is atomic from the caller's view, exactly like
    :func:`shipit.tree.create.create`: if any step after the clone fails the
    half-built leaf is removed before the error propagates, so a failed create never
    leaves a partial clone for the next reviewer to reuse. A pre-existing leaf that
    is NOT a clone (no ``.git``) is refused with :class:`FileExistsError` rather than
    cloned into or deleted.
    """
    dest = plan.dir
    if dest.exists():
        if (dest / _GIT_DIR).exists():
            # Shared reuse: a reviewer is already (or was) on this (repo, branch).
            # Return the existing clone untouched — the whole point of sharing.
            return _summary(dest, plan.branch)
        raise FileExistsError(
            f"review tree dir already exists but is not a clone: {dest}; refusing "
            "to clone into or delete a non-Tree directory in the shared review slot."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        gh.git_clone_dissociated(github_url, str(dest), reference=source_repo)
        gh.git_fetch(cwd=str(dest))
        gh.git_checkout(plan.branch, cwd=str(dest))
        chmod_readonly(dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    return _summary(dest, plan.branch)


def _summary(dest: Path, branch: str) -> Tree:
    """The READY summary for a read-only Tree: its ``origin/<branch>`` is the base."""
    return Tree(path=str(dest), branch=branch, base=f"origin/{branch}")


def chmod_readonly(tree_dir: str | os.PathLike[str]) -> None:
    """Strip the WRITE bits from every working file under ``tree_dir`` (skip ``.git``).

    The ADR-0018 guardrail: a shared clone must stay trustworthy for its co-tenant
    reviewers, so an accidental write fails fast. Only *files* are made read-only —
    directories keep their bits so the Tree is still traversable and a later
    ``rm -rf`` reclaim works — and ``.git`` is skipped so git's own reads are
    unaffected. Read and execute bits are preserved; only ``w`` is cleared.
    """
    root = Path(tree_dir)
    for dirpath, dirnames, filenames in os.walk(root):
        if _GIT_DIR in dirnames:
            dirnames.remove(_GIT_DIR)  # never descend into the repo database
        for name in filenames:
            path = Path(dirpath) / name
            try:
                mode = path.stat().st_mode
                path.chmod(mode & ~_WRITE_BITS)
            except OSError:
                # A symlink or a file that vanished mid-walk is not worth failing the
                # whole provision over — the guardrail is best-effort, not a barrier.
                continue
