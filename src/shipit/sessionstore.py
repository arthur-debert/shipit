"""``sessionstore`` — one Claude Code session store per repo, shared by every Tree.

Claude Code keys session transcripts *and* auto-memory on ``~/.claude/projects/<slug>/``,
where ``<slug>`` is the session's **cwd**, slugified. A Tree per session (ADR-0027)
means a fresh cwd per session, hence a brand-new empty namespace on every launch:
memory is not broken, it is re-partitioned every session and never read back, and
resume cannot find a transcript from any directory but the one that wrote it.

There is no configuration knob — the slug derivation is hardcoded in the harness — but
the store is a plain path and a **symlink is honoured**. So :func:`plant` pre-creates
``~/.claude/projects/<slug>`` as a symlink to the repo's one store, before the session
starts; the session then writes its transcript into the shared target rather than
replacing the link (ADR-0073, verified against Claude Code 2.1.212).

The two callers are the two places a cwd shipit owns comes into being:
:func:`shipit.tree.create.create` (every Tree) and ``shipit install`` (the canonical
checkout), so work in a Tree and work in the plain checkout share one store rather
than splitting in two.

**Identity is the origin remote, not the path** (:class:`shipit.identity.Repo`) —
consistent with :func:`shipit.identity.resolve_repo`, which resolves repo identity from
the remote precisely because the path shape "is not a reliable identity". The store lives
at ``~/.claude/stores/<owner>/<repo>/``, deliberately OUTSIDE ``projects/`` so
shipit-owned state is never confused with the harness's own cwd-slug dirs.

**Planting is a four-case ladder, not "link it"** (:func:`plant`), because the
canonical checkout's slug dir is the hard case and the common one: it already exists
as a real directory with real content. Clobbering destroys it; skipping leaves the
store split in two forever. So: correct symlink → no-op; absent → create; real
directory → :func:`adopt` its contents into the store, then replace it with the link;
a symlink pointing elsewhere → refuse loudly and change nothing.

**Planting's effectful rungs are serialized per store** (:func:`_store_lock`), because
the store is the one thing every checkout of a repo SHARES: two checkouts planting at
once are two adopters merging into the same target, and the matrix' classify → copy →
verify → unlink sequence is only safe if no one else moves the target underneath it.
Unserialized, two adopters can each classify the same destination as absent, and the
second's copy overwrites the first's — after which both verify and delete their sources,
and the first's content is gone. The lock is what makes "nothing is deleted until
verified present in the target" true against a concurrent adopter rather than only
against a single one.

**The lock serializes shipit against shipit; the atomic publish serializes it against
the harness.** The store is also written by LIVE Claude sessions (ADR-0073 — they rewrite
``memory/MEMORY.md``), which hold no shipit lock, so a destination classified absent can
APPEAR or change before an adopter copies onto it. The move rungs (:func:`_move_file`,
:func:`_move_symlink`) therefore copy into a unique STAGING path, verify THAT, and publish
with ``EEXIST`` no-clobber semantics (:func:`os.link` / :func:`os.symlink`): a destination
that appeared is a keep-both collision, never an overwrite, and nothing this module did not
itself create is ever unlinked. The lock alone could not give this — it does not extend to
the harness.

**The lock is necessary but not sufficient: the decision it guards must be made under
it.** A classification taken before the lock is stale by the time the lock is granted,
and the ladder's rungs are not stable under a concurrent planter — two planters of the
same checkout both see a real directory, and the one that waits is admitted holding a
"directory" verdict for what is now a symlink to the store. Adopting that walks the store
into itself and deletes it. So :func:`plant` re-runs :func:`_settle` once it holds the
lock, and only then acts.

Fail-open is the contract at the CALL sites, not here: this module raises nothing for
an ordinary refusal (it reports one), and its callers swallow the environment-shaped
failures — an unresolvable store path must never cost a Tree its creation.
"""

from __future__ import annotations

import filecmp
import logging
import os
import re
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .identity import Repo

#: The session-store axis' logger (LOG02 spray, ADR-0029). A refusal is a durable,
#: degraded-but-continuing outcome and logs at WARNING; the ordinary no-op/link/adopt
#: milestones log at DEBUG.
logger = logging.getLogger(__name__)

