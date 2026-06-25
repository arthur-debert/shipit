"""Reading ``.shipit.toml`` — shipit's policy config.

``.shipit.toml`` owns policy (the secret map, reviewers, the path→toolchain map,
the pristine hashes); ``pixi.toml`` owns provisioning. They describe different
layers, so there is no split-brain (docs/dev/architecture.lex §6). Step 1 needs
only the ``[secrets]`` table.

The ``[secrets]`` table maps a GitHub secret NAME (the table key) to exactly one
source:

    [secrets]
    CARGO_REGISTRY_TOKEN = { doppler = "CRATES_IO_KEY" }
    GH_PAT               = { env = "SHIPIT_GH_PAT" }
    MANUAL_TOKEN         = { prompt = true }
    SCCACHE_GCS_KEY      = { doppler = "SCCACHE_GCS_KEY", optional = true }

``optional = true`` marks a source whose absence is a skip, not a fatal error.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = ".shipit.toml"


class ConfigError(RuntimeError):
    """``.shipit.toml`` is missing or malformed."""


@dataclass(frozen=True)
class SecretSource:
    """One ``[secrets]`` entry: the GitHub secret ``name`` and where it comes from.

    Exactly one of ``doppler`` / ``env`` / ``prompt`` is set, mirroring the
    single-source-per-entry schema.
    """

    name: str
    kind: str  # "doppler" | "env" | "prompt"
    key: str | None  # doppler KEY or env VAR; None for prompt
    optional: bool = False


def _parse_secret(name: str, spec: object) -> SecretSource:
    if not isinstance(spec, dict):
        raise ConfigError(
            f"[secrets].{name} must be an inline table, e.g. "
            f'{{ doppler = "KEY" }}; got {spec!r}'
        )
    optional = bool(spec.get("optional", False))
    sources = [k for k in ("doppler", "env", "prompt") if k in spec]
    if len(sources) != 1:
        raise ConfigError(
            f"[secrets].{name} must name exactly one source "
            f"(doppler / env / prompt); got {sources or 'none'}"
        )
    kind = sources[0]
    if kind == "prompt":
        if spec.get("prompt") is not True:
            raise ConfigError(f"[secrets].{name}: prompt must be `true`")
        return SecretSource(name=name, kind="prompt", key=None, optional=optional)
    key = spec[kind]
    if not isinstance(key, str) or not key:
        raise ConfigError(f"[secrets].{name}: {kind} must be a non-empty string")
    return SecretSource(name=name, kind=kind, key=key, optional=optional)


def load_secrets(spec: dict) -> list[SecretSource]:
    """Parse a ``[secrets]`` table (already loaded) into ordered sources."""
    secrets = spec.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ConfigError("[secrets] must be a table")
    return [_parse_secret(name, value) for name, value in secrets.items()]


def load(path: str | Path) -> dict:
    """Parse a ``.shipit.toml`` file into a dict, or raise :class:`ConfigError`."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"no {CONFIG_NAME} at {p}")
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed {p}: {exc}") from None
