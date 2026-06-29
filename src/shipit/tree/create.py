"""``tree/create`` — the effectful orchestrator that materializes a *ready* Tree.

``create(spec, ...) -> Tree`` turns a pure :class:`~shipit.tree.layout.TreePlan`
into a real, independent, **provisioned** checkout on disk and returns the READY
summary (``{path, branch, base}``). The whole pipeline hides behind this one call
(PRD Implementation Decisions):

1. ``git clone --reference <local> --dissociate <github-url> <dir>`` — a tiny,
   instant, yet fully INDEPENDENT clone (ADR-0014); see
   :func:`shipit.gh.git_clone_dissociated`.
2. ``git fetch origin`` then ``git checkout -b <branch> <base>``.
3. apply ``.treeinclude`` — copy the gitignored-but-needed files (``.env``,
   Doppler config, models) from the source checkout into the new Tree
   (:mod:`shipit.tree.include`).
4. provision: ``shipit install`` then the path's ``pixi install`` / ``npm ci``,
   run with the ADR-0015 build env (per-Tree ``target/``, ``SCCACHE_BASEDIRS``,
   ``CARGO_INCREMENTAL=0``) so a cold Tree's first build is sccache-warm.

Materialization stays atomic from the caller's view: if any step fails, the
half-built leaf is removed before the error propagates. Every git call goes
through the :mod:`shipit.gh` boundary and every provisioning command through
:func:`run_provision`, so both are patchable; the integration smoke exercises the
real git path end to end, while the planning/matching truth tables are unit-tested
in ``layout`` / ``include``.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

import platformdirs

from .. import config, gh, proc
from . import include
from .layout import TreeSpec, central_root, plan

logger = logging.getLogger("shipit.tree")

#: Provisioning is driven by which manifests the checkout carries: a ``.shipit.toml``
#: gets the managed-set reconcile, a ``pixi.toml`` gets ``pixi install``, a
#: ``package.json`` gets ``npm ci``. Each is gated on its file existing, so a repo
#: that uses only one toolchain runs only that step.
PIXI_MANIFEST = "pixi.toml"
NPM_MANIFEST = "package.json"

#: Bytes of randomness behind an agent hash → 8 hex chars. Enough to keep two
#: concurrent Trees for the same issue from colliding on disk without bloating the
#: dir name.
_HASH_BYTES = 4


@dataclass(frozen=True)
class Tree:
    """A materialized Tree — the READY summary a caller prints/consumes."""

    path: str
    branch: str
    base: str


def new_agent_hash() -> str:
    """A short random hex tag that disambiguates a Tree's directory (never its branch)."""
    return secrets.token_hex(_HASH_BYTES)


def create(spec: TreeSpec, *, source_repo: str, github_url: str) -> Tree:
    """Materialize the Tree described by ``spec`` and return its READY summary.

    ``source_repo`` is the local checkout whose object store seeds the clone
    (``--reference``); ``github_url`` is the remote the new Tree's ``origin`` points
    at. The leaf directory's parents are created first (``git clone`` makes only
    the leaf), then the clone is dissociated, fetched, and put on the planned
    branch cut from the planned base.

    Materialization is atomic from the caller's view: if any step after the clone
    fails, the half-built leaf is removed before the error propagates, so a failed
    ``create`` never leaves a partial Tree on disk for the next run to trip over.
    """
    tree_plan = plan(spec)
    dest = tree_plan.dir
    trees_root = spec.root if spec.root is not None else central_root()
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        gh.git_clone_dissociated(github_url, str(dest), reference=source_repo)
        gh.git_fetch(cwd=str(dest))
        gh.git_checkout_new_branch(tree_plan.branch, tree_plan.base, cwd=str(dest))
        include.apply(source_repo, dest)
        _provision(dest, trees_root=Path(trees_root))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    return Tree(path=str(dest), branch=tree_plan.branch, base=tree_plan.base)


