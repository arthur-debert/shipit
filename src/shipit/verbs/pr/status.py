"""`shipit pr status` — read-only PR lifecycle snapshot.

Reports where a PR stands (one of the ``TaskState`` values) and the single next
action, in text by default or JSON with ``--json``. Resolves the PR for the
current branch when no number is given. Read-only: it never edits the PR — it
*reports* READY; a later verb does the flip.

The read path is a thin shell over the engine: resolve the PR ->
:func:`prstate.fetch.gather` -> :func:`prstate.state.evaluate` (with the
config-resolved required reviewer set) -> render. All the lifecycle logic lives
in the engine; this verb only resolves, renders, and maps errors to exit codes.

``no_pr`` is a NORMAL state (exit 0), not an error — so the PR resolution here is
deliberately lenient: a `gh`/auth failure while *looking up the current branch's
PR* reads as "no PR for this branch" rather than crashing a status line. A
`gh`/auth failure during the actual ``gather`` (we have a PR number but can't
read it) is a real error: clean stderr message + non-zero exit.
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

    Returns 0 on a printed status (including ``no_pr``), non-zero on a gh/auth
    failure while reading a known PR.
    """
    resolved = _resolve_lenient(pr)
    if resolved is None:
        _emit(no_pr(), as_json=as_json)
        return 0
    try:
        ctx = gather(resolved)
    except ghapi.GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    status = evaluate(ctx, required=required_reviewers())
    _emit(status, as_json=as_json)
    return 0


def _resolve_lenient(pr: int | None) -> int | None:
    """Resolve the PR for a read-only status line, treating any gh failure during
    the current-branch lookup as "no PR".

    ``pr status`` must not error out of a status line just because the branch has
    no PR (``gh pr view`` exits non-zero then) or gh is momentarily unhappy —
    ``no_pr`` is the answer. An EXPLICIT pr number is returned as-is (no lookup,
    so nothing to swallow); the lenient catch only covers the branch resolution.
    """
    try:
        return resolve_pr(pr)
    except ghapi.GhError:
        return None


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
