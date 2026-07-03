"""The guarded draft→ready flip — the hand-off SERVICE of the PR state engine.

The flip is the one signal that says "done iterating — a human can validate and
merge", so it is GUARDED: it refuses unless the engine says the PR is READY (all
three Ready pillars — Reviewed + CI green + authoritative-mergeable). The refusal
is the :class:`NotReady` domain exception carrying the real status, so a caller
can report exactly why it refused — never a silent no-op.

The guarded re-check lives here (promoted out of ``verbs/pr/`` — CLI01-WS03) so
both `pr ready` and `pr next`'s ready act flip through the SAME guard:
re-evaluate the live snapshot, flip only on READY. Re-checking at flip time (not
trusting a status computed moments earlier) is what makes the flip safe against
a state that moved. Per ADR-0029 the flip and its refusal leave their durable
log twins HERE; the adapter that performs the flag flip (:func:`shipit.gh.
pr_ready`) additionally records the boundary milestone.
"""

from __future__ import annotations

import logging

from .. import gh
from ..pr import PrId
from .fetch import gather
from .reviewers import required_reviewers
from .state import TaskState, TaskStatus, evaluate

#: The engine's logger (shared name across :mod:`shipit.prstate`): the flip is
#: THE lifecycle milestone of the whole loop, its refusal the
#: degraded-but-continuing counterpart (LOG02 spray, ADR-0029).
logger = logging.getLogger("shipit.prstate")


class NotReady(RuntimeError):
    """The guarded flip was asked to flip a PR the engine does not call READY."""

    def __init__(self, status: TaskStatus) -> None:
        self.status = status
        super().__init__(
            f"PR #{status.pr} is not Ready (state: {status.state.value}) — "
            f"{status.next_action}"
        )


def guarded_flip(pr: PrId, *, flip=gh.pr_ready, evaluate_status=None) -> TaskStatus:
    """Re-evaluate the live PR and flip draft→ready ONLY if it is READY.

    The shared guarded re-check behind both `pr ready` and `pr next`'s ready act.
    The target arrives as the typed :class:`~shipit.pr.PrId` (ADR-0030) — the
    repo rides on the identity through both the re-gather and the flip, never
    re-derived. Gathers a FRESH snapshot and re-runs the engine (never trusting
    a status computed earlier — the PR may have moved); on READY it performs the
    flip and returns the READY status, otherwise it raises :class:`NotReady`
    carrying the real status so the caller can report why it refused.

    `flip` / `evaluate_status` are injected for testing: `flip` is the
    draft→ready boundary (default :func:`shipit.gh.pr_ready`); `evaluate_status`
    yields the fresh `TaskStatus` (default: `gather` + `evaluate` with the
    config-resolved required set). A test injects both to drive the guard without
    a network.
    """
    if evaluate_status is None:
        status = evaluate(gather(pr), required=required_reviewers())
    else:
        status = evaluate_status(pr)
    if status.state is not TaskState.READY:
        # A refused flip is a degraded-but-continuing outcome: nothing mutated —
        # loud in the record (WARNING), surfaced to the caller as the domain
        # refusal, never dressed up as success.
        logger.warning(
            "pr#%s flip refused — not Ready (state=%s)",
            pr.number,
            status.state.value,
            extra={"pr": pr.number},
        )
        raise NotReady(status)
    flip(pr)
    logger.info(
        "pr#%s flipped draft→ready — %s",
        pr.number,
        status.next_action,
        extra={"pr": pr.number},
    )
    return status
