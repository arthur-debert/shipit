"""`shipit opportunities` — capture Opportunities into the configured store."""

from __future__ import annotations

from pathlib import Path

import click

from .. import config
from ..opportunities import load_store_config, make_capture, write_to_store
from ._context import current_root_context
from ._errors import cli_errors


@click.group(name="opportunities")
def opportunities() -> None:
    """Capture and manage Opportunities."""


@click.command(name="create")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to .shipit.toml (default: the repo root's).",
)
@click.option("--source", required=True, help="Where the observation came from.")
@click.option("--tag", "tags", multiple=True, required=True, help="Opportunity tag.")
@click.option("--observation", required=True, help="Observed improvement opportunity.")
@click.option(
    "--evidence", required=True, help="Concrete evidence for the observation."
)
@click.option("--next-step", required=True, help="Suggested next step.")
def create_cmd(
    config_path: str | None,
    source: str,
    tags: tuple[str, ...],
    observation: str,
    evidence: str,
    next_step: str,
) -> None:
    """Capture an inbox Opportunity in the configured store."""

    raise SystemExit(
        run_create(
            config_path=config_path,
            source=source,
            tags=tags,
            observation=observation,
            evidence=evidence,
            next_step=next_step,
        )
    )


@cli_errors
def run_create(
    *,
    config_path: str | None = None,
    source: str,
    tags: tuple[str, ...],
    observation: str,
    evidence: str,
    next_step: str,
) -> int:
    """Read config, validate the capture, write it to the store, and render a summary."""

    ctx = current_root_context()
    wd = ctx.require_working_dir()
    cfg_path = Path(config_path or Path(wd.path) / config.CONFIG_NAME)
    store = load_store_config(config.load(cfg_path))
    capture = make_capture(
        repo=wd.repo,
        source=source,
        tags=tags,
        observation=observation,
        evidence=evidence,
        suggested_next_step=next_step,
    )
    result = write_to_store(store, capture)
    click.echo(f"created Opportunity: {result.store_repo}/{result.path}")
    return 0


opportunities.add_command(create_cmd)
