"""Dev-cycle event registry and emit core — the ONE write path for tagged milestone records.

See docs/adr/0032-dev-cycle-events-tagged-records-witness-tiers.md.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Mapping

RECORD_KEY = "event"

EXTRA_KEY = "_event"

EVENT_NAMES = frozenset(
    {
        "session.started",
        "session.intent",
        "tree.created",
        "agent.spawned",
        "agent.phase",
        "agent.done",
        "launcher.overridden",
        "commit.created",
        "install.started",
        "install.completed",
        "install.failed",
        "ghsetup.started",
        "ghsetup.completed",
        "ghsetup.failed",
        "review.requested",
        "review.received",
        "review.degraded",
        "round.detected",
        "breaker.fired",
        "review.deduped",
        "review.calibrated",
        "finding.dispositioned",
        "review.pass.launched",
        "review.pass.settled",
        "wait.started",
        "wait.state_changed",
        "wait.fired",
        "wait.actionable",
        "wait.timed_out",
        "finding.severity_overridden",
        "release.unsigned",
        "pr.ready",
        "pr.unready",
        "sweep.started",
        "sweep.repo.done",
        "sweep.completed",
        "planning.grill.started",
        "planning.adr.written",
        "planning.spec.written",
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }
)

SKILL_SCRIPTED_NAMES = frozenset(
    {
        "session.intent",
        "planning.grill.started",
        "planning.adr.written",
        "planning.spec.written",
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }
)


class UnknownEventError(ValueError):
    """A caller named a dev-cycle event outside the closed vocabulary — the user-facing spelling, raised only by the constrained ``shipit log event`` path."""


class EventNotRecordedError(RuntimeError):
    """A dev-cycle emission failed past name validation, in the constrained write path's identity or binding seams."""


def emit(
    log: logging.Logger,
    name: str,
    msg: str,
    *args: object,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Emit the dev-cycle event ``name`` as an INFO record on the witnessing subsystem's own ``log``; an unregistered ``name`` raises :class:`ValueError`, and ``extra`` cannot smuggle a divergent event tag."""
    if name not in EVENT_NAMES:
        raise ValueError(
            f"unknown dev-cycle event {name!r}; the closed vocabulary is "
            f"{sorted(EVENT_NAMES)} (ADR-0032) — register a new name in "
            "shipit.events.EVENT_NAMES before emitting it"
        )
    fields = {
        k: v for k, v in dict(extra or {}).items() if k not in (RECORD_KEY, EXTRA_KEY)
    }
    log.info(msg, *args, extra={**fields, EXTRA_KEY: name})


class Sightings:
    """The first-sight registry behind :func:`emit_once` — a passed VALUE, minted at a boundary and threaded, never persisted or module-global."""

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen: set[tuple[Hashable, ...]] = set()


def emit_once(
    sightings: Sightings,
    log: logging.Logger,
    name: str,
    key: tuple[Hashable, ...],
    msg: str,
    *args: object,
    extra: Mapping[str, object] | None = None,
) -> bool:
    """:func:`emit` the event only on FIRST SIGHT of ``(name, *key)`` in ``sightings``; ``key`` is the milestone's identity, and the return says whether a record was written. A suppressed re-sighting leaves no record at all."""
    if name not in EVENT_NAMES:
        raise ValueError(
            f"unknown dev-cycle event {name!r}; the closed vocabulary is "
            f"{sorted(EVENT_NAMES)} (ADR-0032) — register a new name in "
            "shipit.events.EVENT_NAMES before emitting it"
        )
    marker = (name, *key)
    if marker in sightings._seen:
        return False
    sightings._seen.add(marker)
    emit(log, name, msg, *args, extra=extra)
    return True
