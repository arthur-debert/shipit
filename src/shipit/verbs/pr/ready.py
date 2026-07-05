"""`shipit pr ready` — the guarded draft→ready flip (and `--undo`), as glue.

The flip is the one signal that says "done iterating — a human can validate and
merge", so it is GUARDED: it refuses unless the engine says the PR is READY (all
three Ready pillars — Reviewed + CI green + authoritative-mergeable). The guard
itself is the engine's :func:`shipit.prstate.flip.guarded_flip` (CLI01-WS03
promoted it out of this verb) — the SAME guard `pr next`'s ready act flips
through — and its refusal is the :class:`~shipit.prstate.flip.NotReady` domain
exception, mapped to a clean ``error: …`` + exit 1 by the one
:func:`~.._errors.cli_errors` shell. `--undo` reverts ready→draft and is ALWAYS
allowed — sending a PR back to draft when a human asks for changes is never
held; it goes through the engine's :func:`~shipit.prstate.flip.undo_flip`
(LOG04-WS02), which performs the flag flip and emits the ``pr.unready``
dev-cycle event with the head branch's ``epic``/``ws`` bound (ADR-0032).

This module is ADR-0030 glue + renderers only: parse the shared PR-target
primitive, resolve the typed target, call the domain flip, render the pure
``format_*`` line through the shared emit.
"""

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
    """Flip a PR draft→ready — guarded: refuses unless the engine says Ready.

    PR is the number; omitted, it resolves the current branch's PR. The flip
    happens only when all three Ready pillars hold (reviewed + CI green +
    mergeable); otherwise it refuses with the real state and a non-zero exit.
    ``--undo`` sends a ready PR back to draft and is always permitted.
    """
    raise SystemExit(run(pr, undo=undo))


@cli_errors
def run(pr: int | None = None, *, undo: bool = False, repo: Repo | None = None) -> int:
    """Resolve → (undo ? revert : guarded flip) → render. Returns an exit code.

    ``repo`` is the identity half of the PR target: omitted (the CLI path), the
    root context's ambient repo — resolved once per invocation (ADR-0030); a
    direct caller (a test) injects it as a value.

    0 on a performed flip/undo; non-zero on a refusal (the engine's ``NotReady``
    reaching the shell) or a real gh/auth failure. A branch with no PR is a
    clean non-zero error here (unlike the read-only `pr status`, a mutating
    verb has nothing to flip) — raised as the domain refusal, rendered by the
    shell.
    """
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError("no PR for this branch — nothing to flip")
    if undo:
        # Always allowed: revert ready→draft — through the engine's undo seam,
        # which flips the flag and emits the `pr.unready` event (ADR-0032).
        undo_flip(target)
        emit(target, format_undone)
        return 0
    status = guarded_flip(target)
    emit(status, format_flipped)
    return 0


def format_flipped(status: TaskStatus) -> str:
    """The pure text renderer for a performed flip (the render seam owns stdout)."""
    return f"PR #{status.pr}: flipped draft→ready — {status.next_action}"


def format_undone(target: PrId) -> str:
    """The pure text renderer for a performed ``--undo``."""
    return f"PR #{target.number}: reverted ready→draft"
