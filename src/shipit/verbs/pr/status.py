"""`shipit pr status` — read-only PR lifecycle snapshot, in text or ``--json``."""

from __future__ import annotations

import click

from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.state import evaluate, no_pr
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import json_option, pr_number_argument
from .._render import emit
from ._format import format_status


@click.command(name="status")
@pr_number_argument
@json_option
def cmd(pr: int | None, as_json: bool) -> None:
    """Report where PR stands in the review loop + the single next action."""
    raise SystemExit(run(pr, as_json=as_json))


@cli_errors
def run(
    pr: int | None = None, *, as_json: bool = False, repo: Repo | None = None
) -> int:
    """Resolve → gather → evaluate → render; ``no_pr`` is a normal state (exit 0), while a real gh/auth failure reaches the error shell."""
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        emit(no_pr(), format_status, as_json=as_json)
        return 0
    ctx = gather(target, load_roster(), emit_events=False)
    status = evaluate(ctx)
    emit(status, format_status, as_json=as_json)
    return 0
