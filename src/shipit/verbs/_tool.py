"""Shared shell pieces of the tree-input Tool verbs (``test``, ``build``).

Every tree-input Tool verb (ADR-0039) has the same rim: split the raw CLI
args into (selector, passthrough), read the ``.shipit.toml`` map, and turn a
missing map into the pointed per-verb error. Extracted here (TOL01-WS02) so
``shipit build`` reuses the exact boundary ``shipit test`` shipped rather
than re-implementing it; the verbs keep their own run loops, timeouts, and
reporting — this module is the rim, not the wheel.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .. import config

#: Root-level manifest basenames → the toolchain they signal, for the pointed
#: missing-map error only. This is DIAGNOSIS-side detection (what would this
#: repo probably declare?), deliberately distinct from the declared map the
#: verbs dispatch on — mirrors the install catalog's provisioning-side
#: signals (:data:`shipit.install.reconcile.TOOLCHAIN_MANIFESTS`) without
#: conflating the two.
_SIGNAL_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("package.json", "npm"),
)


def split_args(
    args: Sequence[str], entries: Sequence[config.ToolchainEntry]
) -> tuple[str | None, tuple[str, ...]]:
    """``(selector, passthrough)`` from a Tool verb's raw args, resolved against
    the repo's legs. Pure.

    click consumes the first ``--`` before the verb sees the args, so
    ``shipit test tests/foo.py`` and ``shipit test -- tests/foo.py`` arrive
    identically — the selector/passthrough boundary cannot be read from the
    tokens alone, so it is read from ``entries`` (the repo's legs):

    - a leading ``-`` token → no selector; everything is passthrough
      (``shipit test -- -k foo``);
    - a first token that NAMES a leg (its toolchain or map path) → the
      selector; the rest is passthrough;
    - a first token that names no leg on a SINGLE-leg repo → the no-selector
      sugar: the one leg is unambiguous, so the whole tuple is passthrough
      (``shipit test tests/foo.py`` forwards the path to pytest);
    - a first token that names no leg on a MULTI-leg repo → still taken as the
      selector, so the planner rejects it loudly naming the known legs
      (passthrough on a multi-leg repo needs an explicit selector regardless).
    """
    if not args or args[0].startswith("-"):
        return None, tuple(args)
    first = args[0]
    names = {e.toolchain for e in entries} | {e.path for e in entries}
    if first in names or len(entries) > 1:
        return first, tuple(args[1:])
    return None, tuple(args)


def missing_map_message(root: Path, tool: str) -> str:
    """The pointed error for a repo with no ``[toolchains]`` map, naming the
    toolchains its root manifests signal (so the fix is a copy-paste away).
    """
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
    """The parsed ``.shipit.toml`` at ``root`` — ``{}`` when the file is
    absent (an absent config is a missing MAP, the verbs' pointed error, not
    a missing-file parse error). Malformed TOML raises
    :class:`~shipit.config.ConfigError` as usual."""
    cfg_path = root / config.CONFIG_NAME
    return config.load(cfg_path) if cfg_path.is_file() else {}


def require_entries(
    cfg: dict, root: Path, tool: str
) -> tuple[config.ToolchainEntry, ...]:
    """The typed ``[toolchains]`` map from ``cfg`` — the Tool verbs' dispatch
    axis. Raises :class:`~shipit.config.ConfigError` when the map is absent
    or empty (the pointed :func:`missing_map_message`) or malformed — all
    rendered by the shared :func:`~._errors.cli_errors` shell as
    ``error: …`` + exit 1.
    """
    entries = config.load_toolchains(cfg)
    if not entries:
        raise config.ConfigError(missing_map_message(root, tool))
    return entries
