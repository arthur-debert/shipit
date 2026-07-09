"""The blocking wait loop — the engine core behind `shipit pr wait` (ADR-0034).

shipit's ONE piece of code that blocks: :func:`wait_for` re-polls an injected
status source (the verb composes ``gather`` + ``evaluate`` — the SAME evaluator
`pr status` reads) at a fixed tool-owned cadence until the awaited condition
arrives or the hard deadline expires. `pr status` and `pr next` stay pure
single-shot reads; before this verb the loop lived in whichever agent drove the
cycle, whose sleep economics (prompt-cache windows) cost minutes of dead time
per landed review — ADR-0034 records the measurement and the reversal.

The two awaitable conditions (:class:`Until`):

  * ``reviews-in`` — the latest round's reviews have all LANDED: no required
    reviewer still holds the PR (:func:`~.state.reviews_in`, the engine's own
    holding vocabulary). The moment an addressing agent becomes dispatchable.
  * ``ready`` — the engine reports READY (`TaskState.READY`).

A ``ready`` wait carries a deadlock guard (#583): ``addressing`` is
CALLER-ACTIONABLE — the process parked behind the wait is the one actor whose
action (dispatching the round's addressing) unblocks the awaited state, so
READY can never arrive while the wait blocks through it. :func:`actionable`
recognizes that shape and :func:`wait_for` returns promptly with the distinct
ACTIONABLE outcome instead of polling to the deadline. ``reviews-in`` needs no
guard: an ``addressing`` snapshot SATISFIES it (the round landed), so that
wait fires rather than stops.

The cadence is CONFIG, not judgment: :data:`POLL_INTERVAL_SECONDS` (60s) is the
documented shipped default, overridable ONLY via the `[reviewers]` table-level
``poll_interval`` key (`Roster.poll_interval`) — never a per-call flag, so the
interval is versioned tooling policy testable in one place.

The escape hatch is a HARD deadline: ``timeout_seconds`` is required semantics
(the verb always passes one — a waiter that can hang forever merely relocates
the hang it was built to remove). On expiry :func:`wait_for` returns promptly
with the TIMED_OUT outcome carrying the last observed status, so the caller can
report the engine's next-action line — what is still outstanding — and exit with
the distinct code.

Observability (ADR-0032): the loop emits ``wait.started`` at entry, one
``wait.state_changed`` per poll tick where the observed state moved (plus the
caller's ``on_change`` line — the tail-able progress), and exactly one terminal
``wait.fired`` / ``wait.actionable`` / ``wait.timed_out``. Re-reading UNCHANGED
state is not a milestone and leaves no record. The per-evaluation observational events (a
landed review, a fired breaker) ride the caller's invocation-wide
:class:`~shipit.events.Sightings` through ``gather`` exactly as `pr next`'s do.

The loop is a pure composition over injected seams — the status source, the
clock, the sleep — so tests script a status sequence and a fake clock; no
network, no real time. This waiter is distinct from the attach-verification
poll in :mod:`.request` (which verifies request placement, not round arrival).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .. import events
from .state import TaskState, TaskStatus, reviews_in

#: The engine's logger (shared name across :mod:`shipit.prstate`): the wait's
#: lifecycle records attribute to the state engine that witnessed them.
logger = logging.getLogger("shipit.prstate")

#: The shipped default poll cadence, in seconds (ADR-0034): the fixed
#: tool-owned interval `pr wait` re-polls the evaluator at. Overridden ONLY by
#: the `[reviewers]` table-level ``poll_interval`` config key
#: (:attr:`~.roster.Roster.poll_interval`); there is deliberately no per-call
#: flag — cadence is versioned tooling policy, not per-agent judgment.
POLL_INTERVAL_SECONDS = 60


class Until(StrEnum):
    """The awaitable conditions — the `--until` vocabulary, one value each."""

    REVIEWS_IN = "reviews-in"
    READY = "ready"


class Outcome(StrEnum):
    """How a wait ended: the condition arrived, the wait stopped on a state
    only its caller can clear (#583), or the hard deadline expired."""

    FIRED = "fired"
    ACTIONABLE = "actionable"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True)
