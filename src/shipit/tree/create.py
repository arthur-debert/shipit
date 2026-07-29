"""``tree/create`` — clone, branch, ``.treeinclude``, provision, session store.

See docs/adr/0014-trees-dissociated-clones-central-root.md.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import (
    config,
    events,
    execrun,
    git,
    identity,
    logcontext,
    pixienv,
    sessionstore,
)
from ..install.apply import HOOK_ACTIVATE_ARGV, LEFTHOOK_BINARY
from ..install.units import LEFTHOOK_FILE, LINT_ENV
from . import include
from .layout import TreeSpec, central_root, plan

logger = logging.getLogger("shipit.tree")

NODE_MANIFEST = "package.json"

#: Package manager → its FROZEN install argv. Yarn is absent: its flag is
#: version-dependent, so it is resolved by :func:`_yarn_install_argv`.
NODE_INSTALL_ARGV: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "ci"),
    "pnpm": ("pnpm", "install", "--frozen-lockfile"),
}

NODE_MANAGERS: frozenset[str] = frozenset(NODE_INSTALL_ARGV) | {"yarn"}

#: Only yarn v1 stamps this banner; Berry (v2+) writes a YAML ``__metadata:`` map.
_YARN_V1_BANNER = "# yarn lockfile v1"

NODE_LOCKFILES: dict[str, str] = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
}

#: Per-step provisioning bound, in seconds: a cold install legitimately runs for
#: many minutes, so the runner's 5-minute default would kill it.
PROVISION_TIMEOUT: float = 30 * 60.0

_STAMP_FORMAT = "%Y%m%d-%H%M%S"


@dataclass(frozen=True)
class Tree:
    path: str
    branch: str
    base: str


def new_tree_id() -> str:
    return str(uuid.uuid4())


def new_tree_naming(agent: str, *, tree_id: str | None = None) -> dict[str, str]:
    """The minted leaf coordinates to spread into a :class:`~shipit.tree.layout.TreeSpec`."""
    return {
        "agent": agent,
        "created": tree_created_stamp(),
        "tree_id": tree_id or new_tree_id(),
    }


def tree_created_stamp(now: float | None = None) -> str:
    """The leaf's ``<timestamp>`` — ``%Y%m%d-%H%M%S`` UTC over an injectable clock."""
    seconds = time.time() if now is None else now
    return time.strftime(_STAMP_FORMAT, time.gmtime(seconds))


