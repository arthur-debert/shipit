"""The blocking wait loop behind `shipit pr wait`: re-poll an injected
status source until the awaited condition arrives or the deadline expires.
See docs/adr/0034-pr-wait-blocking-verb.md."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .. import events
from .state import TaskState, TaskStatus, reviews_in

logger = logging.getLogger("shipit.prstate")

#: Overridden only by the `[reviewers]` table-level ``poll_interval`` key.
POLL_INTERVAL_SECONDS = 60


class Until(StrEnum):
    REVIEWS_IN = "reviews-in"
    READY = "ready"


class Outcome(StrEnum):
    """How a wait ended: fired, caller-actionable, or expired."""

    FIRED = "fired"
    ACTIONABLE = "actionable"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True)
class WaitResult:
    outcome: Outcome
    until: Until
    status: TaskStatus
    ticks: int
    waited_seconds: float

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "until": self.until.value,
            "ticks": self.ticks,
            "waited_seconds": round(self.waited_seconds, 3),
            "status": self.status.to_dict(),
        }


def satisfied(until: Until, status: TaskStatus, required_names: Sequence[str]) -> bool:
    """Whether the awaited condition holds for this snapshot — pure."""
    if until is Until.REVIEWS_IN:
        return reviews_in(status, required_names)
    return status.state is TaskState.READY


def actionable(until: Until, status: TaskStatus) -> bool:
    """Whether this snapshot deadlocks the wait: only a ``ready`` wait
    observing ``addressing``, which only the parked caller can clear."""
    return until is Until.READY and status.state is TaskState.ADDRESSING


def wait_for(
    poll: Callable[[], TaskStatus],
    *,
    pr: int,
    until: Until,
    required_names: Sequence[str],
    timeout_seconds: float,
    poll_seconds: float,
    on_change: Callable[[TaskStatus], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> WaitResult:
    """Block until ``until`` holds or ``timeout_seconds`` expires.

    Polls once immediately, then every ``poll_seconds``, clamping the final
    nap to the deadline. The condition is checked first, so a wait firing
    exactly at the deadline still counts as FIRED. A timeout is an advisory
    outcome, not a raise; a failure inside ``poll`` propagates.
    """
    start = monotonic()
    deadline = start + timeout_seconds
    events.emit(
        logger,
        "wait.started",
        "pr#%s wait started — until %s (poll %ss, timeout %ss)",
        pr,
        until.value,
        poll_seconds,
        timeout_seconds,
        extra={
            "pr": pr,
            "until": until.value,
            "poll_seconds": poll_seconds,
            "timeout_seconds": timeout_seconds,
        },
    )
    ticks = 0
    last_seen: tuple[str, str] | None = None
    while True:
        status = poll()
        ticks += 1
        # The next-action line moves even while the state stays put.
        seen = (status.state.value, status.next_action)
        if seen != last_seen:
            last_seen = seen
            events.emit(
                logger,
                "wait.state_changed",
                "pr#%s wait observed %s — %s",
                pr,
                status.state.value,
                status.next_action,
                extra={"pr": pr, "until": until.value, "state": status.state.value},
            )
            if on_change is not None:
                on_change(status)
        if satisfied(until, status, required_names):
            waited = monotonic() - start
            events.emit(
                logger,
                "wait.fired",
                "pr#%s wait fired — %s after %d poll(s) (%.0fs)",
                pr,
                until.value,
                ticks,
                waited,
                extra={
                    "pr": pr,
                    "until": until.value,
                    "ticks": ticks,
                    "waited_seconds": round(waited, 3),
                    "state": status.state.value,
                },
            )
            return WaitResult(Outcome.FIRED, until, status, ticks, waited)
        if actionable(until, status):
            waited = monotonic() - start
            events.emit(
                logger,
                "wait.actionable",
                "pr#%s wait stopped — %s is caller-actionable, %s cannot arrive "
                "unaided after %d poll(s) (%.0fs) — %s",
                pr,
                status.state.value,
                until.value,
                ticks,
                waited,
                status.next_action,
                extra={
                    "pr": pr,
                    "until": until.value,
                    "ticks": ticks,
                    "waited_seconds": round(waited, 3),
                    "state": status.state.value,
                },
            )
            return WaitResult(Outcome.ACTIONABLE, until, status, ticks, waited)
        now = monotonic()
        if now >= deadline:
            waited = now - start
            events.emit(
                logger,
                "wait.timed_out",
                "pr#%s wait timed out after %d poll(s) (%.0fs) — %s",
                pr,
                ticks,
                waited,
                status.next_action,
                extra={
                    "pr": pr,
                    "until": until.value,
                    "ticks": ticks,
                    "waited_seconds": round(waited, 3),
                    "state": status.state.value,
                },
            )
            return WaitResult(Outcome.TIMED_OUT, until, status, ticks, waited)
        # Clamped so the loop never sleeps past the deadline it enforces.
        sleep(min(poll_seconds, deadline - now))
