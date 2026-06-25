"""`shipit pr status` — read-only PR lifecycle snapshot.

Reports where a PR stands (one of the ``TaskState`` values) and the single next
action, in text by default or JSON with ``--json``. Resolves the PR for the
current branch when no number is given. Read-only: it never edits the PR — it
*reports* READY; a later verb does the flip.

The read path is a thin shell over the engine: resolve the PR ->
:func:`prstate.fetch.gather` -> :func:`prstate.state.evaluate` (with the
config-resolved required reviewer set) -> render. All the lifecycle logic lives
in the engine; this verb only resolves, renders, and maps errors to exit codes.

``no_pr`` is a NORMAL state (exit 0): the shared resolver returns ``None`` when
the branch genuinely has no PR, which renders as ``no_pr``. A real `gh`/auth
failure — at resolution OR at ``gather`` — is NOT collapsed into ``no_pr``; the
PRD requires it to surface as a clean stderr message + non-zero exit so
automation can detect it. The resolver keeps the two cases distinct, so this
verb maps ``None`` -> ``no_pr`` and ``GhError`` -> fatal without guessing.
"""

from __future__ import annotations

import json
import sys

import click

from ...prstate import ghapi
from ...prstate.fetch import gather
from ...prstate.reviewers import required_reviewers
from ...prstate.state import TaskState, TaskStatus, evaluate, no_pr
from ._resolve import resolve_pr


@click.command(name="status")
@click.argument("pr", required=False, type=int)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the status as a JSON object."
)
def cmd(pr: int | None, as_json: bool) -> None:
    """Report where PR stands in the review loop + the single next action.

    PR is the number; omitted, it resolves the current branch's PR. Read-only —
    prints the lifecycle state (reviews pending / addressing / reviewed /
    validating / ready / blocked) and never mutates the PR. ``no PR`` is a normal
    state, not an error.
    """
    raise SystemExit(run(pr, as_json=as_json))


def run(pr: int | None = None, *, as_json: bool = False) -> int:
    """Resolve -> gather -> evaluate -> render. Returns an int exit code.

    Returns 0 on a printed status (including ``no_pr``); non-zero on a real
    gh/auth failure, whether resolving the branch's PR or reading a known one.
    """
    try:
        resolved = resolve_pr(pr)
        if resolved is None:
            _emit(no_pr(), as_json=as_json)
            return 0
        ctx = gather(resolved)
    except ghapi.GhError as exc:
        # A genuine gh/auth failure (NOT "no PR for branch" — the resolver
        # returns None for that). The PRD requires this to be visible: clean
        # stderr + non-zero exit, never a silent no_pr.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    status = evaluate(ctx, required=required_reviewers())
    _emit(status, as_json=as_json)
    return 0


def _emit(status: TaskStatus, *, as_json: bool = False) -> None:
    """Render a TaskStatus: JSON object with --json, else a readable block."""
    if as_json:
        print(json.dumps(status.to_dict(), indent=2))
        return
    if status.state is TaskState.NO_PR:
        print("state:  no_pr")
        print(f"next:   {status.next_action}")
        return
    reviewers = "  ".join(f"{name}={lc}" for name, lc in status.reviewers.items())
    print(f"PR #{status.pr}")
    print(f"state:      {status.state.value}")
    print(f"next:       {status.next_action}")
    print(f"reviewers:  {reviewers}")
    print(f"threads:    {status.open_threads} open")
    print(f"checks:     {status.checks.value}")
    print(f"mergeable:  {status.mergeable}")
    print(f"cycles:     {status.cycles}")
    if status.breaker:
        print(f"breaker:    {status.breaker}")