class WaitResult:
    """The wait's typed result: how it ended + the last observed status.

    ``outcome`` is the terminal state (:class:`Outcome`); ``status`` the final
    snapshot (on TIMED_OUT, what the wait is still waiting on; on ACTIONABLE,
    the caller-actionable state it stopped on — either way the caller's state
    report); ``ticks`` how many polls ran; ``waited_seconds`` the elapsed
    wall clock. ``to_dict`` is the ``--json`` surface, serialized by the shared
    render seam.
    """

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
    """Whether the awaited condition holds for this snapshot — a pure predicate.

    ``reviews-in`` reads the per-reviewer funnel through the engine's own
    holding vocabulary (:func:`~.state.reviews_in`); ``ready`` is the engine's
    READY verdict. Both consume the STRUCTURED status, never `next_action`
    prose (the #24.1 discipline).
    """
    if until is Until.REVIEWS_IN:
        return reviews_in(status, required_names)
    return status.state is TaskState.READY


def actionable(until: Until, status: TaskStatus) -> bool:
    """Whether this snapshot deadlocks the wait — a pure predicate (#583).

    True iff the awaited state can NEVER arrive without the waiting caller
    acting first: a ``ready`` wait observing ``addressing``. The round's
    findings sit unaddressed, addressing them is the parked caller's own next
    move, and READY is unreachable until it happens — polling on is a
    guaranteed dead wait to the deadline.

    Deliberately ONLY ``addressing``: every other state either progresses
    without the caller (reviews landing, CI finishing) or is the timeout's job
    (a wedged BLOCKED — red CI or a dirty merge state can also involve the
    caller, but not EXCLUSIVELY and not always, so the hard deadline stays the
    arbiter there). A ``reviews-in`` wait is never deadlocked: an
    ``addressing`` snapshot satisfies it (the round landed), so
    :func:`satisfied` fires first and this predicate is moot.
    """
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
    """Block until ``until`` holds or ``timeout_seconds`` expires — the ONE loop.

    ``poll`` is the injected status source (the verb composes the live
    ``gather`` + ``evaluate``; a test scripts a sequence). The loop polls once
    immediately — an already-satisfied condition returns without sleeping —
    then re-polls every ``poll_seconds``, clamping the final nap to the
    remaining deadline so a timeout is reported promptly, never up to one full
    interval late. The deadline is HARD: it is checked after every poll, and
    the condition is checked first so a wait that fires exactly at the deadline
    still counts as FIRED. A snapshot that deadlocks the wait
    (:func:`actionable` — a ``ready`` wait observing ``addressing``, #583)
    returns promptly with the ACTIONABLE outcome on the tick that observes it:
    the awaited state cannot arrive until the caller acts, so polling on would
    be a dead wait to the deadline.

    ``on_change`` is called (after the ``wait.state_changed`` event) on every
    tick where the observed state moved — including the first observation,
    which is always a change from nothing — so a supervising human/agent can
    tail progress. ``sleep`` / ``monotonic`` are the clock seam (a direct
    caller — a test — injects fakes; the CLI takes the defaults).

    Never raises for a slow PR: a timeout is an ADVISORY outcome for the
    supervisor (the distinct exit code is the verb's rendering of it), not a
    failure of the PR. A real gh/auth failure inside ``poll`` propagates —
    that IS an error, and it must not be silently retried until the deadline.
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
        # "State changed" is the observable pair a tailer cares about: the
        # lifecycle state plus the engine's next-action line (which moves when
        # a reviewer lands even while the lifecycle state stays put). The
        # first observation is a change from nothing, deliberately emitted.
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
            # The deadlock guard (#583): the observed state is one only THIS
            # caller can clear — waiting on is a guaranteed dead wait, so
            # return promptly with the distinct outcome and the state report.
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
        # Clamp the nap to the remaining deadline so expiry is prompt — the
        # loop never sleeps past the hard deadline it is about to enforce.
        sleep(min(poll_seconds, deadline - now))
