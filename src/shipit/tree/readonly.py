"""``tree/readonly`` — the per-Run, read-only (reviewer) Tree (ADR-0018 / ADR-0074).

A Tree comes in two modes. The write Tree (:mod:`shipit.tree.create`) is one per
write-Run: ``clone --reference --dissociate`` + ``.treeinclude`` + pixi/sccache,
read-write. A **read-only Tree** is the cheap reviewer variant: **clone +
``git checkout`` + ``git submodule sync/update --init --recursive`` only** — NO
``.treeinclude``, NO pixi/provisioning — then the working tree is ``chmod``'d
read-only. Submodules ARE populated here too (#485): a dissociated clone leaves them
as empty gitlinks, and a reviewer reading a PR that touches submodule-backed content
(lex's ``comms/specs``) must see it — the reviewer reads the same complete checkout
CI builds (``submodules: recursive``).

**Read-only Trees are PER-RUN (ADR-0074).** ADR-0018's write/read-only *mode*
distinction stands — a reviewer still gets a ``chmod``'d read-only clone — but the
*sharing* is gone: every reviewer Run gets its OWN flat Tree
(``<root>/<repo>-<agent>-<timestamp>-<id>``, :func:`shipit.tree.layout.tree_dir`),
never a deterministic ``(repo, branch)`` leaf that two reviewers deduplicate onto.
A per-Run leaf is unique (its ``<id>`` is a fresh UUID), so there is no co-tenant to
race, no reuse-vs-refresh decision, and no acquisition stamp: a reviewer clone is
written at create like any write Tree, so its own files date it (ADR-0072), and it
is reclaimed on measured activity like every other Tree. Dropping sharing is what
lets this module be a clean, one-shot variant of the write path rather than a
concurrency-managed shared cache.

This is a clean variant of the write path, not a fork: it reuses the same
:mod:`shipit.gh` git boundary and the :class:`~shipit.tree.create.Tree` summary,
and skips exactly the two write-only steps (include + provision). The
``chmod``-read-only is a guardrail, not the security boundary (ADR-0018): it
catches an accidental write.
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
    """The resolved coordinates for a per-Run read-only Tree: where, and on what branch.

    Unlike a write :class:`~shipit.tree.layout.TreePlan` there is no ``base``: a
    reviewer checks out an EXISTING remote branch (the PR head), it does not cut a
    new one. ``dir`` is the flat per-Run leaf (ADR-0074), unique per reviewer Run.
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

    ``repo`` is the :class:`shipit.identity.Repo` value object — already canonical
    (lowercased owner/name), so its NAME leads the flat leaf regardless of how its
    slug was cased at the source (ADR-0024). The dir is the single flat shape every
    Tree uses — ``<root>/<repo>-<agent>-<timestamp>-<id>`` (:func:`tree_dir`) — with
    no ``review`` segment and no shared branch-keyed leaf: a reviewer clone is per-Run
    now (ADR-0074), so its ``<id>`` is this Run's own UUID and two reviewers on the
    same head never resolve to the same dir.

    The ``branch`` is kept VERBATIM for the checkout — it is the real remote branch
    name, e.g. ``TRE03/WS03``. A branch that is empty / whitespace is rejected with
    :class:`ValueError`: it would yield an unusable empty checkout target. (The dir
    leaf no longer derives from the branch, so branch sanitization is gone with the
    sharing that needed it.)
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

    The reviewer-Run substrate (ADR-0018): clone ``--reference --dissociate``
    (ADR-0014), ``git fetch``, ``git checkout`` the EXISTING branch (no ``-b``, no
    base), ``git submodule sync + update --init --recursive``
    (:func:`shipit.git.submodule_update_init`, #485/#486 — a reviewer over
    submodule-backed content must see the real files), then ``chmod`` the working
    tree read-only. The two write-only steps — ``.treeinclude`` copy and
    pixi/sccache provisioning — are deliberately skipped: a reviewer reads, it never
    builds.

    Materialization is atomic from the caller's view (ADR-0014): the clone is built
    in a sibling ``*.tmp-<pid>`` path and ``os.rename``'d into the leaf, so a failure
    after the clone removes the half-built temp before the error propagates and never
    leaves a partial Tree. The leaf is per-Run (a fresh UUID, ADR-0074), so there is
    no co-tenant to race and no shared slot to reuse — a pre-existing ``dest`` is a
    programming error (a reused id), refused with :class:`FileExistsError` rather than
    cloned into or deleted.
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
        # Populate submodules before the read-only chmod (#485): a reviewer reading a PR
        # over submodule-backed content must see the real files, not an empty gitlink.
        # Run BEFORE chmod_readonly so git can still write the submodule working trees.
        git.submodule_update_init(cwd=str(tmp))
        chmod_readonly(tmp)
    except BaseException:
        # Propagating failure at ERROR with the exception attached (spray
        # convention), plus the temp-clone rollback the atomic two-step performs.
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
        # A per-Run leaf cannot legitimately collide (its id is a fresh UUID), so a
        # rename that lands on an existing dir is a real error: discard the temp clone
        # and fail loud rather than silently adopting a stranger's directory.
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
    """Strip the WRITE bits from every working dir AND file under ``tree_dir`` (skip ``.git``).

    The ADR-0018 guardrail: a reviewer clone must fail an accidental write fast.
    **Directories are made read-only too, not just files** — on Unix the right to
    create or delete an entry is governed by the *containing directory's* mode, so a
    reviewer with ``Bash`` could otherwise add files or delete the read-only ones;
    clearing ``w`` on the dirs (and the Tree root itself) closes that hole. ``.git``
    is skipped so git's own reads stay unaffected, and :func:`remove_tree` restores
    the bits when the Tree is later reclaimed.

    **Symlinks are skipped**, not followed: ``stat``/``chmod`` follow a symlink, so a
    link in the checkout could otherwise re-permission a target OUTSIDE the Tree (and a
    broken link would raise). Read and execute bits are preserved; only ``w`` is cleared.
    """
    for path in _guarded_paths(tree_dir):
        try:
            path.chmod(path.stat().st_mode & ~_WRITE_BITS)
        except OSError:
            # A path that vanished mid-walk is not worth failing the whole provision
            # over — the guardrail is best-effort, not a barrier.
            continue


