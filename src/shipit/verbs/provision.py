"""`shipit provision` — the tombstone for a retired verb: it always refuses.

See docs/adr/0066-provision-lexd-retires-onto-the-channel.md.
"""

from __future__ import annotations

import click

from ..install.errors import InstallError
from ._errors import cli_errors

RETIRED_MESSAGE = (
    "`shipit provision` is RETIRED (ADR-0066) — it no longer exists as a step. "
    "lexd rides the public Artifact channel as an ordinary conda dependency, "
    "delivered by the managed [feature.shipit-lexd] block and already on PATH in "
    "the lint environment. Remove this call from your pixi task (drop the "
    "`shipit provision lexd &&` prefix, or the whole `provision-lexd` task if "
    "that is all it does) and re-run `shipit install`"
)


@click.command(
    name="provision",
    hidden=True,
    context_settings={"ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def cmd(args: tuple[str, ...]) -> None:
    """Retired: lexd rides the Artifact channel — remove this call."""
    raise SystemExit(run(*args))


@cli_errors
def run(*args: str) -> int:
    """Always refuse, printing the remedy on stderr; returns 1."""
    raise InstallError(RETIRED_MESSAGE)
