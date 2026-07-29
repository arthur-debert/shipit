"""stage-from-prefix: copy resolved conda files from the pixi env prefix into an app consumer's shipped bundle.
See docs/adr/0077-collapse-to-conda-direct.md
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import _FEATURE_NAME_RE, _STAGING_ROOT, StageEntry, path_escapes
from .fspath import first_link_component, is_link
from .install.artifactdeps import env_prefix

logger = logging.getLogger("shipit.staging")


class StagingError(RuntimeError):
    """A stage-from-prefix step could not complete; always raised loudly, never a silent skip."""


@dataclass(frozen=True)
class StagedFile:
    """One completed copy; ``source`` and ``dest`` are POSIX paths exactly as declared."""

    package: str
    source: str
    dest: str
    is_dir: bool
    executable: bool


def _reject_unbounded_dest(
    staging_root_res: Path, dst: Path, entry: StageEntry
) -> None:
    """Refuse a destination that is not a STRICT DESCENDANT of the resolved staging root."""
    dst_res = dst.resolve()
    if dst_res == staging_root_res or not dst_res.is_relative_to(staging_root_res):
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} does not resolve to "
            f"a strict descendant of the staging root ({staging_root_res}) — staging "
            f"is bounded to the shipped-bundle dir `{_STAGING_ROOT}/` so it can never "
            f"touch the checkout root, `.git`, or the env; point {entry.source!r} at "
            f"a path under `{_STAGING_ROOT}/…`"
        )


def _reject_link_components(
    base: Path, parts: tuple[str, ...], what: str, entry: StageEntry | None = None
) -> None:
    """Refuse if ANY component of ``base``/``parts`` is a link or junction, not only the leaf."""
    offender = first_link_component(base, parts)
    if offender is not None:
        ctx = f"[stage.{entry.package}] " if entry is not None else ""
        raise StagingError(
            f"{ctx}{what} component {offender.name!r} is a symlink or junction "
            f"({offender}) — staging refuses to FOLLOW links; it copies only real "
            f"files and real directories, so a redirect cannot steer the copy out "
            f"of tree"
        )


def _reject_source_escape(entry: StageEntry) -> None:
    """Refuse an ``entry.source`` that is not prefix-relative, BEFORE it is joined to the prefix."""
    if path_escapes(entry.source):
        raise StagingError(
            f"[stage.{entry.package}] source {entry.source!r} is not a prefix-relative "
            f"path — no leading '/', no '\\', no drive letter, no '..' segment; the "
            f"source must live inside the resolved env prefix"
        )


def _copy_into(src: Path, dst: Path, entry: StageEntry) -> None:
    """Recursively copy ``src`` to ``dst``, copying only real files and directories and following no link."""
    if is_link(src):
        raise StagingError(
            f"[stage.{entry.package}] source node {src.name!r} under {entry.source!r} "
            f"is a symlink or junction ({src}) — staging refuses to follow links out "
            f"of the resolved env; stage only real files materialized in the prefix"
        )
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _copy_into(child, dst / child.name, entry)
        shutil.copystat(src, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        src_mode = src.stat().st_mode
        if src_mode & 0o111:
            dst.chmod(dst.stat().st_mode | (src_mode & 0o111))
    else:
        raise StagingError(
            f"[stage.{entry.package}] source node {src.name!r} under {entry.source!r} "
            f"is not a regular file or directory (a socket/fifo or special file) — "
            f"refusing to stage; the source must be materialized content"
        )


def _remove_if_present(path: Path) -> None:
    """Remove a prior stage at ``path``; a link is UNLINKED, never ``rmtree``d through."""
    if os.path.lexists(path):
        if path.is_dir() and not is_link(path):
            shutil.rmtree(path)
        else:
            path.unlink()


def _stage_one(
    prefix: Path,
    root: Path,
    staging_root_res: Path,
    entry: StageEntry,
) -> StagedFile:
    """Copy one entry from the env prefix into the staging root, idempotently and all-or-nothing."""
    _reject_source_escape(entry)
    _reject_link_components(prefix, PurePosixPath(entry.source).parts, "source", entry)
    src = prefix / entry.source
    if not src.exists():
        raise StagingError(
            f"[stage.{entry.package}] source {src} is not materialized in the env "
            f"prefix — run `shipit install` (or `pixi install`) so the conda "
            f"package `{entry.package}` is resolved and extracted first; the stage "
            f"step COPIES the env, it never fetches"
        )
    dst = root / entry.dest
    _reject_unbounded_dest(staging_root_res, dst, entry)
    src_is_dir = src.is_dir()
    if os.path.lexists(dst) and dst.is_dir() and not is_link(dst) and not src_is_dir:
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} already exists as a "
            f"directory but source {entry.source!r} is a file — refusing to wipe a "
            f"directory to replace it with a single file; check the [stage] mapping"
        )
    try:
        _remove_if_present(dst)
        try:
            _copy_into(src, dst, entry)
        except BaseException:
            _remove_if_present(dst)
            raise
    except OSError as exc:
        raise StagingError(
            f"[stage.{entry.package}] failed to stage {entry.source!r} to "
            f"{entry.dest!r}: {exc}"
        ) from exc
    executable = bool(dst.stat().st_mode & 0o111)
    return StagedFile(
        package=entry.package,
        source=entry.source,
        dest=entry.dest,
        is_dir=src_is_dir,
        executable=executable,
    )


def _reject_bad_feature(feature: str | None) -> None:
    """Refuse a ``--feature`` value that is not a plain feature identifier."""
    if feature is not None and not _FEATURE_NAME_RE.match(feature):
        raise StagingError(
            f"--feature {feature!r} is not a valid feature name (a leading "
            f"alphanumeric, then letters, digits, '.', '-', '_'); a path-shaped "
            f"value must not steer the env prefix outside `.pixi/envs`"
        )


def stage(
    root: Path, entries: Sequence[StageEntry], *, feature: str | None = None
) -> list[StagedFile]:
    """Stage every entry from the ``feature`` env prefix into ``root``; ``None`` targets the default env."""
    _reject_bad_feature(feature)
    root_res = root.resolve()
    prefix = env_prefix(root, feature)
    _reject_link_components(root, prefix.relative_to(root).parts, "the env prefix")
    prefix_res = prefix.resolve()
    # Measured against the resolved CHECKOUT root, never a re-resolved `.pixi/envs`:
    # resolving both sides would keep an escaped prefix "relative to" its own
    # redirected anchor and pass.
    if not prefix_res.is_relative_to(root_res):
        raise StagingError(
            f"the resolved env prefix {prefix_res} escapes the checkout ({root_res}) "
            f"— a symlinked/junctioned `.pixi`/`.pixi/envs` must not redirect staging "
            f"to read files outside the tree; check `--feature` and the env layout"
        )
    staging_root = root / _STAGING_ROOT
    if is_link(staging_root):
        raise StagingError(
            f"the staging root `{_STAGING_ROOT}/` must be a real directory in the "
            f"checkout, not a symlink or junction — `{staging_root}` is a link (it "
            f"would redirect staging into the checkout root, `.git`, or off the "
            f"tree); make `{_STAGING_ROOT}` a real directory"
        )
    if staging_root.exists() and not staging_root.is_dir():
        raise StagingError(
            f"the staging root `{_STAGING_ROOT}/` must be a directory, but "
            f"`{staging_root}` exists as a non-directory; make `{_STAGING_ROOT}` a "
            f"real directory in the checkout"
        )
    staging_root_res = staging_root.resolve()
    if not staging_root_res.is_relative_to(root_res):
        raise StagingError(
            f"the staging root `{_STAGING_ROOT}/` resolves outside the checkout "
            f"({root_res}) — make `{_STAGING_ROOT}` a real directory inside the tree"
        )
    staged: list[StagedFile] = []
    for entry in entries:
        staged.append(_stage_one(prefix, root, staging_root_res, entry))
    logger.info(
        "staged %d file(s) from the env prefix into the staging root %r",
        len(staged),
        _STAGING_ROOT,
        extra={
            "count": len(staged),
            "feature": feature,
            "staging_root": _STAGING_ROOT,
            "packages": ",".join(sorted({s.package for s in staged})) or None,
        },
    )
    return staged
