"""`shipit stage` — copy resolved conda files from the env prefix into the app bundle."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from .. import config, staging
from ._errors import cli_errors
from ._render import emit
from ._tool import load_config

logger = logging.getLogger("shipit.stage")


@click.command(name="stage")
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False))
@click.option(
    "--feature",
    "feature",
    default=None,
    metavar="FEATURE",
    help="The pixi feature/env whose prefix to stage from (default: the default env, "
    "where conda-direct's plain consumer-owned deps resolve).",
)
def cmd(path: str | None, feature: str | None) -> None:
    """Copy this repo's `[stage]` files from the resolved conda env prefix into its bundle."""
    raise SystemExit(run(path, feature=feature))


@cli_errors
def run(path: str | None = None, *, feature: str | None = None) -> int:
    """Load the `[stage]` map and stage every entry from the env prefix."""
    root = Path(path or ".").resolve()
    entries = config.load_stage(load_config(root))
    staged = staging.stage(root, entries, feature=feature)
    emit(staged, format_staged)
    return 0


def format_staged(staged: list[staging.StagedFile]) -> str:
    """One line per staged file or dir, off the typed result."""
    if not staged:
        return "stage: nothing to stage — the [stage] map is absent or empty."
    lines = [f"stage: copied {len(staged)} item(s) from the env prefix:"]
    for item in staged:
        kind = "dir " if item.is_dir else "file"
        exec_note = " (executable)" if item.executable and not item.is_dir else ""
        lines.append(f"  {kind} {item.source} -> {item.dest}{exec_note}")
    return "\n".join(lines)
