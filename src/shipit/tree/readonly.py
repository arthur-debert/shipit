"""``tree/readonly`` — the shared, read-only (reviewer) Tree (ADR-0018).

A Tree comes in two modes. The write Tree (:mod:`shipit.tree.create`) is one per
write-Run: ``clone --reference --dissociate`` + ``.treeinclude`` + pixi/sccache,
read-write. A **read-only Tree** is the cheap reviewer variant: **clone +
``git checkout`` + ``git submodule sync/update --init --recursive`` only** — NO
``.treeinclude``, NO pixi/provisioning — then the working tree is ``chmod``'d
read-only. Submodules ARE populated here too (#485): a dissociated clone leaves them
as empty gitlinks, and a reviewer reading a PR that touches submodule-backed content
(lex's ``comms/specs``) must see it — the reviewer reads the same complete checkout
CI builds (``submodules: recursive``). It is **shared per ``(repo, branch)``**:
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

Every acquisition — a fresh clone and a reuse alike — leaves an ACTIVITY STAMP
(:data:`_ACQUIRED_STAMP`). Sharing is what makes it necessary: a per-Run Tree is
written at create, so its own files date it, but a shared leaf outlives every Run
that used it and a reviewer only ever READS the checkout it was handed. Nothing else
on the reuse path writes an eligible file either, so without the stamp reclaim would
date an aged shared leaf by its PR head's last movement and delete it under a
reviewer who acquired it a second ago (ADR-0072). ADR-0074 makes review Trees per-Run
and dissolves this along with the sharing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import events, git
from ..identity import Repo
from .create import Tree
from .layout import REVIEW_KIND, repo_dir, sanitize_slug

logger = logging.getLogger("shipit.tree")

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

#: The acquisition stamp: an empty file at the Tree ROOT, (re)touched every time a
#: reviewer Run takes this shared leaf (:func:`_stamp_acquisition`).
#:
#: It exists because acquiring a shared review Tree was, uniquely, ACTIVITY THAT LEFT
#: NO TRACE. Reclaim measures idle as the newest of the pruned activity walk
#: (:func:`shipit.tree.activity.newest_mtime`) and HEAD's commit stamp (ADR-0072), and
#: a reuse is invisible to BOTH. The walk misses every step of it: ``fetch`` writes only
#: under the pruned ``.git``; ``checkout``/``reset`` at an UNCHANGED head rewrite no
#: working file; ``chmod`` moves ctime, not mtime; and a reviewer only ever READS the
#: checkout it was handed. The commit stamp misses it for the same reason the head is
#: unchanged — a reviewer commits nothing. So a shared leaf dates to when its PR head
#: last moved, not to when a reviewer last used it, and a Tree cloned three days ago on
#: a quiet head, handed to a reviewer THIS SECOND, is clean, fully pushed and >48h idle:
#: removable, mid-review.
#:
#: The stamp fixes the EVENT, not the rule: it gives acquisition the filesystem trace
#: it always should have had, so the one measured signal keeps answering the one
#: question. That is deliberately not a fourth signal — ADR-0072 fixed the rule at
#: three, and a Tree kind or a lease read would be exactly the proxy it deleted.
#:
#: At the root (not in ``.git``) because the walk prunes ``.git``; git-EXCLUDED
#: because an untracked file at the root would read as ``dirty``, and a permanently
#: dirty Tree is a permanently unreclaimable one — the floor would swallow the whole
#: rule for every review Tree. ADR-0074 dissolves the need entirely: per-Run review
#: Trees are written at create, so their mtimes are their Run's own.
_ACQUIRED_STAMP = ".shipit-acquired"

#: Where git keeps a clone-local ignore list that needs no tracked ``.gitignore`` —
#: the one place to hide :data:`_ACQUIRED_STAMP` from ``git status`` without touching
#: a file the PR under review might itself be changing.
_GIT_EXCLUDE = "info/exclude"


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


def readonly_plan(*, repo: Repo, branch: str, root: Path | None = None) -> ReadOnlyPlan:
    """Resolve a shared read-only Tree's ``(dir, branch)`` for ``(repo, branch)``. Pure.

    ``repo`` is the :class:`shipit.identity.Repo` value object — already canonical
    (lowercased owner/name), so every reviewer resolves the same repo to the same
    namespace regardless of how its slug was cased at the source (ADR-0024). The dir
    is ``<root>/<owner>/<name>/review/<sanitized-branch>-<branch-hash>``
    (:func:`~shipit.tree.layout.repo_dir` + :data:`~shipit.tree.layout.REVIEW_KIND`)
    with a leaf derived ONLY from the branch, so it is **deterministic and
    agent-hash-free**: every reviewer on the same ``(repo, branch)`` resolves to the
    identical leaf and thus shares one clone. The ``branch`` is kept VERBATIM for the
    checkout (it is the real remote branch name, e.g. ``TRE03/WS03``); the dir leaf
    is the sanitized branch (``/`` → ``-``, lowercased) plus a short hash of the
    verbatim branch.

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
    directory = repo_dir(repo, root) / REVIEW_KIND / leaf
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
    EXISTING branch (no ``-b``, no base), ``git submodule sync + update --init
    --recursive`` (:func:`shipit.git.submodule_update_init`, #485/#486 — a reviewer over
    submodule-backed content must see the real files), then
    ``chmod`` the working tree read-only.
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
    started = time.monotonic()
    try:
        git.clone_dissociated(github_url, str(tmp), reference=source_repo)
        git.fetch(cwd=str(tmp))
        git.checkout(plan.branch, cwd=str(tmp))
        # Populate submodules before the read-only chmod (#485): a reviewer reading a PR
        # over submodule-backed content must see the real files, not an empty gitlink.
        # Run BEFORE chmod_readonly so git can still write the submodule working trees.
        git.submodule_update_init(cwd=str(tmp))
        # A fresh clone's files are all newly written, so this stamp is redundant TODAY —
        # it matters on the reuse path. It is written here anyway so the exclude line
        # exists from the start, and every leaf carries the stamp regardless of which
        # path made it. Unlike the reuse path this needs no particular placement: the
        # clone is still at its `tmp` name, so gc cannot yet see it under the leaf it
        # will be renamed to, and there is no aged activity for a scan to misread.
        _stamp_acquisition(tmp)
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
        # A concurrent reviewer won the race and created the leaf first: discard our
        # temp clone and treat it as the shared-reuse case (refresh + return theirs).
        logger.debug(
            "read-only tree: lost the creation race for %s; reusing the "
            "co-tenant's clone",
            dest,
            extra={"tree": str(dest)},
        )
        remove_tree(tmp)
        return _reuse_or_refuse(dest, plan.branch)

    duration_ms = int((time.monotonic() - started) * 1000)
    # A fresh shared read-only Tree is a Tree birth too — the `tree.created`
    # dev-cycle event (ADR-0032). A REUSED leaf (`_reuse_or_refuse`) stays an
    # untagged milestone: nothing was created, so the trail records no birth.
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


