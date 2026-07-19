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
2. UNIFIED SCAN-AND-COPY. The copy is a single recursive traversal
   (:func:`_copy_into`) that resolves and containment-checks EVERY node against the
   env prefix in the SAME pass that copies it — never a ``shutil.copytree`` behind a
   separate ``os.walk`` pre-scan, whose traversals diverge (the scan skipping a
   directory-symlink subtree that ``copytree`` then follows). A node whose real path
   leaves the prefix is refused before its bytes are read; directory symlinks are
   followed exactly as the copy would, cycle-guarded by a visited set of resolved
   dirs. A ``--feature`` that is not a plain identifier is likewise refused before
   the prefix is resolved.

The copy is a re-runnable build step, so it is IDEMPOTENT: a dest that already
exists (a previous stage) is replaced, not refused — the opposite of the vsix
path, whose fresh-path refusal exists only so its transient cleanup removes
exactly what it created. Each staged FILE keeps its source mode (a tool binary
stays executable, a `.wasm`/`.json` stays plain), applied UNIFORMLY per copied
file whether staged alone or inside a directory tree.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import _FEATURE_NAME_RE, _STAGING_ROOT, StageEntry
from .install.artifactdeps import env_prefix

logger = logging.getLogger("shipit.staging")

#: The ``.pixi/envs`` segments a resolved env prefix must stay beneath — the
#: defense-in-depth backstop to the ``feature`` name validation, so even a bug in
#: the naming helper cannot land the prefix outside the checkout's env tree.
_PIXI_ENVS = (".pixi", "envs")


class StagingError(RuntimeError):
    """A stage-from-prefix step could not complete — a source not materialized in
    the env prefix, a source node resolving OUTSIDE the prefix (a symlink escape),
    a destination that is not a strict descendant of the staging root
    ``<root>/resources`` (:data:`shipit.config._STAGING_ROOT`), a ``--feature``
    whose name is not a plain identifier, or a raw filesystem ``OSError`` mapped at
    the copy boundary.

    Raised loudly (never a silent skip): staging a file the channel never
    delivered, or copying host files in through an escaping symlink, is a
    config/setup mistake the app build must stop on, not paper over. Mapped to the
    uniform ``error: …`` + exit 1 by the CLI error shell (it is in
    ``KNOWN_ERRORS``).
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


def _copy_into(
    src: Path, dst: Path, prefix_res: Path, entry: StageEntry, visited: set[Path]
) -> None:
    """Recursively copy ``src`` → ``dst``, resolving and containment-checking EVERY
    node against the env prefix in the SAME traversal that copies it.

    This is the unification that kills the scan/copy divergence: there is no
    ``shutil.copytree`` behind a separate ``os.walk`` pre-scan (whose traversals
    disagree on directory-symlink subtrees). Each node's REAL path
    (``src.resolve()``) must stay inside ``prefix_res`` before its bytes are read;
    ``src.is_dir()``/``is_file()`` follow symlinks, so a directory symlink is
    entered EXACTLY as a dereferencing copy would enter it — but only after its
    resolved target is proven in-prefix, and ``visited`` (resolved dirs) breaks any
    symlink cycle. A regular file is copied from its resolved real path with its
    source mode reasserted (the exec-bit round trip the DoD asserts), applied
    uniformly to every file whether the entry is a lone file or a directory tree.
    Anything else (a broken symlink, a socket/fifo) is refused — not shippable.
    """
    real = src.resolve()
    if not real.is_relative_to(prefix_res):
        raise StagingError(
            f"[stage.{entry.package}] source node {src.name!r} under {entry.source!r} "
            f"resolves outside the env prefix ({prefix_res}) — a symlink must not "
            f"steer staging beyond the resolved env; stage only real files "
            f"materialized in the prefix"
        )
    if src.is_dir():
        if real in visited:
            return  # a symlink cycle back to an already-copied dir — stop
        visited.add(real)
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            _copy_into(child, dst / child.name, prefix_res, entry, visited)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, dst)  # copies the resolved in-prefix bytes + mode
        # Re-assert the source's exec bits so a binary staged under a restrictive
        # umask (or a copy2 that dropped them) stays runnable in the shipped bundle.
        src_mode = real.stat().st_mode
        if src_mode & 0o111:
            dst.chmod(dst.stat().st_mode | (src_mode & 0o111))
    else:
        raise StagingError(
            f"[stage.{entry.package}] source node {src.name!r} under {entry.source!r} "
            f"is not a regular file or directory (a broken symlink or special file) "
            f"— refusing to stage; the source must be materialized content"
        )


def _remove_if_present(path: Path) -> None:
    """Remove a prior stage at ``path`` (dir tree or file/symlink), if any."""
    if os.path.lexists(path):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _stage_one(
    prefix: Path,
    prefix_res: Path,
    root: Path,
    staging_root_res: Path,
    entry: StageEntry,
) -> StagedFile:
    """Copy one :class:`~shipit.config.StageEntry` from the env prefix into the
    staging root, idempotently and ALL-OR-NOTHING, and report the :class:`StagedFile`.

    The source must already be materialized under ``prefix`` (``shipit install``/
    ``pixi install`` extracted it); an absent source is a loud :class:`StagingError`
    pointing at install, never a silent skip — this step COPIES an already-resolved
    env, it never fetches. The dest is bounded to a strict descendant of the staging
    root (:func:`_reject_unbounded_dest`) BEFORE anything is touched, and a file
    source may not overwrite an existing directory dest (that would wipe the tree to
    drop one file). Any prior stage at the dest is removed (idempotent re-run) and
    the copy runs through the unified :func:`_copy_into`; on ANY failure the partial
    dest is cleaned up so a refused entry leaves nothing behind. Every filesystem
    mutation is funneled through the :class:`OSError` → :class:`StagingError`
    boundary so a ``PermissionError``/``FileExistsError`` surfaces as the uniform
    ``error: …`` + exit 1, never a raw traceback.
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
    _reject_unbounded_dest(staging_root_res, dst, entry)
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
        # Idempotent re-run: replace a prior stage. The dest is a strict descendant
        # of the staging root, so removal is scoped inside the shipped-bundle dir.
        _remove_if_present(dst)
        try:
            _copy_into(src, dst, prefix_res, entry, set())
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
    feature, a resolved prefix that escapes ``.pixi/envs``, a staging root that
    itself escapes the checkout (a symlinked ``resources``), or the first entry
    whose source is not materialized, whose source symlink escapes the prefix, or
    whose dest is not a strict descendant of the staging root — a build step that
    must stop, never a partial silent success.

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
    # The single bounded destination space: <root>/resources. Its RESOLVED path
    # anchors every per-entry strict-descendant check; if `resources` is itself a
    # symlink out of the checkout, refuse the whole stage rather than let a dest
    # ride it beyond the tree.
    staging_root_res = (root / _STAGING_ROOT).resolve()
    if not staging_root_res.is_relative_to(root_res):
        raise StagingError(
            f"the staging root `{_STAGING_ROOT}/` resolves outside the checkout "
            f"({root_res}) — a symlinked `{_STAGING_ROOT}` must not steer staging "
            f"beyond the tree; make `{_STAGING_ROOT}` a real directory in the "
            f"checkout"
        )
    staged: list[StagedFile] = []
    for entry in entries:
        staged.append(_stage_one(prefix, prefix_res, root, staging_root_res, entry))
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
