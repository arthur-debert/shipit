"""``tree/readonly`` — the shared, read-only (reviewer) Tree (ADR-0018).

A Tree comes in two modes. The write Tree (:mod:`shipit.tree.create`) is one per
write-Run: ``clone --reference --dissociate`` + ``.treeinclude`` + pixi/sccache,
read-write. A **read-only Tree** is the cheap reviewer variant: **clone +
``git checkout`` only** — NO ``.treeinclude``, NO pixi/provisioning — then the
working tree is ``chmod``'d read-only. It is **shared per ``(repo, branch)``**:
N reviewers on one PR head share ONE clone (safe precisely because none mutate it),
so the dir leaf is deterministic — ``<root>/<org>/<repo>/review/<branch-slug>-<hash>``,
with NO agent hash (the ``<hash>`` is a stable digest of the branch name that keeps
slug-colliding branches apart, not a per-Run hash) — and a second reviewer on the same
head REUSES the clone (refreshed to the current head) instead of re-cloning.

This is a clean variant of the write path, not a fork: it reuses the same
:mod:`shipit.gh` git boundary and the :class:`~shipit.tree.create.Tree` summary,
and skips exactly the two write-only steps (include + provision). The
``chmod``-read-only is a guardrail, not the security boundary (ADR-0018): it
catches an accidental write and keeps a shared clone trustworthy for its co-tenants.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import gh
from .create import Tree
from .layout import REVIEW_KIND, central_root, sanitize_slug

#: Length (hex chars) of the branch-name disambiguator suffixed on a review-Tree leaf.
#: A short ``blake2b`` digest of the VERBATIM branch keeps the sharing key deterministic
#: (same branch → same leaf) while separating branches that sanitize to the same slug
#: (``feat/a-b`` vs ``feat/a/b`` both slug to ``feat-a-b`` — the hash differs).
_BRANCH_HASH_LEN = 8

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

    The dir is ``<root>/<org>/<repo>/review/<sanitized-branch>-<branch-hash>`` — the
    ``review`` kind (:data:`~shipit.tree.layout.REVIEW_KIND`) with a leaf derived ONLY
    from the branch, so it is **deterministic and agent-hash-free**: every reviewer on
    the same ``(repo, branch)`` resolves to the identical leaf and thus shares one
    clone. The ``branch`` is kept VERBATIM for the checkout (it is the real remote
    branch name, e.g. ``TRE03/WS03``); the dir leaf is the sanitized branch (``/`` →
    ``-``, lowercased) plus a short hash of the verbatim branch.

    The hash suffix is load-bearing, not cosmetic: sanitization is lossy, so distinct
    branches can collapse to one slug — ``feat/a-b`` and ``feat/a/b`` both sanitize to
    ``feat-a-b`` — and without a disambiguator one PR's reviewer would REUSE another
    PR's checkout and review the wrong diff. The hash is taken over the verbatim branch,
    so it is stable per branch (sharing is preserved) yet differs the moment the real
    branch differs (collision is broken).

    A branch that sanitizes to nothing (empty / whitespace / all-separators) is
    rejected with :class:`ValueError`, the same invariant the write planner pins:
    it would yield an unusable empty checkout target and a bare ``review/`` leaf.
    """
    slug = sanitize_slug(branch)
    if not slug:
        raise ValueError(
            "tree.readonly.readonly_plan: branch must contain at least one "
            f"alphanumeric character (it becomes the review-Tree dir leaf); got "
            f"{branch!r}, which sanitizes to an empty name."
        )
    leaf = f"{slug}-{_branch_hash(branch)}"
    base_root = root if root is not None else central_root()
    directory = Path(base_root) / org / repo / REVIEW_KIND / leaf
    return ReadOnlyPlan(dir=directory, branch=branch)


