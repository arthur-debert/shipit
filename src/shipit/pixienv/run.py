"""``pixienv/run`` — the execution side of the pixi Tool adapter.

Every ``pixi`` argv shipit executes or hands to a launcher is built here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import platformdirs

from .. import execrun

MANIFEST_NAME = "pixi.toml"

#: The provisioned-env sentinel: a checkout has a usable env iff this dir exists.
DEFAULT_ENV_DIR = (".pixi", "envs", "default")

#: pixi's long-runner bound: a cold install outlives the Exec runner's default,
#: but ``None`` would let a wedged solve hang forever.
INSTALL_TIMEOUT: float = 30 * 60.0


def has_default_env(root: str | Path) -> bool:
    """A stray FILE at :data:`DEFAULT_ENV_DIR` is not a provisioned env."""
    return Path(root).joinpath(*DEFAULT_ENV_DIR).is_dir()


def run_argv(
    argv: list[str], root: str | Path, *, environment: str | None = None
) -> list[str]:
    """The explicit ``--manifest-path`` overrides any leaked ``PIXI_PROJECT_MANIFEST``."""
    cmd = ["pixi", "run", "--manifest-path", str(Path(root) / MANIFEST_NAME)]
    if environment is not None:
        cmd += ["--environment", environment]
    return [*cmd, "--", *argv]


def install(
    root: str | Path,
    *,
    environment: str | None = None,
    env: Mapping[str, str] | None = None,
    runner=None,
) -> execrun.ExecResult:
    """``env``, when given, REPLACES the child environment; ``None`` inherits it."""
    if runner is None:
        runner = execrun.run
    argv = ["pixi", "install"]
    if environment is not None:
        argv += ["--environment", environment]
    return runner(
        argv,
        cwd=str(root),
        env=None if env is None else dict(env),
        replace_env=env is not None,
        timeout=INSTALL_TIMEOUT,
    )


def run_in_env(
    argv: list[str],
    root: str | Path,
    *,
    environment: str | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = INSTALL_TIMEOUT,
    runner=None,
) -> execrun.ExecResult:
    if runner is None:
        runner = execrun.run
    return runner(
        run_argv(argv, root, environment=environment),
        cwd=str(root),
        env=None if env is None else dict(env),
        replace_env=env is not None,
        check=check,
        timeout=timeout,
    )


def run_task(
    task: str,
    root: str | Path,
    *,
    environment: str | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = INSTALL_TIMEOUT,
    runner=None,
) -> execrun.ExecResult:
    """Run the named pixi TASK ``task``, not an argv wrapped behind ``pixi run --``."""
    if runner is None:
        runner = execrun.run
    cmd = ["pixi", "run", "--manifest-path", str(Path(root) / MANIFEST_NAME)]
    if environment is not None:
        cmd += ["--environment", environment]
    cmd.append(task)
    return runner(
        cmd,
        cwd=str(root),
        env=None if env is None else dict(env),
        replace_env=env is not None,
        check=check,
        timeout=timeout,
    )


def cache_dir() -> Path:
    override = os.environ.get("PIXI_CACHE_DIR") or os.environ.get("RATTLER_CACHE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir("rattler")) / "cache"
