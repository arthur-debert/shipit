"""Shared shell pieces of the Tool verbs (``test``, ``build``, ``e2e``)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .. import config

_SIGNAL_MANIFESTS: tuple[tuple[str, str], ...] = config.SIGNAL_MANIFESTS


def split_args(
    args: Sequence[str], entries: Sequence[config.ToolchainEntry]
) -> tuple[str | None, tuple[str, ...]]:
    """``(selector, passthrough)`` from a Tool verb's raw args, resolved against the repo's legs (click has already eaten the first ``--``)."""
    if not args or args[0].startswith("-"):
        return None, tuple(args)
    first = args[0]
    names = {e.toolchain for e in entries} | {e.path for e in entries}
    if first in names or len(entries) > 1:
        return first, tuple(args[1:])
    return None, tuple(args)


def missing_map_message(root: Path, tool: str) -> str:
    """The pointed error for a repo with no ``[toolchains]`` map, naming the toolchains its root manifests signal."""
    signals = [
        f'"{name}" -> {tc}' for name, tc in _SIGNAL_MANIFESTS if (root / name).is_file()
    ]
    hint = f" This repo's manifests suggest: {'; '.join(signals)}." if signals else ""
    example = next(
        (tc for name, tc in _SIGNAL_MANIFESTS if (root / name).is_file()), "rust"
    )
    return (
        f"no [toolchains] path->toolchain map in {config.CONFIG_NAME} — "
        f"`shipit {tool}` dispatches on that declaration (ADR-0007/0039)."
        f'{hint} Declare it under a [toolchains] table, e.g. "." = "{example}".'
    )


def load_config(root: Path) -> dict:
    """The parsed ``.shipit.toml`` at ``root`` — ``{}`` when absent; malformed TOML raises ConfigError."""
    cfg_path = root / config.CONFIG_NAME
    return config.load(cfg_path) if cfg_path.is_file() else {}


def require_entries(
    cfg: dict, root: Path, tool: str
) -> tuple[config.ToolchainEntry, ...]:
    """The typed ``[toolchains]`` map from ``cfg``; raises ConfigError when it is absent, empty, or malformed."""
    entries = config.load_toolchains(cfg)
    if not entries:
        raise config.ConfigError(missing_map_message(root, tool))
    return entries