def sccache_env(tree_dir: Path) -> dict[str, str]:
    """The ADR-0015 build env that makes a cold Tree's first build sccache-warm.

    - ``CARGO_TARGET_DIR`` → the Tree's own ``target/``: artifacts are **per-Tree**
      (ADR-0015 rejects a shared target dir — Cargo locks it, serializing the fleet).
    - ``SCCACHE_BASEDIRS`` → the Tree dir: sccache's cache key includes the absolute
      build path, so without this every distinct Tree path misses the cross-Tree cache.
    - ``CARGO_INCREMENTAL=0``: sccache disables incremental anyway, and incremental
      bakes absolute paths that break under any copy.

    Merged over the child's environment (it does not replace it) by
    :func:`run_provision`.
    """
    return {
        "CARGO_TARGET_DIR": str(tree_dir / "target"),
        "SCCACHE_BASEDIRS": str(tree_dir),
        "CARGO_INCREMENTAL": "0",
    }


def run_provision(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run one provisioning command in the Tree (the patchable provisioning boundary).

    A thin wrapper over :func:`shipit.proc.run` (no shell; ``env`` MERGED over the
    process environment) so tests assert *which* commands provisioning would run and
    *with what env* without spawning ``pixi`` / ``npm``.
    """
    proc.run(cmd, cwd=str(cwd), env=env)


def _provision(dest: Path, *, trees_root: Path) -> None:
    """Provision the freshly-checked-out Tree so a write-session starts ready.

    Runs ``shipit install`` (when the repo carries ``.shipit.toml``), then the
    path's ``pixi install`` / ``npm ci``, each gated on its manifest existing and
    each run with the ADR-0015 build env. Before ``pixi install`` it checks (and
    only *warns* about — #119) the pixi-cache / Trees-root same-filesystem invariant.
    """
    env = sccache_env(dest)
    if (dest / config.CONFIG_NAME).is_file():
        run_provision(["shipit", "install", "."], cwd=dest, env=env)
    if (dest / PIXI_MANIFEST).is_file():
        _warn_if_cache_cross_filesystem(trees_root)
        run_provision(["pixi", "install"], cwd=dest, env=env)
    if (dest / NPM_MANIFEST).is_file():
        run_provision(["npm", "ci"], cwd=dest, env=env)


# --------------------------------------------------------------------------
# #119 — pixi cache / Trees root same-filesystem check (warn, never fail)
# --------------------------------------------------------------------------


def pixi_cache_dir() -> Path:
    """The directory pixi/rattler caches downloaded packages in.

    Honors ``PIXI_CACHE_DIR`` / ``RATTLER_CACHE_DIR`` overrides, else the platform
    default (``<user-cache>/rattler/cache``).
    """
    override = os.environ.get("PIXI_CACHE_DIR") or os.environ.get("RATTLER_CACHE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir("rattler")) / "cache"


def _st_dev(path: Path) -> int:
    """The filesystem device id backing ``path`` (a patchable seam for the FS check)."""
    return os.stat(path).st_dev


def check_same_filesystem(trees_root: Path, cache_dir: Path) -> str | None:
    """A warning when ``trees_root`` and ``cache_dir`` are on DIFFERENT filesystems.

    pixi links packages out of its cache into each Tree's environment; when the
    cache and the Trees root share a filesystem that linking is near-free, but
    across filesystems it silently falls back to **full copies** (#119). Returns the
    warning string in that case, else ``None``. A path that does not exist yet (so
    its device can't be read) is treated as "can't tell" → ``None`` (never fail).
    """
    try:
        if _st_dev(trees_root) != _st_dev(cache_dir):
            return (
                f"pixi cache ({cache_dir}) and Trees root ({trees_root}) are on "
                "different filesystems; package linking falls back to full copies, "
                "so Tree provisioning will be slower and use more disk (#119)."
            )
    except OSError:
        return None
    return None


def _warn_if_cache_cross_filesystem(trees_root: Path) -> None:
    """Emit the #119 cross-filesystem warning (WARNING+ → console) when it applies."""
    message = check_same_filesystem(trees_root, pixi_cache_dir())
    if message:
        logger.warning(message)


def create_from_source(spec: TreeSpec, *, source_repo: str | Path) -> Tree:
    """:func:`create` with ``github_url`` resolved from ``source_repo``'s ``origin``.

    The Tree clones from — and points ``origin`` at — exactly the URL the source
    checkout already uses, so auth and ``gh`` behave identically inside the Tree.
    """
    source = str(source_repo)
    url = gh.git_remote_url(cwd=source)
    return create(spec, source_repo=source, github_url=url)
