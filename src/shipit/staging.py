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

Because this copy is DURABLE and its blast radius is the whole checkout, safety is
STRUCTURAL rather than a growing denylist of dangerous vectors. Two invariants make
the danger classes unreachable by construction:

1. BOUNDED DESTINATION. Every dest must resolve to a STRICT DESCENDANT of one fixed
   staging root — the consumer's shipped-bundle dir ``<root>/resources``
   (:data:`shipit.config._STAGING_ROOT`). The check compares RESOLVED absolute
   paths, so ``.``, the checkout root, ``.git``/``.Git`` (a case alias is the same
   resolved dir), ``.pixi`` — none is expressible, and every ``rmtree``/overwrite
   can only ever land INSIDE the bundle dir. This one rule replaces the whole
   per-dest protected-name denylist (and its case-fold hole).
2. REFUSE LINKS — never follow them. Staging reads conda-EXTRACTED data (a real
   binary, ``.wasm``, queries, C source) — none of it is a symlink we need. So
   instead of resolving each node and re-checking that it stayed in-bounds (a
   whack-a-mole against symlink, then junction, then bind-mount…), the copy REFUSES
   any node whose component is a redirect — a POSIX symlink OR a Windows directory
   junction / reparse point (:func:`_is_link`, ``is_symlink() or is_junction()``,
   which ``is_symlink`` alone misses) — and copies only real files and real
   directories. With no link ever followed, containment is AUTOMATIC: a tree of
   real entries under the prefix physically cannot leave it, so there is no cycle to
   guard, no visited set, no resolved-path re-check. The security anchors are hardened
   the same way: the ``.pixi``/``.pixi/envs`` env-prefix chain and the ``resources``
   staging root must be real (link/junction-free) directories, and the resolved
   prefix and staging root must stay inside the resolved checkout root. A
   ``--feature`` that is not a plain identifier is refused before the prefix is
   resolved.

The copy is a re-runnable build step, so it is IDEMPOTENT: a dest that already
exists (a previous stage) is replaced, not refused — the opposite of the vsix
path, whose fresh-path refusal exists only so its transient cleanup removes
exactly what it created. Each staged FILE keeps its source mode (a tool binary
stays executable, a `.wasm`/`.json` stays plain), applied UNIFORMLY per copied
file whether staged alone or inside a directory tree; a copied DIRECTORY keeps its
source mode/mtime.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import _FEATURE_NAME_RE, _STAGING_ROOT, StageEntry
from .install.artifactdeps import env_prefix

logger = logging.getLogger("shipit.staging")