def _reuse_or_refuse(dest: Path, branch: str) -> Tree:
    """Reuse the existing shared leaf (refreshed to the current head), or refuse a non-clone.

    The shared-reuse decision: a leaf that holds a clone (``.git`` present) is REFRESHED
    to the current remote head and re-guarded, then returned — never served as-is, since
    a stale head would have a co-tenant review the wrong commit. A leaf that is NOT a
    clone is a stray dir squatting the shared slot and is refused loud.
    """
    if (dest / _GIT_DIR).exists():
        started = time.monotonic()
        _refresh_readonly(dest, branch)
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "read-only tree reused at %s (branch %s, refreshed to head in %dms)",
            dest,
            branch,
            duration_ms,
            extra={"tree": str(dest), "duration_ms": duration_ms},
        )
        return _summary(dest, branch)
    raise FileExistsError(
        f"review tree dir already exists but is not a clone: {dest}; refusing "
        "to clone into or delete a non-Tree directory in the shared review slot."
    )


def _refresh_readonly(dest: Path, branch: str) -> None:
    """Re-pin a reused shared clone to the current remote head, re-applying the guard.

    The clone's working tree is ``chmod``'d read-only, so it must first be made writable
    (:func:`chmod_writable`) before git can rewrite it; then ``fetch`` + ``checkout`` +
    ``reset --hard origin/<branch>`` move it to the CURRENT PR head, ``submodule update
    --init --recursive`` re-pins its submodules to that head (#485), and the read-only
    guard is re-applied so a head that advanced under the first reviewer never leaves a
    co-tenant on a stale commit OR with stale (writable) permissions.

    The re-guard runs in a ``finally`` (#486): the mutable refresh can FAIL LOUD — a
    ``reset`` conflict or a submodule fetch that hits an auth/network wall — and a bare
    sequence would then propagate the error with the shared clone left WRITABLE, breaking
    the ADR-0018 guarantee for every co-tenant reviewer reusing the slot. Restoring the
    read-only guard before the error re-raises keeps the FS guard load-bearing even on the
    failure path (the caller still sees the original exception and rolls the leaf back).

    The acquisition is stamped FIRST — before the ``chmod`` and before the refresh
    (:func:`_stamp_acquisition`, and :data:`_ACQUIRED_STAMP` for why the trace must exist
    at all). Until the stamp lands the leaf still reads at its OLD activity, i.e. as idle
    as it was a moment ago, so every slow step that runs before it is a window in which a
    concurrent ``gc`` measures this Tree as removable and deletes it *while this reviewer
    is acquiring it* — the precise mid-review deletion the stamp exists to prevent,
    merely narrowed to the acquisition (codex, #1029 review rounds 2 and 3). BOTH slow
    steps are inside that window, and they are slow for different reasons: the refresh is
    network-bound (a ``fetch`` plus a recursive submodule update), while
    :func:`chmod_writable` is a full-tree walk that ``chmod``s every working path — the
    same order of work as the activity walk ``gc`` itself pays for, not a rounding error.
    Stamping before both is what leaves nothing slow ahead of the claim.

    Claiming first also subsumes what the old ``finally`` placement was for: a reviewer
    whose ``reset`` hit a conflict still holds this leaf, and a Tree deleted under a
    failing Run is the same #1018 bug as one deleted under a healthy one — stamping
    before the ``try`` covers that failure path without needing to re-stamp on the way
    out. Nothing downstream disturbs the claim: the ``chmod`` moves ctime, not mtime, and
    ``reset --hard`` does not touch an excluded untracked file.

    What this does NOT buy is atomicity, and it is worth being exact about the limit.
    ``gc`` reads a Tree's signals and deletes on a later tick, so an acquisition that
    begins after the read but before the delete is unprotected no matter how early it
    stamps — a hint cannot close a check-then-act race, only a lock or a lease can, and
    ADR-0072 fixed the rule at three measured signals precisely to keep a lease read out
    of it. The stamp shrinks the window to the ``gc`` scan's own; ADR-0074 removes it
    entirely by making review Trees per-Run, which is where the race actually dies.
    """
    _stamp_acquisition(dest)
    chmod_writable(dest)
    try:
        git.fetch(cwd=str(dest))
        git.checkout(branch, cwd=str(dest))
        git.reset_hard(f"origin/{branch}", cwd=str(dest))
        # Re-pin submodules to the head the reset landed on (#485): the advanced head may
        # move a gitlink, so a reused reviewer clone must refresh its submodules too, or a
        # co-tenant reads stale submodule content. Before the re-guard, while it is writable.
        git.submodule_update_init(cwd=str(dest))
    finally:
        chmod_readonly(dest)


