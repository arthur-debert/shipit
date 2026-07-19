"""`shipit stage` — copy resolved conda files from the env prefix into the app
consumer's bundle (conda-direct #1079), as ADR-0030 glue + a pure renderer.

The manifest-driven mirror of the legacy `fetch-deps`: it reads the consumer's
`[stage]` map (:func:`shipit.config.load_stage`) and copies each declared
source-in-prefix → dest-under-resources pair off the already-resolved pixi env
(:func:`shipit.staging.stage`). The domain does the work and construction-time
validation; this module validates the one primitive (PATH is an existing dir),
calls the domain, and renders the outcome.
"""

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
    """Copy this repo's `[stage]` files from the resolved conda env prefix into its bundle.

    PATH defaults to the current directory. For each `[stage.<pkg>]` entry —
    a source-in-prefix path (`bin/<tool>` for a tool, `share/<pkg>/…` for a data
    artifact) mapped to a dest under the checkout (`resources/…`) — this copies
    the file or directory pixi already extracted into `<PATH>/.pixi/envs/<env>`.
    Run it AFTER `shipit install`/`pixi install` has resolved the deps; a source
    that is not materialized is a hard error pointing at install (the step copies,
    it never fetches). A tool binary keeps its executable bit. Exit: 0 on success
    (an empty `[stage]` map is a clean no-op), 1 on a missing source or an escaping
    destination, 2 usage.
    """
    raise SystemExit(run(path, feature=feature))


@cli_errors
def run(path: str | None = None, *, feature: str | None = None) -> int:
    """Load the `[stage]` map and stage every entry from the env prefix.

    Returns an int exit code: 0 on success (a repo with no `[stage]` map stages
    nothing and returns 0), with the domain's :class:`~shipit.staging.StagingError`
    and malformed-config :class:`~shipit.config.ConfigError` mapped to ``error: …``
    + exit 1 by the :func:`~._errors.cli_errors` shell.
    """
    root = Path(path or ".").resolve()
    entries = config.load_stage(load_config(root))
    staged = staging.stage(root, entries, feature=feature)
    emit(staged, format_staged)
    return 0


def format_staged(staged: list[staging.StagedFile]) -> str:
    """The per-copy report: one line per staged file/dir, off the result.

    An empty stage (no `[stage]` map, or a map that copied nothing) says so
    plainly rather than printing a bare header that pretends work happened.
    """
    if not staged:
        return "stage: nothing to stage — no [stage] map declared."
    lines = [f"stage: copied {len(staged)} item(s) from the env prefix:"]
    for item in staged:
        kind = "dir " if item.is_dir else "file"
        exec_note = " (executable)" if item.executable and not item.is_dir else ""
        lines.append(f"  {kind} {item.source} -> {item.dest}{exec_note}")
    return "\n".join(lines)