#: Every character the harness does NOT keep verbatim in a cwd slug. Verified against
#: Claude Code 2.1.212 by probing real sessions: ``/``, ``_``, ``.``, a space, ``+``
#: and ``@`` each map to a single ``-``, and runs are NOT collapsed (a real store dir
#: ``-private-tmp-claude-501--Users-…`` carries the double dash a ``/`` followed by a
#: literal ``-`` produces). So the rule is a per-character substitution of everything
#: outside ``[a-zA-Z0-9]``, not a separators-only denylist.
_NON_SLUG = re.compile(r"[^a-zA-Z0-9]")

# The entry types the adoption matrix is total over. Classified from `lstat` and NEVER
# by dereferencing: a symlink is a symlink, not the thing it points at. `OTHER` (fifo,
# socket, device) is the catch-all that keeps the matrix total against a filesystem
# that offers more than three shapes.
_ABSENT = "absent"
_FILE = "file"
_DIR = "dir"
_SYMLINK = "symlink"
_OTHER = "other"

#: :attr:`PlantResult.outcome` values — the four rungs of the ladder.
NOOP = "noop"
LINKED = "linked"
ADOPTED = "adopted"
REFUSED = "refused"


@dataclass(frozen=True)
class PlantResult:
    """What :func:`plant` did — the outcome plus every path it refused to touch.

    ``refusals`` is the paths refused, whichever rung refused them: a type conflict met
    *inside* adoption (see :func:`adopt`) contributes the conflicting source path, and a
    refusal at the ladder's own root — a foreign symlink or a non-directory squatting on
    the slug path — contributes the link path itself.

    An ``outcome`` of :data:`REFUSED` means the link was not planted at all; refusals
    with an ``outcome`` of :data:`ADOPTED` are impossible by construction — a slug dir
    that could not be fully drained is never replaced by the link.
    """

    link: Path
    store: Path
    outcome: str
    refusals: list[str] = field(default_factory=list)


def slug_for(path: Path | str) -> str:
    """The harness's ``~/.claude/projects/`` directory name for a session whose cwd is ``path``.

    A **pure function of the path** — which is what lets :func:`plant` pre-create the
    link with no coordination with the session that will use it.

    The path is **resolved first**. This is load-bearing, not hygiene: the harness slugs
    the cwd's *real* path, so a session started in ``/tmp/x`` (a symlink to
    ``/private/tmp/x`` on macOS) writes to ``-private-tmp-x``. Slugging the unresolved
    path would plant the link at a name no session ever reads — the bug would look
    exactly like doing nothing.
    """
    return _NON_SLUG.sub("-", str(Path(path).resolve()))


def _default_home() -> Path:
    """The real ``~`` — the ONE place this module resolves it, so tests can replace it.

    Every public entry point takes a ``home`` override, but the callers in production
    pass nothing, so a test that exercises a *caller* (``tree create``, ``shipit
    install``) would reach the developer's real ``~/.claude`` through this default and
    plant real symlinks in it. That is not hypothetical — it is what happened before the
    suite-wide autouse guard in ``tests/conftest.py`` existed, and it is the very
    data-loss mode this module is written to prevent. Funnelling the default through one
    named function is what gives that guard a single thing to replace.
    """
    return Path.home()


def store_dir(repo: Repo, *, home: Path | None = None) -> Path:
    """The one session store for ``repo`` — ``~/.claude/stores/<owner>/<repo>/``.

    Keyed on the repo's identity (its origin remote), never on any checkout's path, so
    every Tree of a repo and its canonical checkout resolve to the same directory.
    ``home`` overrides ``~`` (tests pass a tmp root; nothing may touch the real store).
    """
    base = _default_home() if home is None else home
    return base / ".claude" / "stores" / repo.owner.login / repo.name


def link_path(checkout: Path | str, *, home: Path | None = None) -> Path:
    """Where the harness will look for the session store of a session whose cwd is ``checkout``."""
    base = _default_home() if home is None else home
    return base / ".claude" / "projects" / slug_for(checkout)


