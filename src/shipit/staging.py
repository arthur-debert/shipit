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
Because this copy is DURABLE and its blast radius is the whole checkout, parse's
lexical guard is backed by RUNTIME defenses on the resolved paths: a source whose
symlink escapes the prefix (:func:`_reject_source_escape`), a dest that resolves
onto the checkout root or a repo-critical dir (:func:`_reject_escape`), and a
``--feature`` that is not a plain identifier (:func:`_reject_bad_feature`) are all
refused before a single file is copied or removed.

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

from .config import _FEATURE_NAME_RE, _PROTECTED_DEST_ROOTS, StageEntry
from .install.artifactdeps import env_prefix

logger = logging.getLogger("shipit.staging")

#: The ``.pixi/envs`` segments a resolved env prefix must stay beneath — the
#: defense-in-depth backstop to the ``feature`` name validation, so even a bug in
#: the naming helper cannot land the prefix outside the checkout's env tree.
_PIXI_ENVS = (".pixi", "envs")


class StagingError(RuntimeError):
    """A stage-from-prefix step could not complete — a source not materialized in
    the env prefix, a source symlink resolving OUTSIDE the prefix, a destination
    that would escape the checkout or hit a repo-critical dir (the checkout root,
    ``.git``, ``.pixi``), a ``--feature`` whose name is not a plain identifier, or
    a raw filesystem ``OSError`` mapped at the copy boundary.

    Raised loudly (never a silent skip): staging a file the channel never
    delivered, copying host files in through an escaping symlink, or wiping the
    checkout/``.git`` via a ``.`` destination is a config/setup mistake the app
    build must stop on, not paper over. Mapped to the uniform ``error: …`` + exit
    1 by the CLI error shell (it is in ``KNOWN_ERRORS``).
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
    """Refuse a destination that resolves to/above the checkout root, into a
    repo-critical dir, or outside the checkout entirely.

    ``entry.dest`` is already guaranteed relative + ``..``-free (and lexically
    non-``.``/non-protected) at parse (:func:`shipit.config._parse_stage_table`);
    this is the RUNTIME defense that also catches what parse cannot see — a
    SYMLINKED PARENT (a committed ``resources`` → ``/outside``), or a symlinked
    dest that resolves onto the root or ``.git``/``.pixi``. ``dst.resolve()``
    reflects every symlink in an existing ancestor (and a symlinked dest itself),
    so all three checks run on the RESOLVED path BEFORE any mkdir/removal —
    nothing is created or deleted until the path is proven a strict descendant of
    the tree that is not a repo-critical dir. The root check is load-bearing: a
    ``dest = "."`` (or a symlink onto the root) makes ``dst == root``, and the
    idempotent-rerun ``rmtree`` below would then delete the WHOLE checkout
    (``.git``, uncommitted work). Mirrors the vsix stage's ``is_relative_to``
    guard, hardened for the durable copy's larger blast radius.
    """
    dst_res = dst.resolve()
    if dst_res == root_res:
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} resolves to the "
            f"checkout root ({root_res}) — refusing to stage onto the repo root, "
            f"whose idempotent re-run would `rmtree` the entire checkout (.git and "
            f"uncommitted work); point {entry.source!r} at a subdirectory such as "
            f"`resources/…`"
        )
    if not dst_res.is_relative_to(root_res):
        raise StagingError(
            f"[stage.{entry.package}] destination {dst} resolves outside the "
            f"checkout root ({root_res}) — a symlinked parent must not steer "
            f"staging beyond the tree; point {entry.source!r} at a real path "
            f"inside the checkout"
        )
    top = dst_res.relative_to(root_res).parts[0]
    if top in _PROTECTED_DEST_ROOTS:
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} resolves into the "
            f"repo-critical `{top}` directory — refusing to stage there, whose "
            f"idempotent re-run would destroy the repository metadata or the "
            f"resolved env being copied from; point {entry.source!r} at a "
            f"shippable subdirectory such as `resources/…`"
        )


def _reject_source_escape(prefix_res: Path, src: Path, entry: StageEntry) -> None:
    """Refuse a source that resolves OUTSIDE the env prefix through a symlink.

    Parse only checked the source string LEXICALLY (relative, ``..``-free); it
    cannot see that a package planted a symlink inside the prefix whose target
    leaves it. ``copy2``/``copytree`` follow symlinks by default, so an escaping
    link would copy arbitrary HOST files into the shipped bundle — the exact
    source-in-prefix contract this step promises. Two vectors are covered on the
    RESOLVED path, before anything is copied:

    - the selected source itself (a top-level symlink) must resolve inside the
      prefix;
    - for a directory source, EVERY symlink in the tree must resolve inside the
      prefix — ``os.walk`` does not follow links (no descent, no loops), so each
      link is inspected in place. With no link escaping, the default-dereferencing
      ``copytree`` is safe: every target is real, materialized, in-prefix content.
    """
    if not src.resolve().is_relative_to(prefix_res):
        raise StagingError(
            f"[stage.{entry.package}] source {entry.source!r} resolves outside the "
            f"env prefix ({prefix_res}) — a symlink must not steer staging beyond "
            f"the resolved env; stage only real files materialized in the prefix"
        )
    if not src.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(src):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            link = base / name
            if link.is_symlink() and not link.resolve().is_relative_to(prefix_res):
                raise StagingError(
                    f"[stage.{entry.package}] source directory {entry.source!r} "
                    f"contains a symlink ({link.relative_to(src)}) resolving "
                    f"outside the env prefix ({prefix_res}) — refusing to copy host "
                    f"files from beyond the resolved env into the bundle"
                )


def _stage_one(
    prefix: Path, prefix_res: Path, root: Path, root_res: Path, entry: StageEntry
) -> StagedFile:
    """Copy one :class:`~shipit.config.StageEntry` from the env prefix into the
    checkout, idempotently, and report the :class:`StagedFile`.

    The source must already be materialized under ``prefix`` (``shipit install``/
    ``pixi install`` extracted it); an absent source is a loud
    :class:`StagingError` pointing at install, never a silent skip — this step
    COPIES an already-resolved env, it never fetches. The source is symlink-escape
    checked (:func:`_reject_source_escape`) and the dest escape/root/protected-dir
    checked (:func:`_reject_escape`) BEFORE anything is touched; a file source may
    not overwrite an existing directory dest (that would wipe the tree to drop one
    file); any prior stage at the dest is removed (idempotent re-run); the parent
    dirs are created; and the file/dir is copied with modes preserved (a tool
    binary keeps its exec bit). Every filesystem mutation is funneled through the
    :class:`OSError` → :class:`StagingError` boundary so a ``PermissionError`` or
    ``FileExistsError`` surfaces as the uniform ``error: …`` + exit 1, never a raw
    traceback.
    """
    src = prefix / entry.source
    if not src.exists():
        raise StagingError(
            f"[stage.{entry.package}] source {src} is not materialized in the env "
            f"prefix — run `shipit install` (or `pixi install`) so the conda "
            f"package `{entry.package}` is resolved and extracted first; the stage "
            f"step COPIES the env, it never fetches"
        )
    # Source + dest escape checks run BEFORE any mkdir/removal — nothing is touched
    # until the source is proven in-prefix and the dest a safe strict descendant.
    _reject_source_escape(prefix_res, src, entry)
    dst = root / entry.dest
    _reject_escape(root_res, dst, entry)
    src_is_dir = src.is_dir()
    if (
        os.path.lexists(dst)
        and dst.is_dir()
        and not dst.is_symlink()
        and not src_is_dir
    ):
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} already exists as a "
            f"directory but source {entry.source!r} is a file — refusing to wipe a "
            f"directory to replace it with a single file; check the [stage] mapping"
        )
    try:
        # Idempotent re-run: replace a prior stage. The dest resolved to a safe
        # strict descendant above, so removal is scoped to the dest subtree.
        if os.path.lexists(dst):
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src_is_dir:
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
    """Refuse a ``--feature`` value that is not a plain feature identifier.

    ``feature`` interpolates unvalidated through
    :func:`shipit.install.artifactdeps.env_name` into ``shipit-artifacts-{feature}``
    and then into a filesystem path; a value carrying a separator or ``..`` could
    make the computed prefix leave ``.pixi/envs``. Reuse the SAME
    :data:`shipit.config._FEATURE_NAME_RE` the ``[artifact-deps]`` parser enforces
    (a leading alphanumeric, then ``[A-Za-z0-9._-]`` — no ``/``, and ``..`` fails
    the leading-alphanumeric rule), so the CLI/domain boundary is validated the way
    the config boundary already is, one source of truth.
    """
    if feature is not None and not _FEATURE_NAME_RE.match(feature):
        raise StagingError(
            f"--feature {feature!r} is not a valid feature name (a leading "
            f"alphanumeric, then letters, digits, '.', '-', '_'); a path-shaped "
            f"value must not steer the env prefix outside `.pixi/envs`"
        )


def stage(
    root: Path, entries: Sequence[StageEntry], *, feature: str | None = None
) -> list[StagedFile]:
    """Stage every ``entries`` copy from the ``feature`` env prefix into ``root``.

    Validates ``feature`` (:func:`_reject_bad_feature`), resolves the one env
    prefix (:func:`shipit.install.artifactdeps.env_prefix` — the single source of
    truth the vsix staging also uses, so a feature never maps to a different env in
    one caller than another), and copies each entry in declaration order. Returns
    the completed :class:`StagedFile` list (for the verb's report and tests).
    Raises :class:`StagingError` on a bad feature, a resolved prefix that escapes
    ``.pixi/envs``, or the first entry whose source is not materialized, whose
    source symlink escapes the prefix, or whose dest would escape the checkout /
    hit a repo-critical dir — a build step that must stop, never a partial silent
    success.

    ``feature`` selects a named pixi feature/env; ``None`` (the default) targets
    the default env, where conda-direct's plain consumer-owned deps resolve.
    """
    _reject_bad_feature(feature)
    root_res = root.resolve()
    prefix = env_prefix(root, feature)
    prefix_res = prefix.resolve()
    # Defense in depth behind the feature-name check: the resolved prefix must stay
    # under `<root>/.pixi/envs` — a belt to the regex's suspenders.
    envs_root = root.joinpath(*_PIXI_ENVS).resolve()
    if not prefix_res.is_relative_to(envs_root):
        raise StagingError(
            f"resolved env prefix {prefix} escapes `.pixi/envs` ({envs_root}) — "
            f"refusing to stage; check the --feature value"
        )
    staged: list[StagedFile] = []
    for entry in entries:
        staged.append(_stage_one(prefix, prefix_res, root, root_res, entry))
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