def create(spec: TreeSpec, *, source_repo: str, github_url: str) -> Tree:
    """Clone ``source_repo``, ``origin`` at ``github_url``; a failure rolls the leaf back."""
    tree_plan = plan(spec)
    dest = tree_plan.dir
    trees_root = spec.root if spec.root is not None else central_root()
    session = spec.ephemeral or (spec.session if spec.issue is not None else None)
    with logcontext.scoped(tree=str(dest), session=session):
        if dest.exists():
            raise FileExistsError(
                f"tree dir already exists: {dest}; refusing to clone so a failed "
                "create never deletes a pre-existing checkout (rerun, or hash collision)."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        logger.debug(
            "tree cloning %s -> %s (branch %s, base %s)",
            github_url,
            dest,
            tree_plan.branch,
            tree_plan.base,
        )
        try:
            git.clone_dissociated(github_url, str(dest), reference=source_repo)
            # Before the first fetch: `fetch.writeCommitGraph` is what would poison
            # this clone as a `--reference` donor for its own children.
            git.configure_safe_reference_donor(cwd=str(dest))
            git.fetch(cwd=str(dest))
            git.checkout_create_or_reset(
                tree_plan.branch, tree_plan.base, cwd=str(dest)
            )
            # A dissociated clone leaves submodules as EMPTY gitlink dirs.
            git.submodule_update_init(cwd=str(dest))
            copied = include.apply(source_repo, dest)
            logger.debug(
                "tree copied %d .treeinclude file(s) into %s", len(copied), dest
            )
            _provision(dest, trees_root=Path(trees_root))
        except BaseException:
            logger.error(
                "tree create failed after %dms; removing half-built leaf %s",
                _elapsed_ms(started),
                dest,
                exc_info=True,
            )
            shutil.rmtree(dest, ignore_errors=True)
            raise

        _plant_session_store(dest)

        duration_ms = _elapsed_ms(started)
        events.emit(
            logger,
            "tree.created",
            "tree created at %s (branch %s, base %s) in %dms",
            dest,
            tree_plan.branch,
            tree_plan.base,
            duration_ms,
            extra={"duration_ms": duration_ms},
        )
        return Tree(path=str(dest), branch=tree_plan.branch, base=tree_plan.base)


def _plant_session_store(dest: Path) -> None:
    """Point the Tree's harness slug dir at its repo's one session store. Fail-open."""
    try:
        repo = identity.resolve_repo(str(dest))
        sessionstore.plant(dest, repo)
    except Exception:  # noqa: BLE001 — fail-open: never cost a Tree its creation
        logger.debug("session store not planted for %s", dest, exc_info=True)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def provision_env() -> dict[str, str]:
    """The COMPLETE child env for provisioning — used verbatim, with ``replace_env=True``."""
    return pixienv.scrub_env(os.environ)


def run_provision(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run one provisioning command in the Tree — the patchable provisioning boundary."""
    result = execrun.run(
        cmd, cwd=str(cwd), env=env, replace_env=True, timeout=PROVISION_TIMEOUT
    )
    _narrate_step(result)


def _narrate_step(result: execrun.ExecResult) -> None:
    logger.info(
        "provision step %s completed in %dms",
        shlex.join(result.argv),
        result.duration_ms,
        extra={"duration_ms": result.duration_ms},
    )


def _yarn_install_argv(*, classic: bool) -> list[str]:
    """Yarn's frozen argv: v1 takes ``--frozen-lockfile``, Berry only ``--immutable``."""
    flag = "--frozen-lockfile" if classic else "--immutable"
    return ["yarn", "install", flag]


def _yarn_pin_is_classic(pin: str, manifest: Path) -> bool:
    """Whether a ``yarn@<version>`` corepack pin names the v1 line; raises on no major."""
    _, _, version = pin.partition("@")
    major = version.split(".", 1)[0]
    try:
        return int(major) <= 1
    except ValueError as exc:
        raise ValueError(
            f"unparseable yarn version in packageManager {pin!r} in {manifest}: "
            "corepack pins an exact <name>@<version>, so yarn's frozen-install flag "
            "(--frozen-lockfile for v1, --immutable for v2+) cannot be chosen (#545)"
        ) from exc


def _yarn_lockfile_is_classic(lockfile: Path) -> bool:
    with lockfile.open("r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(len(_YARN_V1_BANNER) + 256)
    return _YARN_V1_BANNER in head


def node_install_argv(dest: Path) -> list[str]:
    """The frozen node install argv: the ``packageManager`` pin wins, else the lone lockfile, else raise."""
    manifest = dest / NODE_MANIFEST
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ValueError(
            f"unparseable {NODE_MANIFEST} in {dest}: {exc} — cannot determine "
            "the package manager for the node-deps provisioning step (#543)"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{NODE_MANIFEST} in {dest} is JSON but not an object "
            f"({type(data).__name__}); a package.json is always an object, so no "
            "package manager can be read — failing loud like an unparseable one (#543)"
        )
    pin = data.get("packageManager")
    if pin is not None:
        name, sep, version = str(pin).partition("@")
        if not sep or not version:
            raise ValueError(
                f"malformed packageManager {pin!r} in {manifest}: corepack pins an "
                "exact <name>@<version>, so a bare name (no pinned version) is not a "
                "usable signal — failing loud rather than guessing a frozen install "
                "(#543)"
            )
        if name == "yarn":
            return _yarn_install_argv(classic=_yarn_pin_is_classic(str(pin), manifest))
        if name not in NODE_INSTALL_ARGV:
            raise ValueError(
                f"unsupported packageManager {pin!r} in {manifest}: known "
                f"managers are {sorted(NODE_MANAGERS)} (#543)"
            )
        return list(NODE_INSTALL_ARGV[name])
    found = [lock for lock in NODE_LOCKFILES if (dest / lock).is_file()]
    if len(found) == 1:
        manager = NODE_LOCKFILES[found[0]]
        if manager == "yarn":
            return _yarn_install_argv(
                classic=_yarn_lockfile_is_classic(dest / found[0])
            )
        return list(NODE_INSTALL_ARGV[manager])
    detail = (
        f"multiple lockfiles ({', '.join(found)})"
        if found
        else f"no recognized lockfile ({', '.join(NODE_LOCKFILES)})"
    )
    raise ValueError(
        f"{dest} has a {NODE_MANIFEST} but no packageManager field and {detail}; "
        "refusing to guess a frozen install — a wrong one hard-fails here or "
        "leaves deps unprovisioned that downstream lint legs fail open on (#543)"
    )


def _provision(dest: Path, *, trees_root: Path) -> None:
    """``pixi install`` + hook activation + frozen node install; a pinless base fails closed."""
    if config.shipit_pin(dest / config.CONFIG_NAME) is None:
        raise ValueError(
            f"repo {dest} has no [shipit].version pin — run the bootstrap "
            "`shipit install --pr` first (ADR-0033: a Tree rides its base's "
            "pinned shipit; a pinless base has nothing for bin/shipit to exec)"
        )
    env = provision_env()
    if (dest / pixienv.MANIFEST_NAME).is_file():
        _warn_if_cache_cross_filesystem(trees_root)
        _narrate_step(pixienv.install(dest, env=env))
        if (dest / LEFTHOOK_FILE).is_file():
            _activate_hooks(dest, env=env)
    if (dest / NODE_MANIFEST).is_file():
        run_provision(node_install_argv(dest), cwd=dest, env=env)


def _activate_hooks(dest: Path, *, env: dict[str, str]) -> None:
    """Arm the Tree's git hooks — hooks do not clone, so each Tree installs its own."""
    result = pixienv.run_in_env(
        [LEFTHOOK_BINARY, *HOOK_ACTIVATE_ARGV],
        dest,
        environment=LINT_ENV,
        env=env,
    )
    _narrate_step(result)


def _st_dev(path: Path) -> int:
    return os.stat(path).st_dev


def _nearest_dev(path: Path) -> int | None:
    """``path``'s device id, or its nearest existing ancestor's when it is not there yet."""
    for candidate in (path, *path.parents):
        try:
            return _st_dev(candidate)
        except OSError:
            continue
    return None


def check_same_filesystem(trees_root: Path, cache_dir: Path) -> str | None:
    """The warning when cache and Trees root differ — pixi's linking degrades to copies."""
    trees_dev = _nearest_dev(trees_root)
    cache_dev = _nearest_dev(cache_dir)
    if trees_dev is None or cache_dev is None:
        return None
    if trees_dev != cache_dev:
        return (
            f"pixi cache ({cache_dir}) and Trees root ({trees_root}) are on "
            "different filesystems; package linking falls back to full copies, "
            "so Tree provisioning will be slower and use more disk (#119)."
        )
    return None


def _warn_if_cache_cross_filesystem(trees_root: Path) -> None:
    message = check_same_filesystem(trees_root, pixienv.cache_dir())
    if message:
        logger.warning(message)


def create_from_source(spec: TreeSpec, *, source_repo: str | Path) -> Tree:
    """:func:`create` with ``github_url`` resolved from ``source_repo``'s ``origin``."""
    source = str(source_repo)
    url = git.remote_url(cwd=source)
    return create(spec, source_repo=source, github_url=url)
