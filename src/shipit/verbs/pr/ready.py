"""`shipit pr ready` — the guarded draft→ready flip (and `--undo`), as glue."""

from __future__ import annotations

import click

from ...gh import resolve_pr
from ...identity import Repo
from ...pr import PrId
from ...prstate.errors import PrStateError
from ...prstate.flip import (  # noqa: F401  (NotReady re-exported for callers/tests)
    NotReady,
    guarded_flip,
    undo_flip,
)
from ...prstate.state import TaskStatus
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import pr_number_argument
from .._render import emit


@click.command(name="ready")
@pr_number_argument
@click.option(
    "--undo",
    is_flag=True,
    help="Revert ready→draft (always allowed; not held by Ready).",
)
def cmd(pr: int | None, undo: bool) -> None:
    """Flip a PR draft→ready — guarded: refuses unless the engine says Ready."""
    raise SystemExit(run(pr, undo=undo))


@cli_errors
def run(pr: int | None = None, *, undo: bool = False, repo: Repo | None = None) -> int:
    """Resolve → (undo ? revert : guarded flip) → render; a branch with no PR is a refusal, unlike the read-only `pr status`."""
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError("no PR for this branch — nothing to flip")
    if undo:
        undo_flip(target)
        emit(target, format_undone)
        return 0
    status = guarded_flip(target)
    emit(status, format_flipped)
    return 0


def format_flipped(status: TaskStatus) -> str:
    """The pure text renderer for a performed flip."""
    return f"PR #{status.pr}: flipped draft→ready — ready for human validation"


def format_undone(target: PrId) -> str:
    """The pure text renderer for a performed ``--undo``."""
    return f"PR #{target.number}: reverted ready→draft"
