"""One Claude Code session store per repo, shared by every Tree and the canonical checkout.

See docs/adr/0073-the-session-store-is-per-repo-not-per-tree.md.
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

logger = logging.getLogger(__name__)

_NON_SLUG = re.compile(r"[^a-zA-Z0-9]")

_ABSENT = "absent"
_FILE = "file"
_DIR = "dir"
_SYMLINK = "symlink"
_OTHER = "other"

NOOP = "noop"
LINKED = "linked"
ADOPTED = "adopted"
REFUSED = "refused"


@dataclass(frozen=True)
class PlantResult:
    """What :func:`plant` did — the outcome plus every path it refused to touch."""

    link: Path
    store: Path
    outcome: str
    refusals: list[str] = field(default_factory=list)


def slug_for(path: Path | str) -> str:
    """The harness's ``~/.claude/projects/`` name for a session whose cwd is ``path``; RESOLVED first, since the harness slugs the real path."""
    return _NON_SLUG.sub("-", str(Path(path).resolve()))


def _default_home() -> Path:
    return Path.home()


def store_dir(repo: Repo, *, home: Path | None = None) -> Path:
    """The one session store for ``repo``, keyed on its identity rather than any checkout's path."""
    base = _default_home() if home is None else home
    return base / ".claude" / "stores" / repo.owner.login / repo.name


def link_path(checkout: Path | str, *, home: Path | None = None) -> Path:
    base = _default_home() if home is None else home
    return base / ".claude" / "projects" / slug_for(checkout)


def lock_path(repo: Repo, *, home: Path | None = None) -> Path:
    """The adoption lock file for ``repo``'s store — a SIBLING of it, suffix appended rather than substituted (repo names carry dots)."""
    store = store_dir(repo, home=home)
    return store.parent / f"{store.name}.lock"


@contextmanager
def _store_lock(lock: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``lock`` for a whole plant transaction; a documented no-op on Windows."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def plant(checkout: Path | str, repo: Repo, *, home: Path | None = None) -> PlantResult:
    """Point ``checkout``'s slug dir at ``repo``'s one session store; return what happened.

    The correct symlink is a no-op, an absent path is linked, a real directory is
    :func:`adopt`ed then replaced by the link, a foreign symlink is refused. A refusal
    is a return value; ``OSError`` escapes only for unexpected I/O.
    """
    store = store_dir(repo, home=home)
    link = link_path(checkout, home=home)

    settled = _settle(link, store)
    if settled is not None:
        return settled

    store.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)

    with _store_lock(lock_path(repo, home=home)):
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
    """The terminal rungs — :data:`NOOP`, :data:`REFUSED`, or ``None`` when ``link`` is absent or a real directory and the caller must act. Writes nothing."""
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

    A recursive merge over RELATIVE paths: identical entries drop, differing ones keep
    both, a type conflict is refused untouched, symlinks adopt by link text. Nothing is
    deleted from a source until verified in the target. The CALLER serializes.
    """
    refusals: list[str] = []
    for entry in sorted(source.iterdir()):
        refusals.extend(_adopt_entry(entry, target / entry.name))
    return refusals


def _adopt_entry(src: Path, dst: Path) -> list[str]:
    src_kind, dst_kind = _classify(src), _classify(dst)

    if src_kind == _DIR and dst_kind in (_ABSENT, _DIR):
        dst.mkdir(parents=True, exist_ok=True)
        refusals = adopt(src, dst)
        _prune_empty(src)
        return refusals

    if src_kind == _FILE and dst_kind == _ABSENT:
        return _move_file(src, dst)

    if src_kind == _FILE and dst_kind == _FILE:
        if filecmp.cmp(src, dst, shallow=False):
            src.unlink()
            return []
        return _move_file(src, _free_name(dst))

    if src_kind == _SYMLINK and dst_kind == _ABSENT:
        return _move_symlink(src, dst)

    if src_kind == _SYMLINK and dst_kind == _SYMLINK:
        if os.readlink(src) == os.readlink(dst):
            src.unlink()
            return []
        return _move_symlink(src, _free_name(dst))

    return _refuse(src, dst, src_kind, dst_kind)


def _refuse(src: Path, dst: Path, src_kind: str, dst_kind: str) -> list[str]:
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
    """Copy ``src`` to ``dst``, verify the bytes, unlink ``src`` — published NO-CLOBBER through a staging sibling, so a ``dst`` a live session created is never overwritten."""
    staging = dst.with_name(f".{dst.name}.shipit-adopt-{uuid4().hex}")
    shutil.copy2(src, staging)
    if not filecmp.cmp(src, staging, shallow=False):
        staging.unlink(missing_ok=True)
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
    target = dst
    while True:
        try:
            os.link(staging, target)
            break
        except FileExistsError:
            target = _free_name(dst)
    staging.unlink()


def _move_symlink(src: Path, dst: Path) -> list[str]:
    """Recreate ``src``'s link TEXT at ``dst`` (never following it), then unlink ``src``; the atomic create is its own verification, so there is no read-back at the shared name."""
    text = os.readlink(src)
    target = dst
    while True:
        try:
            target.symlink_to(text)
            break
        except FileExistsError:
            target = _free_name(dst)
    src.unlink()
    return []


def _free_name(dst: Path) -> Path:
    n = 1
    while True:
        candidate = dst.with_name(f"{dst.stem}.adopted-{n}{dst.suffix}")
        if _classify(candidate) == _ABSENT:
            return candidate
        n += 1


def _prune_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        logger.debug("session store adoption kept non-empty source dir %s", directory)


def _classify(path: Path) -> str:
    """The entry's type from ``lstat``, WITHOUT dereferencing; a missing path is :data:`_ABSENT`, any other shape :data:`_OTHER`."""
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
