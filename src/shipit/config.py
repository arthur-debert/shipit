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

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = ".shipit.toml"

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


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


# --------------------------------------------------------------------------
# The [shipit] / [managed] manifest — written by ``shipit install``
# --------------------------------------------------------------------------
#
# ``[shipit].version`` pins the shipit commit that last wrote the managed set;
# ``[managed]`` is the per-unit pristine-hash map the next re-install compares
# against (docs/dev/architecture.lex §6, ROADMAP.lex §2). tomllib is read-only,
# so the writer below hand-serializes these two flat string tables and splices
# them into an existing file, leaving any ``[secrets]`` (and anything else the
# consumer owns) textually untouched.


def content_hash(data: bytes) -> str:
    """The ``sha256:<hex>`` pristine hash of a managed unit's content."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_managed(cfg: dict) -> dict[str, str]:
    """The ``[managed]`` pristine map (path → ``sha256:...``); ``{}`` when absent."""
    managed = cfg.get("managed", {})
    if not isinstance(managed, dict):
        raise ConfigError("[managed] must be a table")
    return {str(k): str(v) for k, v in managed.items()}


def shipit_version(cfg: dict) -> str | None:
    """The ``[shipit].version`` pin, or ``None`` when absent."""
    section = cfg.get("shipit", {})
    if not isinstance(section, dict):
        raise ConfigError("[shipit] must be a table")
    value = section.get("version")
    return str(value) if value is not None else None


def _toml_key(key: str) -> str:
    """A TOML key, bare when it can be and quoted (paths, ``#``) otherwise."""
    if _BARE_KEY.fullmatch(key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip_tables(text: str, tables: set[str]) -> str:
    """Drop the given top-level tables (header + body) from TOML ``text``."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped.strip("[]").strip()
            skipping = name in tables
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    return "\n".join(out).strip()


def dump_manifest(version: str, managed: dict[str, str]) -> str:
    """Serialize the ``[shipit]`` and ``[managed]`` tables to TOML text."""
    lines = ["[shipit]", f'version = "{version}"', "", "[managed]"]
    for key, value in managed.items():
        lines.append(f'{_toml_key(key)} = "{value}"')
    return "\n".join(lines) + "\n"


def write_manifest(
    path: str | Path, *, version: str, managed: dict[str, str]
) -> None:
    """Write the ``[shipit]``/``[managed]`` tables, preserving the rest of the file."""
    p = Path(path)
    existing = p.read_text(encoding="utf-8") if p.is_file() else ""
    kept = _strip_tables(existing, {"shipit", "managed"})
    block = dump_manifest(version, managed)
    text = f"{kept}\n\n{block}" if kept else block
    p.write_text(text, encoding="utf-8")
