"""``pixienv/read`` — the I/O boundary that hands pixi's JSON to the pure parsers.

Every Exec takes an injectable runner.
"""

from __future__ import annotations

from pathlib import Path

from .. import execrun
from .model import (
    Activation,
    EnvIdentity,
    Info,
    InstalledPackage,
    parse_activation,
    parse_env_identity,
    parse_info,
    parse_installed_packages,
)

CONDA_META = "conda-meta"

ENV_IDENTITY_FILE = "pixi"

#: A DIFFERENT digest from :attr:`EnvIdentity.environment_lock_file_hash`.
FINGERPRINT_FILE = ".pixi-environment-fingerprint"

#: Stated rather than inherited, so the no-implicit-timeout sweep stays verifiable.
READ_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def env_identity_path(prefix: Path) -> Path:
    return Path(prefix) / CONDA_META / ENV_IDENTITY_FILE


def read_env_identity(prefix: Path) -> EnvIdentity | None:
    """``None`` when the file is absent — an un-provisioned prefix has none yet."""
    path = env_identity_path(prefix)
    if not path.exists():
        return None
    return parse_env_identity(path.read_text())


def read_fingerprint(prefix: Path) -> str | None:
    """The bare ``.pixi-environment-fingerprint`` digest, or ``None`` when absent."""
    path = Path(prefix) / CONDA_META / FINGERPRINT_FILE
    if not path.exists():
        return None
    return path.read_text().strip()


def shell_hook(
    manifest_path: Path,
    *,
    environment: str | None = None,
    runner=None,
) -> Activation:
    if runner is None:
        runner = execrun.run
    cmd = ["pixi", "shell-hook", "--json", "--manifest-path", str(manifest_path)]
    if environment is not None:
        cmd += ["--environment", environment]
    result = runner(cmd, timeout=READ_TIMEOUT)
    return parse_activation(result.stdout)


def list_packages(
    manifest_path: Path,
    *,
    environment: str | None = None,
    runner=None,
) -> tuple[InstalledPackage, ...]:
    if runner is None:
        runner = execrun.run
    cmd = ["pixi", "list", "--json", "--manifest-path", str(manifest_path)]
    if environment is not None:
        cmd += ["--environment", environment]
    result = runner(cmd, timeout=READ_TIMEOUT)
    return parse_installed_packages(result.stdout)


def info(manifest_path: Path, *, runner=None) -> Info:
    if runner is None:
        runner = execrun.run
    result = runner(
        ["pixi", "info", "--json", "--manifest-path", str(manifest_path)],
        timeout=READ_TIMEOUT,
    )
    return parse_info(result.stdout)