def lock_path(repo: Repo, *, home: Path | None = None) -> Path:
    """The adoption lock file for ``repo``'s store — a SIBLING of the store, never inside it.

    Beside the store rather than in it because the store's contents are the harness's to
    read: a lock file inside would be one more entry Claude Code has to ignore, and one
    more entry a future adopter would try to merge. The lock is shipit's bookkeeping, so
    it lives at shipit's level.

    The suffix is APPENDED, never substituted (``with_suffix`` would replace one): repo
    names carry dots — ``docs.github.io`` and ``docs.github.com`` are one repo name each,
    not a stem and an extension — and substituting would collapse both onto a single
    ``docs.github.lock``, quietly serializing two unrelated repos against each other.
    """
    store = store_dir(repo, home=home)
    return store.parent / f"{store.name}.lock"


@contextmanager
def _store_lock(lock: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``lock`` for a whole plant transaction.

    The unit of exclusion is the STORE, and the critical section is the entire
    transaction — decide the rung, classify, copy, verify, unlink the source, and replace
    the drained dir with the link — not merely the copy. Locking only the copy would still
    let a second adopter classify a destination as absent while the first is mid-verify,
    which is the same race one step later: the guarantee adoption sells is that a source is
    deleted only once its content is provably in the target, and that "provably" spans from
    classification to unlink. The *rung* decision is inside the section for the same
    reason — see :func:`plant`.

    An advisory lock is enough because shipit's adopters are the only writers that ever
    collide here: a live session appends its own uuid-named transcripts to the store and
    never writes a path an adopter is moving. This does not serialize the *harness*, and
    is not meant to.

    Platform contract mirrors the eval store's locking seam
    (:func:`shipit.harness.eval.store.lock_exclusive`, #893): ``fcntl`` is unix-only and
    is imported HERE rather than at module level, since this module sits on the CLI's
    import chain and a module-level import would crash the CLI on Windows at import
    time. On Windows the lock is a documented NO-OP — the kernel drops the real lock when
    the handle closes, and a Windows adopter is assumed to be the only one.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def plant(checkout: Path | str, repo: Repo, *, home: Path | None = None) -> PlantResult:
    """Point ``checkout``'s slug dir at ``repo``'s one session store; return what happened.

    The ADR-0073 ladder, for **any** slug dir — generic, idempotent, repo-agnostic:

    1. **already the correct symlink** → no-op (idempotence: re-running install, or
       re-creating a Tree, must be free);
    2. **absent** → create the symlink;
    3. **a real directory** → :func:`adopt` its contents into the store, then replace
       it with the symlink. Content-preserving; the link replaces the dir only once the
       dir is provably empty, so a refusal inside adoption costs content nothing;
    4. **a symlink pointing elsewhere** → refuse, loudly, change nothing. Something
       outside shipit owns that path and this does not get to guess.

    Sameness in case 1 is **link-text identity**, compared without dereferencing (the
    ADR's rule). Our own links are always written as ``str(store_dir(...))``, so a
    re-run compares byte-identical text and case 1 holds — that IS the idempotence.

    Cases 2 and 3 — the two that write — run under the store's :func:`_store_lock`, and
    **re-decide the ladder from a fresh classification once they hold it**. The
    classification below the lock is a fast path only: between it and the lock, another
    planter of the SAME checkout (two ``shipit install`` runs in the canonical checkout)
    can adopt the dir and replace it with the link, which would leave the loser adopting
    a symlink that now points AT the store — ``iterdir`` follows it, every entry compares
    equal to itself, and the identical-file rung deletes the store's only copy. The
    decision is therefore only trustworthy while the lock is held; that is why
    :func:`_settle` is applied twice rather than once.

    Cases 1 and 4 — no-op and refusal — are terminal without writing anything, and the
    pre-lock :func:`_settle` is what keeps them that way: a refusal that created the store
    dir, the ``projects/`` parent or the lock file would not be the "nothing changed" it
    claims to be. (The re-check under the lock can still refuse *after* those mkdirs, but
    only when the path changed type mid-race — by then this call had a real directory to
    adopt, and the store dir it left behind is the empty one the winning planter created
    anyway.)

    Raises ``OSError`` only for genuinely unexpected I/O; a *refusal* is a return value,
    not an exception. Callers are fail-open (a Tree is not worth losing to a store).
    """
    store = store_dir(repo, home=home)
    link = link_path(checkout, home=home)

    settled = _settle(link, store)
    if settled is not None:
        return settled

    store.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)

    with _store_lock(lock_path(repo, home=home)):
        # Re-decide under the lock: the classification that got us here is now stale, and
        # acting on a stale one is the same-checkout race that empties the store.
        settled = _settle(link, store)
        if settled is not None:
            return settled

        if _classify(link) == _ABSENT:
            link.symlink_to(store, target_is_directory=True)
            logger.debug("session store linked: %s -> %s", link, store)
            return PlantResult(link, store, LINKED)

        refusals = adopt(link, store)
        remaining = sorted(p.name for p in link.iterdir())
        if remaining:
            # Never rmdir a dir that still holds content — that is the data loss this
            # whole WS exists to prevent. The store is left split, loudly, which is
            # recoverable; a deleted memory is not.
            logger.warning(
                "session store NOT linked: adopted what it could from %s into %s, but "
                "%d entr(y/ies) remain (%s); the slug dir is kept as-is — resolve by "
                "hand.",
                link,
                store,
                len(remaining),
                ", ".join(remaining),
            )
            return PlantResult(link, store, REFUSED, refusals)

        link.rmdir()
        link.symlink_to(store, target_is_directory=True)
    logger.debug("session store adopted and linked: %s -> %s", link, store)
    return PlantResult(link, store, ADOPTED, refusals)


def _settle(link: Path, store: Path) -> PlantResult | None:
    """The ladder's terminal rungs: :data:`NOOP`, :data:`REFUSED`, or ``None`` for "act".

    ``None`` means ``link`` is absent or a real directory — the two classifications that
    call for a write, and the two :func:`plant` re-checks under the lock. Every rung here
    is decided from ``lstat`` alone and **writes nothing**, which is what lets it serve
    both as the pre-lock fast path and as the authoritative decision once the lock is
    held, and what keeps a refusal free of side effects.
    """
    kind = _classify(link)

    if kind == _SYMLINK:
        if os.readlink(link) == str(store):
            logger.debug("session store already linked: %s -> %s", link, store)
            return PlantResult(link, store, NOOP)
        logger.warning(
            "session store NOT linked: %s is a symlink to %s, not to %s; refusing to "
            "retarget a link shipit does not own — nothing changed.",
            link,
            os.readlink(link),
            store,
        )
        return PlantResult(link, store, REFUSED, [str(link)])

    if kind not in (_ABSENT, _DIR):
        # A plain file (or a socket/fifo) squatting on the slug path: a type conflict at
        # the ladder's own root. The ADR's matrix refuses every type conflict rather than
        # guess, and the same reasoning applies a level up.
        logger.warning(
            "session store NOT linked: %s exists and is a %s, not a directory; "
            "refusing to replace it — nothing changed.",
            link,
            kind,
        )
        return PlantResult(link, store, REFUSED, [str(link)])

    return None


def adopt(source: Path, target: Path) -> list[str]:
    """Merge ``source``'s contents into ``target``; return the paths refused.

    A **recursive merge over relative paths**, not a move of top-level entries: a slug
    dir holds ``memory/`` (itself a directory), per-session ``<uuid>/`` dirs and
    ``<uuid>.jsonl`` transcripts, so the *first* collision adoption meets is
    directory-versus-directory. Moving a top-level entry would rename or clobber the
    whole ``memory/`` tree and produce a layout Claude will not read. The unit of
    conflict is therefore the relative path, resolved by walking both sides and
    applying the ADR's **total** (source × target) type matrix:

    ==========  ================  ==========================  ====================  ==========================
    source ↓    target: absent    target: file                target: dir           target: symlink
    ==========  ================  ==========================  ====================  ==========================
    **file**    move in           identical → drop;           REFUSE                REFUSE
                                  differs → keep both
    **dir**     move in           REFUSE                      merge recursively     REFUSE
    **symlink** move in, as a     REFUSE                      REFUSE                same text → drop;
                symlink                                                             differs → keep both
    ==========  ================  ==========================  ====================  ==========================

    The matrix is total because any pair left undefined is a pair an implementer
    guesses, and a wrong guess here overwrites data. The three outcomes:

    - **merge recursively** (dir/dir) — never rename, never replace; descend and
      reapply the matrix. This is the common case: both sides carry ``memory/``.
    - **keep both** — the target's entry is untouched; the source's lands beside it
      under a non-colliding name. Never overwrite, never silently drop, never
      machine-merge. Sameness is byte-identity for files and **link-text** identity for
      symlinks (compared without dereferencing — a link is data about the source, and
      two links with the same text are the same link even if both dangle).
    - **REFUSE** — a type conflict at one path: skip it, say so loudly, change nothing
      there, and carry on with the rest. It means an assumption about the layout is
      wrong, and dedupe/rename/overwrite would each destroy one of the two.

    Symlinks are **adopted, never followed**: following one would move content the
    source does not own and would silently convert a link into a copy.

    ``MEMORY.md`` gets no special case — it is a file and collides like one. The
    *semantic* merge of divergent memories is judgement work (WS05, #1024), not a
    filesystem operation.

    **Nothing is deleted from a source until its content is verified present in the
    target.** Every move copies, verifies, and only then unlinks; a source directory is
    removed only once it is empty. Memory is irreplaceable — an adoption that loses a
    file to save a directory entry has defeated the point.

    **The caller serializes.** This function walks ``target`` and acts on what it finds,
    so it is only safe while nothing else writes ``target``'s paths: two adopters running
    at once can both classify one destination as absent, and the second's copy would land
    on the first's file just before both verify and delete their sources. It does NOT
    take the lock itself — it recurses, and re-entering an ``flock`` on a fresh descriptor
    would deadlock against itself. :func:`plant` is the entry point that holds
    :func:`_store_lock` across the whole transaction; a direct caller owes the same.
    """
    refusals: list[str] = []
    for entry in sorted(source.iterdir()):
        refusals.extend(_adopt_entry(entry, target / entry.name))
    return refusals


def _adopt_entry(src: Path, dst: Path) -> list[str]:
    """Apply the matrix to ONE relative path. Returns the refusals it produced."""
    src_kind, dst_kind = _classify(src), _classify(dst)

    if src_kind == _DIR and dst_kind in (_ABSENT, _DIR):
        # "move in" (absent) and "merge recursively" (dir) are the same operation once
        # the target exists: descend and reapply the matrix per child. Doing it this way
        # rather than a bulk move buys per-leaf verification for free.
        dst.mkdir(parents=True, exist_ok=True)
        refusals = adopt(src, dst)
        _prune_empty(src)
        return refusals

    if src_kind == _FILE and dst_kind == _ABSENT:
        return _move_file(src, dst)

    if src_kind == _FILE and dst_kind == _FILE:
        if filecmp.cmp(src, dst, shallow=False):
            src.unlink()  # a verified duplicate: the content provably survives in dst
            return []
        return _move_file(src, _free_name(dst))

    if src_kind == _SYMLINK and dst_kind == _ABSENT:
        return _move_symlink(src, dst)

    if src_kind == _SYMLINK and dst_kind == _SYMLINK:
        if os.readlink(src) == os.readlink(dst):
            src.unlink()  # same link text == the same link, even if both dangle
            return []
        return _move_symlink(src, _free_name(dst))

    return _refuse(src, dst, src_kind, dst_kind)


def _refuse(src: Path, dst: Path, src_kind: str, dst_kind: str) -> list[str]:
    """A type conflict at one path: change nothing there, say so, carry on."""
    logger.warning(
        "session store adoption REFUSED %s: source is a %s but target %s is a %s; "
        "leaving both untouched — a type conflict is not a collision to resolve.",
        src,
        src_kind,
        dst,
        dst_kind,
    )
    return [str(src)]


def _move_file(src: Path, dst: Path) -> list[str]:
    """Copy ``src`` to ``dst``, VERIFY the bytes, unlink ``src`` — publishing NO-CLOBBER.

    ``dst`` was classified :data:`_ABSENT` by :func:`_adopt_entry`, but the store is written
    by live Claude sessions that hold no lock (module docstring, ADR-0073), so it can APPEAR
    or change before this runs. A plain ``copy2(src, dst)`` would overwrite that live file,
    and the old verify-failure ``dst.unlink`` would delete it outright. Instead: copy into a
    unique STAGING sibling, verify THAT, then hard-link it into place with :func:`os.link`,
    whose ``EEXIST`` is the atomic no-clobber publish — a ``dst`` that appeared is resolved
    by the matrix' keep-both rung (:func:`_free_name`), never an overwrite. On a copy that
    does not verify, only the STAGING path this call created is removed; the destination —
    which this module may not have made — is never touched.
    """
    staging = dst.with_name(f".{dst.name}.shipit-adopt-{uuid4().hex}")
    shutil.copy2(src, staging)
    if not filecmp.cmp(src, staging, shallow=False):
        # Never reached in practice; if it ever is, the source is what we keep.
        staging.unlink(missing_ok=True)  # only the partial copy WE just made
        logger.warning(
            "session store adoption REFUSED %s: the copy for %s did not verify; "
            "the source is kept and the staging copy removed.",
            src,
            dst,
        )
        return [str(src)]
    _publish_no_clobber(staging, dst)
    src.unlink()
    return []


def _publish_no_clobber(staging: Path, dst: Path) -> None:
    """Hard-link the verified ``staging`` copy to ``dst`` WITHOUT clobbering, then drop it.

    :func:`os.link` raises ``FileExistsError`` if the destination exists — the O_EXCL
    semantics the per-store lock cannot give against the lock-less harness. A ``dst`` (or a
    freshly scanned ``_free_name``) a live session created since classification is met with
    keep-both, never an overwrite; the loop covers the free name itself racing. The hard link
    shares ``staging``'s inode and ``copy2``'s metadata, so unlinking staging afterwards
    leaves the published file intact.
    """
    target = dst
    while True:
        try:
            os.link(staging, target)
            break
        except FileExistsError:
            target = _free_name(dst)
    staging.unlink()


def _move_symlink(src: Path, dst: Path) -> list[str]:
    """Recreate ``src``'s link TEXT at ``dst`` (never following it), verify, unlink ``src``.

    Same no-clobber contract as :func:`_move_file` against the lock-less harness (module
    docstring, ADR-0073): a ``dst`` classified absent can appear first. :func:`os.symlink` is
    itself atomic and raises ``EEXIST`` if the destination exists, so the link is created
    straight at ``dst`` and a collision routes to keep-both (:func:`_free_name`); the loop
    covers the free name racing too. Only the link THIS call created is ever unlinked (on a
    verify failure that in practice never fires) — a destination the store did not make is
    left untouched.
    """
    text = os.readlink(src)
    target = dst
    while True:
        try:
            target.symlink_to(text)
            break
        except FileExistsError:
            target = _free_name(dst)
    if os.readlink(target) != text:
        target.unlink()  # this call created it; the verify failed
        logger.warning(
            "session store adoption REFUSED %s: the symlink recreated at %s did not "
            "verify; the source is kept.",
            src,
            target,
        )
        return [str(src)]
    src.unlink()
    return []


def _free_name(dst: Path) -> Path:
    """A non-colliding sibling of ``dst`` — the "keep both" name.

    The extension is preserved (``MEMORY.md`` → ``MEMORY.adopted-1.md``) so an adopted
    memory is still a readable ``.md`` to whatever reads the store next.
    """
    n = 1
    while True:
        candidate = dst.with_name(f"{dst.stem}.adopted-{n}{dst.suffix}")
        if _classify(candidate) == _ABSENT:
            return candidate
        n += 1


def _prune_empty(directory: Path) -> None:
    """``rmdir`` ``directory`` iff it is empty — the only deletion adoption ever does.

    A dir that still holds entries is one a REFUSE left content in; keeping it is the
    "nothing is deleted until verified present in the target" contract doing its job.
    """
    try:
        directory.rmdir()
    except OSError:
        logger.debug("session store adoption kept non-empty source dir %s", directory)


def _classify(path: Path) -> str:
    """The entry's type from ``lstat`` — WITHOUT dereferencing, so a symlink is a symlink.

    A missing path (and a path whose parent is missing) is :data:`_ABSENT`; anything
    that is not a symlink/dir/regular file is :data:`_OTHER`, which the matrix refuses
    rather than guesses at.
    """
    try:
        mode = os.lstat(path).st_mode
    except (OSError, ValueError):
        return _ABSENT
    if stat.S_ISLNK(mode):
        return _SYMLINK
    if stat.S_ISDIR(mode):
        return _DIR
    if stat.S_ISREG(mode):
        return _FILE
    return _OTHER
