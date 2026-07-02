"""``pixienv/read`` — the I/O boundary that hands pixi's JSON to the functional core.

The thin **edge** (ADR-0021): read the on-disk ``conda-meta/pixi`` and shell out to
pixi's machine-readable read verbs — ``shell-hook``/``list``/``info``, each
harvested as ``--json`` (native JSON over any scrape, ADR-0028) — then delegate to
the pure parsers in :mod:`shipit.pixienv.model`. All effects live here so the core
stays fixture-testable; each Exec takes an injectable ``runner`` (default
:func:`shipit.execrun.run`) so tests assert argv/parse without spawning ``pixi``.
The execution side of the adapter (install, run-wrapping) is
:mod:`shipit.pixienv.run`.
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

#: The ``conda-meta`` subdirectory of an env prefix, where pixi persists per-env state.
CONDA_META = "conda-meta"

#: pixi's rich env-identity record inside ``conda-meta`` (JSON; see :class:`EnvIdentity`).
ENV_IDENTITY_FILE = "pixi"

#: The bare sync-state digest pixi writes beside it — a DIFFERENT digest from
#: :attr:`EnvIdentity.environment_lock_file_hash` (docs/dev/pixi §2); do not conflate them.
FINGERPRINT_FILE = ".pixi-environment-fingerprint"

#: The read verbs' stated timeout, in seconds (ADR-0028: every Exec states its
#: bound deliberately — never the runner's implicit default). A ``--json`` read
#: against a provisioned env answers in seconds, so the runner's default IS the
#: right bound — stated on the wire rather than inherited, so the
#: no-implicit-timeout sweep stays grep-verifiable. The execution side's
#: long-runner bound is :data:`shipit.pixienv.run.INSTALL_TIMEOUT`.
READ_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def env_identity_path(prefix: Path) -> Path:
    """The ``conda-meta/pixi`` path inside an env ``prefix``."""
    return Path(prefix) / CONDA_META / ENV_IDENTITY_FILE


def read_env_identity(prefix: Path) -> EnvIdentity | None:
    """Read + parse ``<prefix>/conda-meta/pixi`` into an :class:`EnvIdentity`.

    Returns ``None`` when the file is absent (an un-provisioned or partially-built
    prefix has no ``conda-meta/pixi`` yet), mirroring :func:`read_fingerprint` rather
    than raising ``FileNotFoundError`` — the caller decides what a missing identity means.
    """
    path = env_identity_path(prefix)
    if not path.exists():
        return None
    return parse_env_identity(path.read_text())


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
    runner=None,
) -> Activation:
    """Run ``pixi shell-hook --json`` for ``manifest_path`` and parse the :class:`Activation`.

    ``environment`` selects a non-default pixi environment. ``runner`` is the injectable
    Exec boundary (default :func:`shipit.execrun.run`); it must return an object with a
    ``.stdout`` string. The Exec states :data:`READ_TIMEOUT`. shipit consumes pixi's
    activation output here rather than computing a rival (ADR-0022).
    """
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
    """Run ``pixi list --json`` for ``manifest_path`` and parse the installed packages.

    The native-JSON harvest of the read verb (ADR-0028): what an environment actually
    holds, as :class:`InstalledPackage` value objects — never a scrape of the human
    table. ``environment`` selects a non-default pixi environment; ``runner`` is the
    injectable Exec boundary. The Exec states :data:`READ_TIMEOUT` — a read verb
    against a provisioned env answers in seconds.
    """
    if runner is None:
        runner = execrun.run
    cmd = ["pixi", "list", "--json", "--manifest-path", str(manifest_path)]
    if environment is not None:
        cmd += ["--environment", environment]
    result = runner(cmd, timeout=READ_TIMEOUT)
    return parse_installed_packages(result.stdout)


def info(manifest_path: Path, *, runner=None) -> Info:
    """Run ``pixi info --json`` for ``manifest_path`` and parse the :class:`Info`.

    The machine/workspace snapshot straight from pixi's own JSON (ADR-0022: borrow,
    never re-derive): pixi version, platform, cache dir, and every declared
    environment's surface. ``runner`` is the injectable Exec boundary; the Exec
    states :data:`READ_TIMEOUT` (a pure read, no solve).
    """
    if runner is None:
        runner = execrun.run
    result = runner(
        ["pixi", "info", "--json", "--manifest-path", str(manifest_path)],
        timeout=READ_TIMEOUT,
    )
    return parse_info(result.stdout)
