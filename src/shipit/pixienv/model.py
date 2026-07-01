"""``pixienv/model`` — pixi's env/activation model, mirrored as value objects.

The functional core of the ``pixienv`` deep module (ADR-0021 / ADR-0022): pixi's
persisted env identity and its activation snapshot become **thin, frozen value
objects**, and "what activation adds" is a **pure transform over immutable env
snapshots** — never a hand-derived rival to pixi's own computation.

Two shapes are mirrored, each straight from pixi's JSON:

- :class:`EnvIdentity` ← ``.pixi/envs/<env>/conda-meta/pixi`` — the RICH env-identity
  record (which manifest / env / lock-hash / pixi-version materialised this prefix).
  Its ``environment_lock_file_hash`` is a DIFFERENT digest from the bare
  ``conda-meta/.pixi-environment-fingerprint`` (observed ``99f00798db0ea80c`` vs
  ``99b739d0fedb92eb`` for the same prefix); the two must not be conflated
  (docs/dev/pixi §2).
- :class:`Activation` ← ``pixi shell-hook --json`` — the env vars pixi sets on EVERY
  activation plus its ``activation_scripts``. shipit never re-derives this; it consumes
  pixi's output and transforms it (:func:`activation_delta` / :func:`activated_env`).

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
    """

    environment_variables: Mapping[str, str]
    activation_scripts: tuple[str, ...]


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
    return Activation(
        environment_variables=MappingProxyType(env_map),
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
