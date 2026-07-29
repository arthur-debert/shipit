"""``pixienv/model`` — pixi's env/activation model, mirrored as frozen value objects.

See docs/adr/0022-borrow-pixis-model.md.
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
    subdir: str
    virtual_packages: tuple[str, ...]


@dataclass(frozen=True)
class EnvIdentity:
    """``environment_lock_file_hash`` is SYNC-STATE, and a DIFFERENT digest from the
    bare ``conda-meta/.pixi-environment-fingerprint`` — never a stable install id.
    """

    manifest_path: Path
    environment_name: str
    pixi_version: str
    environment_lock_file_hash: str
    resolved_platform: Platform


@dataclass(frozen=True)
class Activation:
    """``environment_variables`` is re-bound to a read-only snapshot: ``frozen``
    freezes the binding, not a caller-held mutable mapping.
    """

    environment_variables: Mapping[str, str]
    activation_scripts: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "environment_variables",
            MappingProxyType(dict(self.environment_variables)),
        )


def _platform(data: Mapping[str, object]) -> Platform:
    packages = data.get("virtual_packages") or ()
    return Platform(
        subdir=str(data.get("subdir", "")),
        virtual_packages=tuple(str(p) for p in packages),
    )


def env_identity_from_dict(data: Mapping[str, object]) -> EnvIdentity:
    platform = data.get("resolved_platform") or {}
    return EnvIdentity(
        manifest_path=Path(str(data["manifest_path"])),
        environment_name=str(data["environment_name"]),
        pixi_version=str(data["pixi_version"]),
        environment_lock_file_hash=str(data["environment_lock_file_hash"]),
        resolved_platform=_platform(platform if isinstance(platform, Mapping) else {}),
    )


def parse_env_identity(text: str) -> EnvIdentity:
    return env_identity_from_dict(json.loads(text))


def activation_from_dict(data: Mapping[str, object]) -> Activation:
    env = data.get("environment_variables") or {}
    scripts = data.get("activation_scripts") or ()
    env_map = {str(k): str(v) for k, v in dict(env).items()}
    return Activation(
        environment_variables=env_map,
        activation_scripts=tuple(str(s) for s in scripts),
    )


def parse_activation(text: str) -> Activation:
    return activation_from_dict(json.loads(text))


def activation_delta(base: Mapping[str, str], activation: Activation) -> dict[str, str]:
    """The vars activation adds or changes relative to ``base``; neither is mutated."""
    return {
        key: value
        for key, value in activation.environment_variables.items()
        if base.get(key) != value
    }


def activated_env(base: Mapping[str, str], activation: Activation) -> dict[str, str]:
    return {**base, **activation.environment_variables}


@dataclass(frozen=True)
class InstalledPackage:
    """``version``/``build`` are ``None`` where pixi reports null."""

    name: str
    version: str | None
    build: str | None
    kind: str
    is_explicit: bool


def installed_package_from_dict(data: Mapping[str, object]) -> InstalledPackage:
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
    return tuple(installed_package_from_dict(entry) for entry in json.loads(text))


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    manifest_path: Path


@dataclass(frozen=True)
class EnvironmentInfo:
    name: str
    features: tuple[str, ...]
    dependencies: tuple[str, ...]
    pypi_dependencies: tuple[str, ...]
    tasks: tuple[str, ...]
    prefix: Path


@dataclass(frozen=True)
class Info:
    """``project`` is ``None`` outside a workspace."""

    pixi_version: str
    platform: str
    cache_dir: Path | None
    project: ProjectInfo | None
    environments: tuple[EnvironmentInfo, ...]


def _environment_info(data: Mapping[str, object]) -> EnvironmentInfo:

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
    return info_from_dict(json.loads(text))


def path_entries(activation: Activation) -> tuple[str, ...]:
    """The activation's ``PATH`` split on :data:`os.pathsep`; ``()`` when unset."""
    raw = activation.environment_variables.get("PATH")
    if not raw:
        return ()
    return tuple(raw.split(os.pathsep))
