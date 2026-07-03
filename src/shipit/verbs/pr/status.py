"""`shipit pr status` — read-only PR lifecycle snapshot.

Reports where a PR stands (one of the ``TaskState`` values) and the single next
action, in text by default or JSON with ``--json``. Resolves the PR for the
current branch when no number is given. Read-only: it never edits the PR — it
*reports* READY; a later verb does the flip.

The FIRST verb through the ADR-0030 seam — the walking skeleton the rest of
the pr family (and CLI02's promotions) copies. The three pieces:

- **params** — click validates the explicit primitives (the shared PR-target
  argument and ``--json`` flag from :mod:`.._params`); a malformed argument is
  a click usage error (exit 2), never verb-body code. The PR *target* is the
  deliberate ADR-0030 exception: :func:`shipit.gh.resolve_pr` MINTS the
  ``PrId`` at the runtime boundary — repo from the root context, number
  explicit or the current branch's PR — because "no PR for this branch" is a
  runtime outcome, not a usage error.
- **domain call** — :func:`shipit.gh.resolve_pr` -> load the reviewer Roster
  once (:func:`~...prstate.reviewers_config.load_roster`) ->
  :func:`shipit.prstate.fetch.gather` (the Roster rides the snapshot) ->
  :func:`shipit.prstate.state.evaluate` (which reads the required reviewer set
  off the Roster) returns the typed :class:`~shipit.prstate.state.TaskStatus`;
  all lifecycle logic lives in the engine. The target travels as the typed
  ``PrId`` — no service re-derives the ambient repo per fetch (CLI01-WS02).
- **render** — the pure :func:`~._format.format_status` string function (shared
  with `pr next` through the render seam, never a cross-verb import) through
  the shared :func:`~.._render.emit` (JSON from ``TaskStatus.to_dict()``); the
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

from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.state import evaluate, no_pr
from .._context import current_root_context
from .._errors import cli_errors
from .._params import json_option, pr_number_argument
from .._render import emit
from ._format import format_status


@click.command(name="status")
@pr_number_argument
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
def run(
    pr: int | None = None, *, as_json: bool = False, repo: Repo | None = None
) -> int:
    """Resolve -> gather -> evaluate -> render. Returns an int exit code.

    ``repo`` is the identity half of the PR target: omitted (the CLI path), the
    root context's ambient repo — resolved once per invocation (ADR-0030); a
    direct caller (a test) injects it as a value. Outside a checkout the ONE
    uniform refusal (:class:`~.._context.NoAmbientRepoError`) maps to
    ``error: …`` + exit 1 via the shell below.

    Returns 0 on a printed status (including ``no_pr``). A real gh/auth failure
    — whether resolving the branch's PR or reading a known one — propagates to
    the :func:`~shipit.verbs._errors.cli_errors` shell (clean ``error: …``
    stderr + exit 1, per the PRD; never a silent ``no_pr``).
    """
    target = resolve_pr(
        pr, repo if repo is not None else current_root_context().require_repo()
    )
    if target is None:
        emit(no_pr(), format_status, as_json=as_json)
        return 0
    # The ONE reviewer-config read of this invocation (CLI01-WS04): the Roster
    # rides the snapshot from here — the engine and adapters read every
    # per-reviewer setting off it as a value.
    ctx = gather(target, load_roster())
    status = evaluate(ctx)
    emit(status, format_status, as_json=as_json)
    return 0
