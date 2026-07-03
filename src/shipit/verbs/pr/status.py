"""`shipit pr status` — read-only PR lifecycle snapshot.

Reports where a PR stands (one of the ``TaskState`` values) and the single next
action, in text by default or JSON with ``--json``. Resolves the PR for the
current branch when no number is given. Read-only: it never edits the PR — it
*reports* READY; a later verb does the flip.

The FIRST verb through the ADR-0030 seam — the walking skeleton the rest of
the pr family (and CLI02's promotions) copies. The three pieces:

- **params** — click validates the explicit primitives (`PR` as an ``int``,
  the shared ``--json`` flag from :mod:`.._params`); a malformed argument is a
  click usage error (exit 2), never verb-body code. The PR *target* is the
  deliberate ADR-0030 exception: :func:`~._resolve.resolve_pr` resolves
  "explicit number vs the current branch's PR" at the verb boundary, because
  "no PR for this branch" is a runtime outcome, not a usage error.
- **domain call** — :func:`~._resolve.resolve_pr` ->
  :func:`shipit.prstate.fetch.gather` -> :func:`shipit.prstate.state.evaluate`
  (with the config-resolved required reviewer set) returns the typed
  :class:`~shipit.prstate.state.TaskStatus`; all lifecycle logic lives in the
  engine.
- **render** — the pure :func:`format_status` string function through the
  shared :func:`~.._render.emit` (JSON from ``TaskStatus.to_dict()``); the
  exit code derives from the result, with runtime failures mapped by the
  one :func:`~.._errors.cli_errors` shell (``error: …`` + exit 1) instead of
  a per-verb ``try/except``.

``no_pr`` is a NORMAL state (exit 0): the shared resolver returns ``None`` when
the branch genuinely has no PR, which renders as ``no_pr``. A real `gh`/auth
failure — at resolution OR at ``gather`` — is NOT collapsed into ``no_pr``; the
PRD requires it to surface as a clean stderr message + non-zero exit so
automation can detect it. The resolver keeps the two cases distinct, so this
verb maps ``None`` -> ``no_pr`` and lets ``ExecError`` reach the shell without
guessing.
"""

from __future__ import annotations

import click

from ...prstate.fetch import gather
from ...prstate.reviewers import required_reviewers
from ...prstate.state import TaskState, TaskStatus, evaluate, no_pr
from .._errors import cli_errors
from .._params import json_option
from .._render import emit
from ._resolve import resolve_pr


@click.command(name="status")
@click.argument("pr", required=False, type=int)
@json_option
def cmd(pr: int | None, as_json: bool) -> None:
    """Report where PR stands in the review loop + the single next action.

    PR is the number; omitted, it resolves the current branch's PR. Read-only —
    prints the lifecycle state (reviews pending / addressing / reviewed /
    validating / ready / blocked) and never mutates the PR. ``no PR`` is a normal
    state, not an error.
    """
    raise SystemExit(run(pr, as_json=as_json))


@cli_errors
def run(pr: int | None = None, *, as_json: bool = False) -> int:
    """Resolve -> gather -> evaluate -> render. Returns an int exit code.

    Returns 0 on a printed status (including ``no_pr``). A real gh/auth failure
    — whether resolving the branch's PR or reading a known one — propagates to
    the :func:`~shipit.verbs._errors.cli_errors` shell (clean ``error: …``
    stderr + exit 1, per the PRD; never a silent ``no_pr``).
    """
    resolved = resolve_pr(pr)
    if resolved is None:
        emit(no_pr(), format_status, as_json=as_json)
        return 0
    status = evaluate(gather(resolved), required=required_reviewers())
    emit(status, format_status, as_json=as_json)
    return 0


def format_status(status: TaskStatus) -> str:
    """The pure text renderer: a :class:`TaskStatus` as the readable block.

    A plain string function (no printing — the render seam owns the terminal),
    so text-output tests assert on the return value. ``no_pr`` renders the
    short two-line form; a full status renders the labelled block.
    """
    if status.state is TaskState.NO_PR:
        return f"state:  no_pr\nnext:   {status.next_action}"
    reviewers = "  ".join(f"{name}={lc}" for name, lc in status.reviewers.items())
    # A degraded PR is annotated INLINE on the state line — "ready (degraded:
    # codex-local failed)" — so the one line a reader scans already carries the
    # warning (ADR-0006: a degraded PR is never silently "fine"). The full set is
    # also listed on its own line for legibility when several reviewers degraded.
    degraded_list = ", ".join(
        f"{name} {reason}" for name, reason in status.degraded.items()
    )
    degraded_note = f" (degraded: {degraded_list})" if status.degraded else ""
    lines = [
        f"PR #{status.pr}",
        f"state:      {status.state.value}{degraded_note}",
        f"next:       {status.next_action}",
        f"reviewers:  {reviewers}",
        f"threads:    {status.open_threads} open",
        f"checks:     {status.checks.value}",
        f"mergeable:  {status.mergeable}",
        f"cycles:     {status.cycles}",
    ]
    if status.degraded:
        lines.append(f"degraded:   {degraded_list}")
    if status.breaker:
        lines.append(f"breaker:    {status.breaker}")
    return "\n".join(lines)