class StagingError(RuntimeError):
    """A stage-from-prefix step could not complete — a source not materialized in
    the env prefix, a source/env-anchor/staging-root component that is a LINK (a
    symlink or a Windows junction/reparse point, which staging refuses rather than
    follows), a destination that is not a strict descendant of the staging root
    ``<root>/resources`` (:data:`shipit.config._STAGING_ROOT`), a resolved prefix or
    staging root that escapes the checkout, a ``--feature`` whose name is not a plain
    identifier, or a raw filesystem ``OSError`` mapped at the copy boundary.

    Raised loudly (never a silent skip): staging a file the channel never
    delivered, or following a link out of the resolved env, is a config/setup
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


def _reject_unbounded_dest(
    staging_root_res: Path, dst: Path, entry: StageEntry
) -> None:
    """Refuse a destination that is not a STRICT DESCENDANT of the resolved staging
    root — the one rule that makes the whole data-loss class unexpressible.

    ``entry.dest`` is already lexically under ``resources/`` and ``..``-free at
    parse (:func:`shipit.config._parse_stage_table`); this is the RUNTIME defense on
    the RESOLVED absolute path, which is what closes the case-fold hole (``.Git`` and
    ``.git`` resolve to the SAME dir, compared by identity not spelling) and the
    symlinked-ancestor vector (a committed ``resources`` → elsewhere is reflected by
    ``dst.resolve()``). Because ``resources`` itself is bounded to the checkout by
    the caller, a dest that resolves onto or outside the staging root — the checkout
    root, ``.git``, ``.pixi`` — cannot pass, so the idempotent-rerun ``rmtree``/
    overwrite below can only ever act INSIDE the shipped-bundle dir.
    """
    dst_res = dst.resolve()
    if dst_res == staging_root_res or not dst_res.is_relative_to(staging_root_res):
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} does not resolve to "
            f"a strict descendant of the staging root ({staging_root_res}) — staging "
            f"is bounded to the shipped-bundle dir `{_STAGING_ROOT}/` so it can never "
            f"touch the checkout root, `.git`, or the env; point {entry.source!r} at "
            f"a path under `{_STAGING_ROOT}/…`"
        )


def _is_link(path: Path) -> bool:
    """True if ``path``'s final component is a REDIRECT — a POSIX symlink or a
    Windows directory junction / mount-point reparse point.

    Uses lstat/reparse-tag inspection (``is_symlink`` OR ``is_junction`` — the
    latter is what catches an NTFS junction that ``is_symlink`` reports ``False``
    for, the round-5 critical). Deliberately NOT a ``realpath``-divergence compare:
    that would misclassify a real directory whose ON-DISK CASE differs from the
    referenced name (``Resources`` reached via ``resources`` on a case-insensitive
    FS, where ``os.path.normcase`` is a no-op on darwin) as a redirect. Asking the
    component's own nature is case-agnostic. A non-existent path is not a link.
    """
    return path.is_symlink() or path.is_junction()


def _reject_link_components(
    base: Path, parts: tuple[str, ...], what: str, entry: StageEntry | None = None
) -> None:
    """Refuse if ANY component of ``base``/``parts`` (walked one at a time) is a
    link/junction — so a redirect anywhere along a source or anchor path is caught,
    not only its leaf.

    Staging never follows a link (:func:`_is_link`); refusing the whole chain means
    a real tree of real components physically cannot leave ``base``, making the
    resolved-path containment check redundant for what it copies.
    """
    cur = base
    for part in parts:
        cur = cur / part
        if _is_link(cur):
            ctx = f"[stage.{entry.package}] " if entry is not None else ""
            raise StagingError(
                f"{ctx}{what} component {part!r} is a symlink or junction ({cur}) — "
                f"staging refuses to FOLLOW links; it copies only real files and "
                f"real directories, so a redirect cannot steer the copy out of tree"
            )


def _copy_into(src: Path, dst: Path, entry: StageEntry) -> None:
    """Recursively copy ``src`` → ``dst``, copying ONLY real files and directories.

    No link is ever followed: ``src`` (and every child) is refused if it is a
    symlink/junction (:func:`_is_link`), so ``is_dir()``/``is_file()`` are
    unambiguous and the whole copied subtree is physically inside the env prefix —
    containment is structural, needing no resolved-path re-check, no cycle guard,
    no visited set. A regular file is copied with its source mode reasserted (the
    exec-bit round trip the DoD asserts), applied uniformly to every file whether
    the entry is a lone file or a directory tree; a copied directory gets its source
    mode/mtime restored (``copystat``) so a ``0o700`` tree is not flattened to the
    umask default. A special file (socket/fifo) is refused — not shippable.
    """
    if _is_link(src):
        raise StagingError(
            f"[stage.{entry.package}] source node {src.name!r} under {entry.source!r} "
            f"is a symlink or junction ({src}) — staging refuses to follow links out "
            f"of the resolved env; stage only real files materialized in the prefix"
        )
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _copy_into(child, dst / child.name, entry)
        shutil.copystat(src, dst)  # restore the source dir's mode/mtime
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)  # copies the real in-prefix bytes + mode
        # Re-assert the source's exec bits so a binary staged under a restrictive
        # umask (or a copy2 that dropped them) stays runnable in the shipped bundle.
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
    """Remove a prior stage at ``path`` (real dir tree, or a file/link), if any.

    A link (symlink or junction, :func:`_is_link`) is UNLINKED, never ``rmtree``d —
    removing the redirect, never deleting through it into its target's contents.
    """
    if os.path.lexists(path):
        if path.is_dir() and not _is_link(path):
            shutil.rmtree(path)
        else:
            path.unlink()


def _stage_one(
    prefix: Path,
    root: Path,
    staging_root_res: Path,
    entry: StageEntry,
) -> StagedFile:
    """Copy one :class:`~shipit.config.StageEntry` from the env prefix into the
    staging root, idempotently and ALL-OR-NOTHING, and report the :class:`StagedFile`.

    The source must already be materialized under ``prefix`` (``shipit install``/
    ``pixi install`` extracted it); an absent source is a loud :class:`StagingError`
    pointing at install, never a silent skip — this step COPIES an already-resolved
    env, it never fetches. No component of the source path (:func:`_reject_link_components`)
    may be a link/junction, and the dest is bounded to a strict descendant of the
    staging root (:func:`_reject_unbounded_dest`) BEFORE anything is touched; a file
    source may not overwrite an existing directory dest (that would wipe the tree to
    drop one file). Any prior stage at the dest is removed (idempotent re-run) and
    the copy runs through the link-refusing :func:`_copy_into`; on ANY failure the
    partial dest is cleaned up so a refused entry leaves nothing behind. Every
    filesystem mutation is funneled through the :class:`OSError` → :class:`StagingError`
    boundary so a ``PermissionError``/``FileExistsError`` surfaces as the uniform
    ``error: …`` + exit 1, never a raw traceback.
    """
    # Refuse a link/junction ANYWHERE along the source path before touching it, so a
    # redirect cannot steer even the top-level lookup out of the resolved env.
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
    if os.path.lexists(dst) and dst.is_dir() and not _is_link(dst) and not src_is_dir:
        raise StagingError(
            f"[stage.{entry.package}] destination {entry.dest!r} already exists as a "
            f"directory but source {entry.source!r} is a file — refusing to wipe a "
            f"directory to replace it with a single file; check the [stage] mapping"
        )
    try:
        # Idempotent re-run: replace a prior stage. The dest is a strict descendant
        # of the staging root, so removal is scoped inside the shipped-bundle dir.
        _remove_if_present(dst)
        try:
            _copy_into(src, dst, entry)
        except BaseException:
            # All-or-nothing per entry: a refused/failed copy leaves nothing behind.
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
    one caller than another) and the one staging root (``<root>/resources``), then
    copies each entry in declaration order. Returns the completed :class:`StagedFile`
    list (for the verb's report and tests). Raises :class:`StagingError` on a bad
    feature, a link/junction on the ``.pixi``/``.pixi/envs`` env-prefix chain or a
    resolved prefix outside the checkout, a ``resources`` staging root that is a
    link/junction / non-directory / resolves outside the checkout, or the first entry
    whose source is not materialized, whose source path has a link/junction
    component, or whose dest is not a strict descendant of the staging root — a build
    step that must stop, never a partial silent success.

    ``feature`` selects a named pixi feature/env; ``None`` (the default) targets
    the default env, where conda-direct's plain consumer-owned deps resolve.
    """
    _reject_bad_feature(feature)
    root_res = root.resolve()
    prefix = env_prefix(root, feature)
    # Env-prefix anchor: refuse a link/junction on `.pixi`/`.pixi/envs`/the env dir,
    # and require the RESOLVED prefix to stay inside the RESOLVED checkout. Comparing
    # to `root_res` (not a re-resolved `.pixi/envs`) is what closes the copilot hole:
    # if `.pixi/envs` were a symlink out of the tree, resolving BOTH sides would keep
    # the prefix "relative to" the redirected anchor — measuring against the checkout
    # root instead catches the escape, and refusing the link catches it first.
    _reject_link_components(root, prefix.relative_to(root).parts, "the env prefix")
    prefix_res = prefix.resolve()
    if not prefix_res.is_relative_to(root_res):
        raise StagingError(
            f"the resolved env prefix {prefix_res} escapes the checkout ({root_res}) "
            f"— a symlinked/junctioned `.pixi`/`.pixi/envs` must not redirect staging "
            f"to read files outside the tree; check `--feature` and the env layout"
        )
    # The single bounded destination space: <root>/resources — the anchor of every
    # per-entry strict-descendant check. The `resources` component must itself be a
    # REAL directory (link/junction-free, :func:`_is_link`) whose realpath stays in
    # the checkout, so a `resources` link/junction to `.`/`.git`/outside cannot point
    # the bound at the checkout root, git metadata, or off the tree. Refusing the
    # component by its OWN nature (not a resolved-path string compare) accepts a real
    # dir even when its on-disk case differs (`Resources` on a case-insensitive FS),
    # and — unlike `is_symlink` alone — a Windows junction is caught too.
    staging_root = root / _STAGING_ROOT
    if _is_link(staging_root):
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
