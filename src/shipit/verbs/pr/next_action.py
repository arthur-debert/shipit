"""`shipit pr next` — do the ONE next action, then report. Glue + renderers."""

from __future__ import annotations

from dataclasses import dataclass

import click

from ... import events
from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.dispatch import NextActs, dispatch
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.state import TaskStatus, evaluate, no_pr
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import json_option, pr_number_argument
from .._render import emit
from ._format import format_status


@dataclass(frozen=True)
class NextResult:
    """The verb's typed result: the action taken + the resulting status."""

    action: str
    status: TaskStatus

    def to_dict(self) -> dict:
        return {"action": self.action, "status": self.status.to_dict()}


def format_next(result: NextResult) -> str:
    return f"action: {result.action}\n{format_status(result.status)}"


@click.command(name="next")
@pr_number_argument
@json_option
def cmd(pr: int | None, as_json: bool) -> None:
    """Do the single next action for PR, then report it + the resulting status."""
    raise SystemExit(run(pr, as_json=as_json))


@cli_errors
def run(
    pr: int | None = None, *, as_json: bool = False, repo: Repo | None = None
) -> int:
    """Resolve → gather → evaluate → dispatch → perform one act → render; a branch with no PR is a normal report, exit 0."""
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        status = no_pr()
        emit(
            NextResult(f"no action taken — {status.next_action}", status),
            format_next,
            as_json=as_json,
        )
        return 0
    roster = load_roster()
    sightings = events.Sightings()
    status = evaluate(gather(target, roster, sightings=sightings))
    action = dispatch(status, NextActs(target, roster, sightings))
    final = evaluate(gather(target, roster, sightings=sightings))
    emit(NextResult(action, final), format_next, as_json=as_json)
    return 0
