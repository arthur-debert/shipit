"""The next-action dispatcher: a pure decision from a `TaskStatus` to the
one act to take, with execution injected as the :class:`Acts` boundary."""

from __future__ import annotations

import logging
from typing import Protocol

from .. import events
from ..pr import PrId
from .errors import PrStateError
from .flip import guarded_flip
from .request import request_reviewers
from .reviewers import required_adapters
from .roster import Roster
from .state import TaskState, TaskStatus

logger = logging.getLogger("shipit.prstate")


class Acts(Protocol):
    """The injected execution boundary — the three things `pr next` can do."""

    def report(self, status: TaskStatus) -> str: ...

    def request_review(self, status: TaskStatus) -> str: ...

    def flip_ready(self, status: TaskStatus) -> str: ...


def dispatch(status: TaskStatus, acts: Acts) -> str:
    """Route a `TaskStatus` to the one act and perform it via `acts`."""
    state = status.state

    if state is TaskState.REVIEWS_PENDING and status.to_request:
        line = acts.request_review(status)
    elif state is TaskState.READY:
        line = acts.flip_ready(status)
    else:
        line = acts.report(status)
    if status.pr is not None:
        logger.info(
            "pr#%s next action taken — %s",
            status.pr,
            line,
            extra={"pr": status.pr},
        )
    return line


class NextActs:
    """The concrete :class:`Acts` boundary the `pr next` verb injects."""

    def __init__(
        self,
        pr: PrId,
        roster: Roster | None = None,
        sightings: events.Sightings | None = None,
    ) -> None:
        self._pr = pr
        self._roster = roster if roster is not None else Roster()
        self._sightings = sightings

    def report(self, status: TaskStatus) -> str:
        return f"no action taken — {status.next_action}"

    def request_review(self, status: TaskStatus) -> str:
        """Request the reviewers the engine put in `to_request`; the set is
        consumed, never re-derived from the lifecycle map, which cannot tell
        an in-flight local-agent reviewer from a never-requested one."""
        by_name = {r.name: r for r in required_adapters(self._roster)}
        selected = [by_name[name] for name in status.to_request if name in by_name]
        # A local reviewer can fail synchronously before detach, so try the
        # no-edge locals before any remote edge is placed.
        selected.sort(key=lambda adapter: adapter.has_requested_edge)
        if not selected:
            return f"no requestable reviewer to (re-)request — {status.next_action}"
        result = request_reviewers(self._pr, selected, self._roster, force=True)
        if not result.ok:
            raise PrStateError(
                "review request dropped by GitHub (no review_requested edge "
                f"attached): {', '.join(result.dropped)} — re-run `pr next`"
            )
        acted = result.verified + result.in_flight
        if not acted:
            return f"no requestable reviewer to (re-)request — {status.next_action}"
        return f"requested review(s): {', '.join(acted)}"

    def flip_ready(self, status: TaskStatus) -> str:
        # The guarded re-check means a stale status can never flip.
        guarded_flip(self._pr, self._roster, sightings=self._sightings)
        return "flipped draft→ready — ready for human validation"
