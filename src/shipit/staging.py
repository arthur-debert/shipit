"""stage-from-prefix — the generic, manifest-driven copy of resolved conda files
from the pixi env prefix into an app consumer's shipped bundle (conda-direct
#1079, ADR-0077 §"Staging", ADR-0076).

The APP-consumer half of conda-direct: once `pixi install`/`shipit install` has
resolved a conda dependency and extracted it into the env prefix
(`<root>/.pixi/envs/<env>/…` — a tool at `bin/<tool>`, a data artifact under
`share/<pkg>/…`, ADR-0076), an app that SHIPS those files needs them copied into
its bundle (`resources/`, then packed by electron-builder/vsce/… downstream).
This module is the manifest-driven mirror of the legacy `fetch-deps`/`deps.json`
tool, with only the SOURCE axis swapped: a gh-release download becomes a read of
the already-resolved env prefix. The manifest is the `[stage.<pkg>]` map
(:func:`shipit.config.load_stage`): source-in-prefix → dest-under-root pairs,
selecting a per-consumer subset from the union package.

It is DELIBERATELY a separate mechanism from the vsix `bundle.stage` map
(:func:`shipit.release.bundle._stage_vsix_natives`): that staging is release-time,
TRANSIENT (unstaged after `vsce package`), single-binary, per-target, and keyed on
`[artifact-deps]` (the DSL conda-direct is dismantling). THIS staging is DURABLE
(the app ships it), STANDALONE (a build step, not release compose), copies files
AND directories, and is keyed on the source path. The two share only the
security-sensitive primitives — the prefix resolver
(:func:`shipit.install.artifactdeps.env_prefix`) and the checkout-escape guard
(:func:`shipit.config._reject_path_escape`, applied at parse) — never duplicated.

The copy is a re-runnable build step, so it is IDEMPOTENT: a dest that already
exists (a previous stage) is replaced, not refused — the opposite of the vsix
path, whose fresh-path refusal exists only so its transient cleanup removes
exactly what it created. Each staged FILE keeps its source mode (a tool binary
stays executable, a `.wasm`/`.json` stays plain); a staged DIRECTORY is copied
whole, per-file modes preserved.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import StageEntry
from .install.artifactdeps import env_prefix

logger = logging.getLogger("shipit.staging")


class StagingError(RuntimeError):
    """A stage-from-prefix step could not complete — a source not materialized in
    the env prefix, or a destination that would escape the checkout.

    Raised loudly (never a silent skip): staging a file the channel never
    delivered, or through a symlink that leaves the tree, is a config/setup
    mistake the app build must stop on, not paper over. Mapped to the uniform
    ``error: …`` + exit 1 by the CLI error shell (it is in ``KNOWN_ERRORS``).
    """


@dataclass(frozen=True)
class StagedFile:
    """One completed copy: the package it came from, the source-in-prefix and
    dest-under-root (both POSIX, as declared), whether it was a directory, and
    whether the staged file is executable.

    ``executable`` is meaningful for a file (a tool binary must stay runnable in
    the shipped bundle); for a directory it reports whether the copied TREE ROOT
    carries an exec bit, and per-file modes inside are preserved by the copy.
    """

    package: str
    source: str
    dest: str
    is_dir: bool
    executable: bool


def _reject_escape(root_res: Path, dst: Path, entry: StageEntry) -> None:
    """Refuse a destination that resolves outside the checkout root.

    ``entry.dest`` is already guaranteed relative + ``..``-free at parse
    (:func:`shipit.config._reject_path_escape`); this catches the remaining
    vector — a SYMLINKED PARENT (a committed ``resources`` → ``/outside``) that
    would steer the copy through it. ``dst.resolve()`` reflects any symlink in an
    EXISTING ancestor (and a symlinked dest itself), so the check runs BEFORE any
    mkdir/removal — nothing is created or deleted until the path is proven inside
    the tree. Mirrors the vsix stage's ``is_relative_to(leg_root)`` guard.
    """
    if not dst.resolve().is_relative_to(root_res):
        raise StagingError(
            f"[stage.{entry.package}] destination {dst} resolves outside the "
            f"checkout root ({root_res}) — a symlinked parent must not steer "
            f"staging beyond the tree; point {entry.source!r} at a real path "
            f"inside the checkout"
        )


def _stage_one(
    prefix: Path, root: Path, root_res: Path, entry: StageEntry
) -> StagedFile:
    """Copy one :class:`~shipit.config.StageEntry` from the env prefix into the
    checkout, idempotently, and report the :class:`StagedFile`.

    The source must already be materialized under ``prefix`` (``shipit install``/
    ``pixi install`` extracted it); an absent source is a loud
    :class:`StagingError` pointing at install, never a silent skip — this step
    COPIES an already-resolved env, it never fetches. The dest is escape-checked
    (:func:`_reject_escape`), any prior stage at the dest is removed (idempotent
    re-run), the parent dirs are created, and the file/dir is copied with modes
    preserved (a tool binary keeps its exec bit).
    """
    src = prefix / entry.source
    if not src.exists():
        raise StagingError(
            f"[stage.{entry.package}] source {src} is not materialized in the env "
            f"prefix — run `shipit install` (or `pixi install`) so the conda "
            f"package `{entry.package}` is resolved and extracted first; the stage "
            f"step COPIES the env, it never fetches"
        )
    dst = root / entry.dest
    # Escape check BEFORE any mkdir/removal — nothing is touched until the dest is
    # proven inside the tree (a symlinked parent is the one remaining vector).
    _reject_escape(root_res, dst, entry)
    # Idempotent re-run: replace a prior stage. The dest resolved inside root
    # above, so removal is safe (a symlinked dest would have failed the guard).
    if os.path.lexists(dst):
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        # copytree copies with copy2 per file, preserving each file's mode.
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)  # preserves mode (and so the exec bit) already
        # Belt-and-suspenders: re-assert the source's exec bits explicitly so a
        # binary staged under a restrictive umask (or a copy2 that dropped them)
        # stays runnable in the shipped bundle — the executable round trip the
        # DoD asserts. A non-exec source (.wasm/.json) is left plain.
        src_mode = src.stat().st_mode
        if src_mode & 0o111:
            dst.chmod(dst.stat().st_mode | (src_mode & 0o111))
    executable = bool(dst.stat().st_mode & 0o111)
    return StagedFile(
        package=entry.package,
        source=entry.source,
        dest=entry.dest,
        is_dir=src.is_dir(),
        executable=executable,
    )


def stage(
    root: Path, entries: Sequence[StageEntry], *, feature: str | None = None
) -> list[StagedFile]:
    """Stage every ``entries`` copy from the ``feature`` env prefix into ``root``.

    Resolves the one env prefix (:func:`shipit.install.artifactdeps.env_prefix` —
    the single source of truth the vsix staging also uses, so a feature never maps
    to a different env in one caller than another) and copies each entry in
    declaration order. Returns the completed :class:`StagedFile` list (for the
    verb's report and tests). Raises :class:`StagingError` on the first entry whose
    source is not materialized or whose dest would escape the checkout — a build
    step that must stop, never a partial silent success.

    ``feature`` selects a named pixi feature/env; ``None`` (the default) targets
    the default env, where conda-direct's plain consumer-owned deps resolve.
    """
    prefix = env_prefix(root, feature)
    root_res = root.resolve()
    staged: list[StagedFile] = []
    for entry in entries:
        staged.append(_stage_one(prefix, root, root_res, entry))
    logger.info(
        "staged %d file(s) from the env prefix into resources",
        len(staged),
        extra={
            "count": len(staged),
            "feature": feature,
            "packages": ",".join(sorted({s.package for s in staged})) or None,
        },
    )
    return staged
