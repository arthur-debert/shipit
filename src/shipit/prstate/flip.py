"""The guarded draft→ready flip: it refuses unless the engine calls the PR
READY, raising :class:`NotReady` with the real status rather than no-oping
silently. Both `pr ready` and `pr next` flip through this one guard."""

from __future__ import annotations

import logging

from .. import events, gh
from ..pr import PrId
from .fetch import bind_pr_identity, gather
from .reviewers_config import load_roster
from .roster import Roster
from .state import TaskState, TaskStatus, evaluate

logger = logging.getLogger("shipit.prstate")


class NotReady(RuntimeError):
    """The guarded flip was asked to flip a PR the engine does not call READY."""

    def __init__(self, status: TaskStatus) -> None:
        self.status = status
        super().__init__(
            f"PR #{status.pr} is not Ready (state: {status.state.value}) — "
            f"{status.next_action}"
        )


def guarded_flip(
    pr: PrId,
    roster: Roster | None = None,
    *,
    flip=gh.pr_ready,
    evaluate_status=None,
    sightings: events.Sightings | None = None,
) -> TaskStatus:
    """Re-evaluate the live PR and flip draft→ready only if it is READY; a
    fresh snapshot is always gathered, since the PR may have moved."""
    if evaluate_status is None:
        status = evaluate(
            gather(
                pr,
                roster if roster is not None else load_roster(),
                sightings=sightings,
            )
        )
    else:
        status = evaluate_status(pr)
    if status.state is not TaskState.READY:
        logger.warning(
            "pr#%s flip refused — not Ready (state=%s)",
            pr.number,
            status.state.value,
            extra={"pr": pr.number},
        )
        raise NotReady(status)
    flip(pr)
    # Emitted once per actual flip, never on a refusal.
    events.emit(
        logger,
        "pr.ready",
        "pr#%s flipped draft→ready — %s",
        pr.number,
        status.next_action,
        extra={"pr": pr.number},
    )
    return status


def undo_flip(pr: PrId, *, flip=gh.pr_ready, bind_identity=bind_pr_identity) -> None:
    """Revert ready→draft — always allowed, never guarded. It performs no
    gather, so the ``epic``/``ws`` binding comes from ``bind_identity``."""
    bind_identity(pr)
    flip(pr, undo=True)
    events.emit(
        logger,
        "pr.unready",
        "pr#%s reverted ready→draft — back in the agent's court",
        pr.number,
        extra={"pr": pr.number},
    )
