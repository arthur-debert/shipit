"""``pixienv/read`` — the I/O boundary that hands pixi's JSON to the functional core.

The thin **edge** (ADR-0021): read the on-disk ``conda-meta/pixi`` and shell out to
``pixi shell-hook --json``, then delegate to the pure parsers in
:mod:`shipit.pixienv.model`. All effects live here so the core stays fixture-testable;
the subprocess call takes an injectable ``runner`` (default :func:`shipit.proc.run`) so
tests assert argv/parse without spawning ``pixi``.
"""

from __future__ import annotations

from pathlib import Path

from .. import proc
from .model import Activation, EnvIdentity, parse_activation, parse_env_identity

#: The ``conda-meta`` subdirectory of an env prefix, where pixi persists per-env state.
CONDA_META = "conda-meta"

#: pixi's rich env-identity record inside ``conda-meta`` (JSON; see :class:`EnvIdentity`).
ENV_IDENTITY_FILE = "pixi"

#: The bare sync-state digest pixi writes beside it — a DIFFERENT digest from
#: :attr:`EnvIdentity.environment_lock_file_hash` (docs/dev/pixi §2); do not conflate them.
FINGERPRINT_FILE = ".pixi-environment-fingerprint"


def env_identity_path(prefix: Path) -> Path:
    """The ``conda-meta/pixi`` path inside an env ``prefix``."""
    return Path(prefix) / CONDA_META / ENV_IDENTITY_FILE


def read_env_identity(prefix: Path) -> EnvIdentity:
    """Read + parse ``<prefix>/conda-meta/pixi`` into an :class:`EnvIdentity`."""
    return parse_env_identity(env_identity_path(prefix).read_text())


def read_fingerprint(prefix: Path) -> str | None:
    """The bare ``.pixi-environment-fingerprint`` digest, or ``None`` when absent.

    Distinct from :attr:`EnvIdentity.environment_lock_file_hash` — both are sync-state,
    but they are different digests for the same prefix and must not be conflated.
    """
    path = Path(prefix) / CONDA_META / FINGERPRINT_FILE
    if not path.exists():
        return None
    return path.read_text().strip()


def shell_hook(
    manifest_path: Path,
    *,
    environment: str | None = None,
    runner=proc.run,
) -> Activation:
    """Run ``pixi shell-hook --json`` for ``manifest_path`` and parse the :class:`Activation`.

    ``environment`` selects a non-default pixi environment. ``runner`` is the injectable
    subprocess boundary (default :func:`shipit.proc.run`); it must return an object with a
    ``.stdout`` string. shipit consumes pixi's activation output here rather than computing
    a rival (ADR-0022).
    """
    cmd = ["pixi", "shell-hook", "--json", "--manifest-path", str(manifest_path)]
    if environment is not None:
        cmd += ["--environment", environment]
    result = runner(cmd)
    return parse_activation(result.stdout)