def _stamp_acquisition(dest: Path) -> None:
    """Record that a reviewer Run just took ``dest`` — the acquisition's FS trace.

    Excludes then touches :data:`_ACQUIRED_STAMP` (see there for why the trace has to
    exist at all). Called on both acquisition paths — a fresh clone and a reuse — as the
    FIRST thing either does, so the claim precedes every slow step that would otherwise
    run against a leaf still reading as idle (:func:`_refresh_readonly`). Once written it
    stays written: the refresh's ``reset --hard`` does not touch an excluded untracked
    file, so the claim is made once and holds for the whole acquisition.

    It establishes its own precondition rather than taking one. The stamp is a new file
    at the Tree ROOT, so it needs the root dir writable — and on the reuse path the leaf
    arrives still under the read-only guard. Restoring the root's write bit HERE (one
    ``chmod`` on one dir, O(1)) is what lets the claim run first; requiring a writable
    root instead would order this call behind :func:`chmod_writable`'s full-tree walk and
    reopen the window that walk's duration spans. Only the root is touched — the guard
    over the working files is left exactly as it was found, and each acquisition path
    re-applies it wholesale afterwards (:func:`chmod_readonly`, or the ``finally`` in
    :func:`_refresh_readonly`), so this never widens what a co-tenant can write.

    Refreshing an EXISTING stamp would not need even that — ``os.utime`` on a file this
    Run owns is permitted while the file is read-only — but a leaf cloned before the
    stamp existed has no file to refresh, and creating one is the case that needs the
    root. One path that always works beats two that split on the leaf's vintage.

    The exclude is written BEFORE the stamp, never after: between creating an untracked
    root file and hiding it, ``git status --porcelain`` reports the Tree ``dirty``, and
    a concurrent ``registry.scan`` landing in that window would read a permanent
    local-work floor off a file shipit itself planted. Both steps are idempotent — the
    exclude line is appended only if absent, so refreshing a leaf a hundred times leaves
    one line.

    Best-effort ON PURPOSE, and this is the one direction that needs an argument: a
    failure here is swallowed at DEBUG rather than raised. The stamp only ever KEEPS a
    Tree, so failing to write it cannot cause a wrong delete on its own — it just
    returns the Tree to the pre-existing behaviour of measuring its head's age. Raising
    instead would be strictly worse: it would fail a reviewer's acquisition — the real
    work — over a bookkeeping write, and on the reuse path it would now abort the refresh
    before it ever re-pinned the checkout, turning a bookkeeping hiccup into a reviewer
    who cannot read the PR at all.
    """
    try:
        # The root's write bit, so the stamp below can be created under the guard. Not
        # `chmod_writable`: that walks the whole tree, and the point of doing this here
        # is to leave nothing slow ahead of the claim.
        dest.chmod(dest.stat().st_mode | _WRITE_BITS)
        exclude = dest / _GIT_DIR / _GIT_EXCLUDE
        exclude.parent.mkdir(parents=True, exist_ok=True)
        entries = exclude.read_text().splitlines() if exclude.exists() else []
        if _ACQUIRED_STAMP not in entries:
            with exclude.open("a") as handle:
                handle.write(f"{_ACQUIRED_STAMP}\n")
        (dest / _ACQUIRED_STAMP).touch()
    except OSError:
        logger.debug(
            "read-only tree: could not stamp acquisition at %s",
            dest,
            exc_info=True,
            extra={"tree": str(dest)},
        )


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