def _guarded_paths(tree_dir: str | os.PathLike[str]) -> list[Path]:
    """The dirs + files the read-only guard covers: the Tree root, every subdir, every
    file, EXCLUDING ``.git`` and any symlink.

    Returned root-first so a caller restoring write bits re-permissions a directory
    before the entries it contains; :func:`chmod_readonly` is order-insensitive (clearing
    ``w`` never blocks read traversal). Symlinks are filtered here so the guard never
    follows a link out of the Tree.
    """
    root = Path(tree_dir)
    paths: list[Path] = []
    if not root.is_symlink():
        paths.append(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if _GIT_DIR in dirnames:
            dirnames.remove(_GIT_DIR)  # never descend into / re-permission the repo db
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            paths.append(path)
    return paths


def remove_tree(tree_dir: str | os.PathLike[str]) -> bool:
    """``rmtree`` a Tree, restoring write perms on any read-only dir/file as it goes.

    The read-only guard (:func:`chmod_readonly`) clears the write bit on a reviewer
    Tree's dirs AND files; on Unix a directory must be writable to delete its entries, so
    a plain ``shutil.rmtree`` raises ``PermissionError`` partway through. This passes an
    error handler that restores the write bit on the offending path (and its parent dir)
    and retries the failed unlink/rmdir, so reclaim always completes — a writable write
    Tree takes the same path with no extra work.

    Returns ``True`` when a Tree was present and is now off disk, ``False`` when the path
    was already gone (a no-op). The boolean lets callers (``gc``) count only what they
    actually reclaimed rather than crediting a removal that never happened.
    """
    if not os.path.lexists(tree_dir):
        return False
    shutil.rmtree(tree_dir, onexc=_chmod_then_retry)
    # The one funnel every Tree reclaim passes through (`remove`, `gc`, temp
    # rollbacks), so removal — a lifecycle milestone whose only record was the
    # verb's print — is narrated here once, with the Tree it took off disk.
    logger.info("tree removed: %s", tree_dir, extra={"tree": str(tree_dir)})
    return True


def _chmod_then_retry(func, path, _exc):  # type: ignore[no-untyped-def]
    """``rmtree`` error handler: re-grant write on the failed path + its parent, retry.

    ``rmtree`` calls this when an ``unlink``/``rmdir`` fails — typically because the path
    or its containing directory is read-only (the reviewer guard). Restoring the write
    bit on both and re-running the failed op lets the removal proceed; a symlink path is
    not de-referenced (only its parent dir's mode governs deleting it).
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
