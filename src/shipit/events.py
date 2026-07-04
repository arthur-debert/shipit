"""Dev-cycle event registry + emit core (ADR-0032 / LOG04) — the ONE write path
for tagged milestone records.

A dev-cycle event is an ORDINARY log record — same per-repo JSONL file, same
pipeline (context-merge, redaction, rotation), same reader — distinguished only
by an ``event`` field carrying a dot-namespaced name from :data:`EVENT_NAMES`,
a CLOSED, additive vocabulary: an unregistered name raises :class:`ValueError`
at the emit site (the :data:`shipit.logcontext.DOMAIN_KEYS` discipline), so a
typo — or an agent diary — can never mint a new event type. The registry is
where new types get debated; adding one is adding a name here, touching nothing
downstream (the reader selects on the ``event`` field's presence, never on a
name list of its own).

:func:`emit` is the one internal helper every witnessing verb calls
(verb-witnessed tier; the hook- and skill-scripted tiers arrive via the
``shipit log event`` verb in a later Work Stream — it will call this same
helper). It logs at INFO on the CALLER's logger — the record stays attributed
to the subsystem that witnessed the milestone — and the bound domain keys land
on it through the one pipeline like any other record, so an event is exactly as
correlated as the moment it was witnessed: present-when-bound, absent keys stay
absent.

:func:`emit_once` is the FIRST-SIGHT variant for *observational* milestones —
ones the engine can only witness by re-reading GitHub state it did not itself
change (a landed review at gather, a reviewed head, a fired breaker). A verb
like ``pr next`` evaluates the same snapshot shape several times in one
invocation (gather → act → the guarded flip's re-gather), so a plain
:func:`emit` at those seams would tag the same milestone two or three times per
run. :func:`emit_once` dedupes on a caller-supplied identity key for the LIFE
OF THE PROCESS — exactly the scope shipit state has (ADR-0029/0032 reject any
side store or index): one CLI invocation witnesses each milestone at most once,
while a later invocation legitimately re-witnesses what it re-reads. Every such
event carries its identifying fields flat on the record (``review_id``, the
round's head sha, the breaker name), so a reader that wants cross-invocation
uniqueness can dedupe on data, not on record count.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Mapping

#: The closed dev-cycle event vocabulary (ADR-0032; PRD
#: ``docs/prd/log04-dev-cycle-event-log.md``). Dot-namespaced
#: ``<noun>.<milestone>`` names; the verb-witnessed tier emits them all as of
#: LOG04-WS02 (the hook- and skill-scripted names — ``commit.created``, the
#: planning family, ``session.intent`` — arrive with their tiers) — the
#: registry is additive, and registering is not emitting.
#: The flat field a dev-cycle event's name lands under on the durable JSONL
#: record — what the reader (``shipit logs --events``) selects on.
RECORD_KEY = "event"

#: The LogRecord-side carrier of the event name between :func:`emit` and the
#: render seam. It cannot be ``event`` itself: inside the structlog pipeline
#: ``event_dict["event"]`` IS the human message until the final
#: ``EventRenamer`` step, so a colliding extra would be silently dropped by
#: ``ExtraAdder``. The renderers (:mod:`shipit.logsetup`) rename this back to
#: :data:`RECORD_KEY` — structlog's own ``EventRenamer(replace_by=…)`` pattern.
EXTRA_KEY = "_event"

EVENT_NAMES = frozenset(
    {
        # session lifecycle
        "session.started",
        "session.intent",
        # substrate + agents
        "tree.created",
        "agent.spawned",
        "agent.done",
        # local progress (hook-witnessed tier)
        "commit.created",
        # the review loop
        "review.requested",
        "review.received",
        "review.degraded",
        "round.detected",
        "breaker.fired",
        # the ready flip and its undo
        "pr.ready",
        "pr.unready",
        # the planning cycle (skill-scripted tier)
        "planning.grill.started",
        "planning.adr.written",
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }
)

#: The skill-scripted tier's names (ADR-0032; PRD §Implementation Decisions) —
#: the only events whose human ``msg`` may come from the caller (the emit
#: verb's ``--about``), because a skill checkpoint is the one witness that
#: knows the crystallized intent ("planning session: reviewer symmetry").
#: Verb- and hook-witnessed events compose their own ``msg`` at the witnessing
#: site, so the constrained verb ignores ``--about`` for every name outside
#: this set — the freeform-prose slot stays exactly as wide as the tier that
#: needs it. Registry data, not verb policy: the tier a name belongs to is
#: decided here, where new names get debated.
SKILL_SCRIPTED_NAMES = frozenset(
    {
        "session.intent",
        "planning.grill.started",
        "planning.adr.written",
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }
)


def emit(
    log: logging.Logger,
    name: str,
    msg: str,
    *args: object,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Emit the dev-cycle event ``name`` as an INFO record on ``log``.

    ``log`` is the witnessing subsystem's own logger (``shipit.prstate``, …) so
    the record's ``logger`` field keeps attributing the milestone to where it
    happened. ``msg``/``args`` are the ordinary human message (the LOG02 domain
    phrase — the event name is the TYPE, never the prose). ``extra`` adds flat
    per-event fields (``reviewer=…``) exactly as a plain log call would; the
    event tag itself always comes from ``name`` — it rides the LogRecord as
    :data:`EXTRA_KEY` and lands durably as :data:`RECORD_KEY`, and an ``extra``
    cannot smuggle a divergent value under either key.

    An unregistered ``name`` raises :class:`ValueError` — fail loud at the emit
    site, the closed-vocabulary guard that keeps the durable record a milestone
    trail rather than a diary. The bound domain keys ride in via the pipeline's
    context-merge; nothing is re-bound here.
    """
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


#: The process-lifetime first-sight registry behind :func:`emit_once`:
#: ``(name, *key)`` tuples already emitted by THIS process. Never persisted —
#: cross-process dedup would need the side store ADR-0029/0032 reject; a fresh
#: invocation re-witnesses what it re-reads, carrying the identity fields that
#: let a reader dedupe on data. Tests reset it via the conftest fixture.
_seen: set[tuple[Hashable, ...]] = set()


def emit_once(
    log: logging.Logger,
    name: str,
    key: tuple[Hashable, ...],
    msg: str,
    *args: object,
    extra: Mapping[str, object] | None = None,
) -> bool:
    """:func:`emit` the event only on FIRST SIGHT of ``(name, *key)`` this process.

    ``key`` is the milestone's identity — the tuple that makes two sightings the
    SAME milestone (``(slug, pr, review_id)`` for a landed review; ``(slug, pr,
    head_sha)`` for a reviewed head). Include the repo slug where the other
    halves are only repo-unique: one process can touch several repos. Returns
    whether the event was emitted, so an emitting seam can assert/act on the
    outcome; a suppressed re-sighting leaves NO record at all (not even DEBUG —
    re-reading known state is not a milestone). Same closed-vocabulary guard as
    :func:`emit` (an unregistered ``name`` raises before the registry is
    touched, so a typo never poisons the seen-set either).
    """
    if name not in EVENT_NAMES:
        raise ValueError(
            f"unknown dev-cycle event {name!r}; the closed vocabulary is "
            f"{sorted(EVENT_NAMES)} (ADR-0032) — register a new name in "
            "shipit.events.EVENT_NAMES before emitting it"
        )
    marker = (name, *key)
    if marker in _seen:
        return False
    _seen.add(marker)
    emit(log, name, msg, *args, extra=extra)
    return True