def _branch_hash(branch: str) -> str:
    """A short, stable hex digest of the VERBATIM branch — the leaf disambiguator.

    Deterministic per branch string (so the shared-clone key is preserved) and
    sensitive to the pre-sanitization name (so branches that slug-collide get distinct
    leaves). :func:`hashlib.blake2b` with a small ``digest_size`` keeps the suffix short.
    """
    digest = hashlib.blake2b(branch.encode("utf-8"), digest_size=_BRANCH_HASH_LEN // 2)
    return digest.hexdigest()


def create_readonly(plan: ReadOnlyPlan, *, source_repo: str, github_url: str) -> Tree:
    """Materialize (or REUSE) the shared read-only Tree ``plan`` and return its summary.

    The reviewer-Run substrate (ADR-0018): if the shared leaf already holds a clone
    (a reviewer is, or was, already on this ``(repo, branch)``), it is **reused** —
    but NOT served stale: the clone is REFRESHED to the current remote head
    (:func:`_refresh_readonly`) before it is returned, because the PR head may have
    advanced since the first reviewer cloned and a co-tenant must never review an old
    commit. Otherwise the leaf is provisioned the read-only way: clone
    ``--reference --dissociate`` (ADR-0014), ``git fetch``, ``git checkout`` the
    EXISTING branch (no ``-b``, no base), then ``chmod`` the working tree read-only.
    The two write-only steps — ``.treeinclude`` copy and pixi/sccache provisioning —
    are deliberately skipped: a reviewer reads, it never builds.

    A fresh clone is built in a sibling ``*.tmp-<pid>`` path and ``os.rename``'d into
    the shared leaf ATOMICALLY (ADR-0014). The two-step is what makes concurrent
    reviewer Runs for one ``(repo, branch)`` safe: there is no window where a co-tenant
    sees a half-built leaf, and if another Run wins the race (the rename lands on an
    existing leaf) this Run discards its temp clone and falls back to the reuse path —
    so a TOCTOU between the existence check and the clone resolves to sharing, never a
    corrupted slot.

    A pre-existing leaf that is NOT a clone (no ``.git``) is refused with
    :class:`FileExistsError` rather than cloned into or deleted.
    """
    dest = plan.dir
    if dest.exists():
        return _reuse_or_refuse(dest, plan.branch)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    remove_tree(tmp)  # clear any leftover temp from a crashed prior Run
    try:
        gh.git_clone_dissociated(github_url, str(tmp), reference=source_repo)
        gh.git_fetch(cwd=str(tmp))
        gh.git_checkout(plan.branch, cwd=str(tmp))
        chmod_readonly(tmp)
    except BaseException:
        remove_tree(tmp)
        raise

    try:
        os.rename(tmp, dest)
    except OSError:
        # A concurrent reviewer won the race and created the leaf first: discard our
        # temp clone and treat it as the shared-reuse case (refresh + return theirs).
        remove_tree(tmp)
        return _reuse_or_refuse(dest, plan.branch)

    return _summary(dest, plan.branch)


def _reuse_or_refuse(dest: Path, branch: str) -> Tree:
    """Reuse the existing shared leaf (refreshed to the current head), or refuse a non-clone.

    The shared-reuse decision: a leaf that holds a clone (``.git`` present) is REFRESHED
    to the current remote head and re-guarded, then returned — never served as-is, since
    a stale head would have a co-tenant review the wrong commit. A leaf that is NOT a
    clone is a stray dir squatting the shared slot and is refused loud.
    """
    if (dest / _GIT_DIR).exists():
        _refresh_readonly(dest, branch)
        return _summary(dest, branch)
    raise FileExistsError(
        f"review tree dir already exists but is not a clone: {dest}; refusing "
        "to clone into or delete a non-Tree directory in the shared review slot."
    )


def _refresh_readonly(dest: Path, branch: str) -> None:
    """Re-pin a reused shared clone to the current remote head, re-applying the guard.

    The clone's working tree is ``chmod``'d read-only, so it must first be made writable
    (:func:`chmod_writable`) before git can rewrite it; then ``fetch`` + ``checkout`` +
    ``reset --hard origin/<branch>`` move it to the CURRENT PR head, and the read-only
    guard is re-applied so a head that advanced under the first reviewer never leaves a
    co-tenant on a stale commit OR with stale (writable) permissions.
    """
    chmod_writable(dest)
    gh.git_fetch(cwd=str(dest))
    gh.git_checkout(branch, cwd=str(dest))
    gh.git_reset_hard(f"origin/{branch}", cwd=str(dest))
    chmod_readonly(dest)


def _summary(dest: Path, branch: str) -> Tree:
    """The READY summary for a read-only Tree: its ``origin/<branch>`` is the base."""
    return Tree(path=str(dest), branch=branch, base=f"origin/{branch}")


def chmod_readonly(tree_dir: str | os.PathLike[str]) -> None:
    """Strip the WRITE bits from every working dir AND file under ``tree_dir`` (skip ``.git``).

    The ADR-0018 guardrail: a shared clone must stay trustworthy for its co-tenant
    reviewers, so an accidental write fails fast. **Directories are made read-only too,
    not just files** — on Unix the right to create or delete an entry is governed by the
    *containing directory's* mode, so a reviewer with ``Bash`` could otherwise add files
    or delete the read-only ones; clearing ``w`` on the dirs (and the Tree root itself)
    closes that hole. ``.git`` is skipped so git's own reads stay unaffected, and
    :func:`remove_tree` restores the bits when the Tree is later reclaimed.

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


def chmod_writable(tree_dir: str | os.PathLike[str]) -> None:
    """Restore the WRITE bits on every working dir AND file under ``tree_dir`` (skip ``.git``).

    The inverse of :func:`chmod_readonly` — used before re-pinning a reused shared clone
    to a new head (git must be able to rewrite the working tree). The Tree root is made
    writable FIRST so its entries can then be re-permissioned. Symlinks are skipped for
    the same reason as in :func:`chmod_readonly`.
    """
    for path in _guarded_paths(tree_dir):
        try:
            path.chmod(path.stat().st_mode | _WRITE_BITS)
        except OSError:
            continue


def _guarded_paths(tree_dir: str | os.PathLike[str]) -> list[Path]:
    """The dirs + files the read-only guard covers: the Tree root, every subdir, every
    file, EXCLUDING ``.git`` and any symlink.

    Returned root-first so a caller restoring write bits re-permissions a directory
    before the entries it contains; :func:`chmod_readonly` is order-insensitive (clearing
    ``w`` never blocks read traversal). Symlinks are filtered here so neither guard
    direction follows a link out of the Tree.
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
