"""``pixienv/model`` — pixi's env/activation model, mirrored as value objects.

The functional core of the ``pixienv`` deep module (ADR-0021 / ADR-0022): pixi's
persisted env identity and its activation snapshot become **thin, frozen value
objects**, and "what activation adds" is a **pure transform over immutable env
snapshots** — never a hand-derived rival to pixi's own computation.

Four shapes are mirrored, each straight from pixi's JSON:

- :class:`EnvIdentity` ← ``.pixi/envs/<env>/conda-meta/pixi`` — the RICH env-identity
  record (which manifest / env / lock-hash / pixi-version materialised this prefix).
  Its ``environment_lock_file_hash`` is a DIFFERENT digest from the bare
  ``conda-meta/.pixi-environment-fingerprint`` (observed ``99f00798db0ea80c`` vs
  ``99b739d0fedb92eb`` for the same prefix); the two must not be conflated
  (docs/dev/pixi §2).
- :class:`Activation` ← ``pixi shell-hook --json`` — the env vars pixi sets on EVERY
  activation plus its ``activation_scripts``. shipit never re-derives this; it consumes
  pixi's output and transforms it (:func:`activation_delta` / :func:`activated_env`).
- :class:`InstalledPackage` ← ``pixi list --json`` — what one environment actually
  holds, per package (identity, resolver kind, explicitness).
- :class:`Info` (+ :class:`ProjectInfo` / :class:`EnvironmentInfo`) ←
  ``pixi info --json`` — the machine/workspace snapshot: pixi version, platform,
  cache dir, and each declared environment's surface.

Everything here is pure: the parse functions take an already-captured JSON string and
return value objects, so a fixture pixi-JSON blob feeds straight in (Testing Decisions).
The I/O boundary — read the on-disk file, shell out to ``pixi shell-hook`` — lives in
:mod:`shipit.pixienv.read`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class Platform:
    """A pixi platform: its conda ``subdir`` and the virtual packages it resolved with.

    Mirrors the ``resolved_platform`` / ``minimum_supported_platform`` shape inside
    ``conda-meta/pixi``. ``virtual_packages`` is a tuple (order-preserving, immutable)
    of the ``__name=version=build`` tokens pixi records (e.g. ``__osx=13.0``).
    """

    subdir: str
    virtual_packages: tuple[str, ...]


@dataclass(frozen=True)
class EnvIdentity:
    """Which manifest / env / lock / pixi-version materialised a prefix.

    A faithful mirror of ``conda-meta/pixi``: the natural record to answer "what is
    this environment?" without re-deriving anything. ``environment_lock_file_hash`` is
    the lock-tied digest pixi persists here — DISTINCT from the bare
    ``.pixi-environment-fingerprint`` (see the module docstring); both are SYNC-STATE
    (they move when the lock moves), not a stable install id.
    """

    manifest_path: Path
    environment_name: str
    pixi_version: str
    environment_lock_file_hash: str
    resolved_platform: Platform


@dataclass(frozen=True)
class Activation:
    """A snapshot of what pixi injects on activation, straight from ``shell-hook --json``.

    ``environment_variables`` is the COMPLETE set of vars pixi sets when it activates the
    environment (PATH munge + ``CONDA_*`` + any ``[activation.env]``); it is exposed as a
    read-only mapping so the snapshot cannot be mutated after capture. ``activation_scripts``
    is the (usually empty) list of extra scripts pixi sources. This is the ONLY source of
    activation truth shipit uses — the delta transforms below read it, they never recompute it.

    ``frozen=True`` freezes only the *field binding*, not the referent: a caller could pass
    (and retain a handle to) a mutable ``dict``. :meth:`__post_init__` therefore snapshots
    whatever ``Mapping`` is passed into a private fresh ``dict`` the caller cannot reach and
    re-binds the field to a :class:`~types.MappingProxyType` view of it — so the value object
    is genuinely read-only end to end (ADR-0021 value-object discipline), regardless of how it
    was constructed.
    """

    environment_variables: Mapping[str, str]
    activation_scripts: tuple[str, ...]

    def __post_init__(self) -> None:
        # object.__setattr__ is the frozen-dataclass-sanctioned way to normalize a field
        # in __post_init__: copy into a fresh dict (severing any caller-held reference),
        # then expose it through a read-only MappingProxyType.
        object.__setattr__(
            self,
            "environment_variables",
            MappingProxyType(dict(self.environment_variables)),
        )


def _platform(data: Mapping[str, object]) -> Platform:
    """Build a :class:`Platform` from a ``{subdir, virtual_packages}`` JSON object."""
    packages = data.get("virtual_packages") or ()
    return Platform(
        subdir=str(data.get("subdir", "")),
        virtual_packages=tuple(str(p) for p in packages),
    )


def env_identity_from_dict(data: Mapping[str, object]) -> EnvIdentity:
    """Build an :class:`EnvIdentity` from an already-parsed ``conda-meta/pixi`` object."""
    platform = data.get("resolved_platform") or {}
    return EnvIdentity(
        manifest_path=Path(str(data["manifest_path"])),
        environment_name=str(data["environment_name"]),
        pixi_version=str(data["pixi_version"]),
        environment_lock_file_hash=str(data["environment_lock_file_hash"]),
        resolved_platform=_platform(platform if isinstance(platform, Mapping) else {}),
    )


def parse_env_identity(text: str) -> EnvIdentity:
    """Parse the JSON text of a ``conda-meta/pixi`` file into an :class:`EnvIdentity`."""
    return env_identity_from_dict(json.loads(text))


def activation_from_dict(data: Mapping[str, object]) -> Activation:
    """Build an :class:`Activation` from an already-parsed ``shell-hook --json`` object."""
    env = data.get("environment_variables") or {}
    scripts = data.get("activation_scripts") or ()
    env_map = {str(k): str(v) for k, v in dict(env).items()}
    # `Activation.__post_init__` snapshots + freezes this dict into a read-only view.
    return Activation(
        environment_variables=env_map,
        activation_scripts=tuple(str(s) for s in scripts),
    )


def parse_activation(text: str) -> Activation:
    """Parse the JSON text of ``pixi shell-hook --json`` into an :class:`Activation`."""
    return activation_from_dict(json.loads(text))


def activation_delta(base: Mapping[str, str], activation: Activation) -> dict[str, str]:
    """The vars activation ADDS or CHANGES relative to ``base`` — a pure transform.

    Given the env snapshot BEFORE activation and the :class:`Activation` pixi reports,
    return only the keys pixi introduces or overrides (a key whose value already equals
    ``base``'s is not in the delta). This is "what activation adds", computed FROM pixi's
    JSON rather than hand-derived — the whole point of borrowing pixi's model (ADR-0022).
    Neither input is mutated; a fresh dict is returned.
    """
    return {
        key: value
        for key, value in activation.environment_variables.items()
        if base.get(key) != value
    }


def activated_env(base: Mapping[str, str], activation: Activation) -> dict[str, str]:
    """``base`` with pixi's activation vars applied over it — a pure snapshot→snapshot map.

    The immutable-snapshot transform ADR-0022 asks for: take a base env snapshot, lay
    pixi's activation env vars on top, return the resulting snapshot as a new dict. Inputs
    are untouched.
    """
    return {**base, **activation.environment_variables}


@dataclass(frozen=True)
class InstalledPackage:
    """One installed package, straight from a ``pixi list --json`` entry.

    A thin mirror of the fields shipit reasons about — identity (``name`` /
    ``version`` / ``build``), which resolver owns it (``kind``: ``conda`` or
    ``pypi``), and whether the workspace asked for it directly (``is_explicit``).
    ``version`` and ``build`` are ``None`` where pixi reports null (an editable
    path dependency has no version; a pypi package has no conda build string).
    """

    name: str
    version: str | None
    build: str | None
    kind: str
    is_explicit: bool


def installed_package_from_dict(data: Mapping[str, object]) -> InstalledPackage:
    """Build an :class:`InstalledPackage` from one parsed ``pixi list`` entry."""
    version = data.get("version")
    build = data.get("build")
    return InstalledPackage(
        name=str(data["name"]),
        version=None if version is None else str(version),
        build=None if build is None else str(build),
        kind=str(data.get("kind", "")),
        is_explicit=bool(data.get("is_explicit", False)),
    )


def parse_installed_packages(text: str) -> tuple[InstalledPackage, ...]:
    """Parse the JSON text of ``pixi list --json`` into installed packages."""
    return tuple(installed_package_from_dict(entry) for entry in json.loads(text))


@dataclass(frozen=True)
class ProjectInfo:
    """The ``project_info`` block of ``pixi info --json``: which workspace this is."""

    name: str
    manifest_path: Path


@dataclass(frozen=True)
class EnvironmentInfo:
    """One ``environments_info`` entry of ``pixi info --json``.

    The declared surface of one pixi environment — its features, dependency names
    (conda and pypi), task names, and the prefix pixi materializes it at.
    """

    name: str
    features: tuple[str, ...]
    dependencies: tuple[str, ...]
    pypi_dependencies: tuple[str, ...]
    tasks: tuple[str, ...]
    prefix: Path


@dataclass(frozen=True)
class Info:
    """The ``pixi info --json`` snapshot shipit consumes.

    ``pixi_version`` mirrors pixi's top-level ``version`` field (renamed for the
    same reason :class:`EnvIdentity` calls it ``pixi_version``: at a call site a
    bare ``version`` reads as the PROJECT's). ``project`` is ``None`` outside a
    workspace — ``pixi info`` answers machine-level questions there too.
    """

    pixi_version: str
    platform: str
    cache_dir: Path | None
    project: ProjectInfo | None
    environments: tuple[EnvironmentInfo, ...]


def _environment_info(data: Mapping[str, object]) -> EnvironmentInfo:
    """Build an :class:`EnvironmentInfo` from one parsed ``environments_info`` entry."""

    def names(key: str) -> tuple[str, ...]:
        return tuple(str(item) for item in (data.get(key) or ()))

    return EnvironmentInfo(
        name=str(data.get("name", "")),
        features=names("features"),
        dependencies=names("dependencies"),
        pypi_dependencies=names("pypi_dependencies"),
        tasks=names("tasks"),
        prefix=Path(str(data.get("prefix", ""))),
    )


def info_from_dict(data: Mapping[str, object]) -> Info:
    """Build an :class:`Info` from an already-parsed ``pixi info --json`` object."""
    project = data.get("project_info")
    cache = data.get("cache_dir")
    return Info(
        pixi_version=str(data.get("version", "")),
        platform=str(data.get("platform", "")),
        cache_dir=None if cache is None else Path(str(cache)),
        project=(
            None
            if not isinstance(project, Mapping)
            else ProjectInfo(
                name=str(project.get("name", "")),
                manifest_path=Path(str(project["manifest_path"])),
            )
        ),
        environments=tuple(
            _environment_info(entry)
            for entry in (data.get("environments_info") or ())
            if isinstance(entry, Mapping)
        ),
    )


def parse_info(text: str) -> Info:
    """Parse the JSON text of ``pixi info --json`` into an :class:`Info`."""
    return info_from_dict(json.loads(text))


def path_entries(activation: Activation) -> tuple[str, ...]:
    """The ``PATH`` pixi's activation sets, split into its entries (empty tuple if unset).

    pixi's dominant activation effect is prepending the env's ``bin`` to ``PATH``; exposing
    it split (on :data:`os.pathsep`) lets callers reason about the resolved tool path without
    re-parsing the raw string at every site.
    """
    raw = activation.environment_variables.get("PATH")
    if not raw:
        return ()
    return tuple(raw.split(os.pathsep))
