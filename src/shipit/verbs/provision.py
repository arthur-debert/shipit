"""`shipit provision` — ADR-0030 glue + renderer over the provision domain.

The verb is click glue and a renderer: the pin, platform resolution, fetch and
install logic live in :mod:`shipit.provision` (one module per tool) and return
a typed report; this module renders it through the shared
:func:`~._render.emit` seam (``--json`` serializes ``to_dict()``), with runtime
failures — a refused platform, a checksum mismatch, a failed fetch Exec —
mapped by the one :func:`~._errors.cli_errors` shell (``error: …`` + exit 1).

`provision lexd` is the consumer delivery path for the lex Lang's gate tool
(docs/legacy-prd/adoption.md): the managed ``provision-lexd`` pixi task invokes it,
so a consumer repo carries no provisioning script. Idempotent — re-running at
the same pin is a no-op.
"""

from __future__ import annotations

import click

from ..provision import lexd
from ._errors import cli_errors
from ._params import json_option
from ._render import emit


@click.group(name="provision")
def provision() -> None:
    """Provision pinned external tools into the active pixi env.

    Tools on the required-check path that are not on conda-forge (so they
    cannot ride pixi.lock) are pinned in the shipit binary and fetched from
    their release by these subcommands — idempotent, checksum-verified, and
    installed into the invoking env's prefix.
    """


@provision.command(name="lexd")
@json_option
def lexd_cmd(as_json: bool) -> None:
    """Put the pinned lexd on PATH inside the active pixi env.

    A no-op when the pinned lexd is already installed. Runs inside a pixi env
    (`pixi run …`); the invoking env's prefix receives the binary.
    """
    raise SystemExit(run_lexd(as_json=as_json))


@cli_errors
def run_lexd(*, as_json: bool = False) -> int:
    """Provision → render. Returns an exit code (refusals map to 1 via the shell)."""
    report = lexd.provision()
    emit(report, format_lexd, as_json=as_json)
    return 0


def format_lexd(report: lexd.LexdReport) -> str:
    """The pure text renderer — one line, off the typed report."""
    if report.action == lexd.ACTION_NOOP:
        return f"provision lexd: {report.pin} already provisioned ({report.dest})"
    return f"provision lexd: installed {report.pin} ({report.triple}) -> {report.dest}"
