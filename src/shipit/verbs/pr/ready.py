"""`shipit pr ready` — the guarded draft→ready flip (and `--undo`).

The flip is the one signal that says "done iterating — a human can validate and
merge", so it is GUARDED: it refuses unless the engine says the PR is READY (all
three Ready pillars — Reviewed + CI green + authoritative-mergeable). The refusal
is a clean message + non-zero exit, never a silent no-op. `--undo` reverts
ready→draft and is ALWAYS allowed — sending a PR back to draft when a human asks
for changes is never held.

The guarded re-check lives in :func:`guarded_flip` so both this verb and
`pr next`'s ready act flip through the SAME guard: re-evaluate the live snapshot,
flip only on READY. Re-checking at flip time (not trusting a status computed
moments earlier) is what makes the flip safe against a state that moved.
"""

from __future__ import annotations

import sys

import click

from ...prstate import ghapi
from ...prstate.fetch import gather
from ...prstate.reviewers import required_reviewers
from ...prstate.state import TaskState, TaskStatus, evaluate
from ._resolve import resolve_pr


class NotReady(RuntimeError):
    """The guarded flip was asked to flip a PR the engine does not call READY."""

    def __init__(self, status: TaskStatus) -> None:
        self.status = status
        super().__init__(
            f"PR #{status.pr} is not Ready (state: {status.state.value}) — "
            f"{status.next_action}"
        )


def guarded_flip(pr: int, *, flip=ghapi.pr_ready, evaluate_status=None) -> TaskStatus:
    """Re-evaluate the live PR and flip draft→ready ONLY if it is READY.

    The shared guarded re-check behind both `pr ready` and `pr next`'s ready act.
    Gathers a FRESH snapshot and re-runs the engine (never trusting a status
    computed earlier — the PR may have moved); on READY it performs the flip and
    returns the READY status, otherwise it raises :class:`NotReady` carrying the
    real status so the caller can report why it refused.

    `flip` / `evaluate_status` are injected for testing: `flip` is the
    draft→ready boundary (default :func:`ghapi.pr_ready`); `evaluate_status`
    yields the fresh `TaskStatus` (default: `gather` + `evaluate` with the
    config-resolved required set). A test injects both to drive the guard without
    a network.
    """
    if evaluate_status is None:
        status = evaluate(gather(pr), required=required_reviewers())
    else:
        status = evaluate_status(pr)
    if status.state is not TaskState.READY:
        raise NotReady(status)
    flip(pr)
    return status


@click.command(name="ready")
@click.argument("pr", required=False, type=int)
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


def run(pr: int | None = None, *, undo: bool = False) -> int:
    """Resolve → (undo ? revert : guarded flip). Returns an int exit code.

    0 on a performed flip/undo; non-zero on a refusal (not Ready) or a real
    gh/auth failure. A branch with no PR is a clean non-zero error here (unlike
    the read-only `pr status`, a mutating verb has nothing to flip).
    """
    try:
        resolved = resolve_pr(pr)
        if resolved is None:
            print("error: no PR for this branch — nothing to flip", file=sys.stderr)
            return 1
        if undo:
            # Always allowed: revert ready→draft. No readiness hold.
            ghapi.pr_ready(resolved, undo=True)
            print(f"PR #{resolved}: reverted ready→draft")
            return 0
        status = guarded_flip(resolved)
    except NotReady as exc:
        print(f"refusing to flip: {exc}", file=sys.stderr)
        return 1
    except ghapi.GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"PR #{status.pr}: flipped draft→ready — {status.next_action}")
    return 0
