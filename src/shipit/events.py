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
(verb-witnessed tier; the hook- and skill-scripted tiers enter through the
constrained ``shipit log event`` verb — :mod:`shipit.verbs.logevent` — which
calls this same helper). It logs at INFO on the CALLER's logger — the record stays attributed
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
run. :func:`emit_once` dedupes on a caller-supplied identity key against a
:class:`Sightings` registry — a passed VALUE (ADR-0021 rule 4: no module-global
mutable state), minted at a verb boundary and threaded through the invocation's
evaluations, so one CLI invocation witnesses each milestone at most once while
a later invocation (a fresh registry) legitimately re-witnesses what it
re-reads (ADR-0029/0032 reject any cross-run side store or index). Every such
event carries its identifying fields flat on the record (``review_id``, the
round's head sha, the breaker name), so a reader that wants cross-invocation
uniqueness can dedupe on data, not on record count.
"""

from __future__ import annotations

import logging
from collections.abc import Hashable, Mapping

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

#: The closed dev-cycle event vocabulary (ADR-0032; legacy PRD
#: ``docs/legacy-prd/log04-dev-cycle-event-log.md``). Dot-namespaced
#: ``<noun>.<milestone>`` names, all live: the shipit verbs emit the
#: verb-witnessed names, the managed post-commit hook emits ``commit.created``,
#: and the planning skills script ``session.intent`` + the ``planning.*``
#: family through ``shipit log event``. The registry is additive — adding a
#: name here is the whole downstream cost of a new event type.
EVENT_NAMES = frozenset(
    {
        # session lifecycle
        "session.started",
        "session.intent",
        # substrate + agents
        "tree.created",
        "agent.spawned",
        "agent.done",
        # the pinned launcher's sanctioned dev override (ADR-0033): a shipit
        # invocation running under SHIPIT_EXEC announces the bypass durably —
        # the flow-log twin of the launcher's stderr line, emitted by the
        # exec'd build itself at CLI entry (the bash launcher cannot write the
        # JSONL record; the build it execs can).
        "launcher.overridden",
        # local progress (hook-witnessed tier)
        "commit.created",
        # the onboarding verbs (#434): install and gh-setup narrate their run
        # into the flow record — started at entry, completed on a clean exit,
        # and (the reason these exist) failed with the failing step, so a run
        # that died mid-apply is legible in `shipit logs --flow` instead of
        # leaving only a session-end record behind.
        "install.started",
        "install.completed",
        "install.failed",
        "ghsetup.started",
        "ghsetup.completed",
        "ghsetup.failed",
        # the review loop
        "review.requested",
        "review.received",
        "review.degraded",
        "round.detected",
        "breaker.fired",
        # the round-1 review pipeline (RVW02-WS04 / ADR-0045 / ADR-0052 — the
        # default single pass or the opted-in dimension fan-out), emitted
        # verb-witnessed by the detached review child: the union reached the
        # posted review either through the default MECHANICAL dedup
        # (``review.deduped``, RVW02-WS08 — calibrator off, pass severities
        # kept) or, when a reviewer opts the dormant calibrator back on, the LLM
        # judge (``review.calibrated`` — the union judged onto the one severity
        # ruler); and each judged finding routed OUT of the posted review (its
        # disposition rides the record — the Opportunity-harvest seam's
        # flow-log twin).
        "review.deduped",
        "review.calibrated",
        "finding.dispositioned",
        # per-pass progress inside a review round (RVW03-WS02): a multi-minute
        # round is opaque between launch and union without these — one
        # `launched` per pass/calibrator as it starts and one `settled` as it
        # returns (outcome + duration + run_id/dimension riding the record), so
        # a coordinating agent tailing `shipit logs -f --events` sees which
        # passes are running/settled without waiting for the round to end.
        "review.pass.launched",
        "review.pass.settled",
        # the blocking waiter (`shipit pr wait`, ADR-0034): the wait's own
        # lifecycle — started at entry, one state_changed per poll tick where
        # the observed state moved (the tail-able progress trail), and exactly
        # one terminal record: fired (the awaited condition arrived),
        # actionable (the wait stopped on a state only its caller can clear —
        # a `ready` wait observing `addressing`, the #583 deadlock guard), or
        # timed_out (the hard deadline expired — an advisory state for the
        # supervisor, not a failure of the PR).
        "wait.started",
        "wait.state_changed",
        "wait.fired",
        "wait.actionable",
        "wait.timed_out",
        # a finding's write-once Severity override (RVW02 / ADR-0044): the
        # dormant correction path for a wrong reviewer-emitted severity —
        # keyed by the finding comment's id, written once via `shipit pr
        # classify`; it beats every other rung of the severity precedence
        # chain the engine resolves findings through.
        "finding.severity_overridden",
        # the ready flip and its undo
        "pr.ready",
        "pr.unready",
        # the fleet verification sweep (TOL01-WS07): one run's lifecycle —
        # started at entry, one repo.done per portfolio repo (its red-cell
        # count and adoption-ready verdict ride the record), completed with
        # the fleet totals — the durable flow-log trail behind the committed
        # matrix report artifact.
        "sweep.started",
        "sweep.repo.done",
        "sweep.completed",
        # the planning cycle (skill-scripted tier)
        "planning.grill.started",
        "planning.adr.written",
        "planning.spec.written",
        # Legacy event name retained so old planning records and older
        # installed skills remain readable/accepted.
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }
)

#: The skill-scripted tier's names (ADR-0032; legacy PRD §Implementation Decisions) —
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
        "planning.spec.written",
        # Legacy event name retained for backward compatibility.
        "planning.prd.written",
        "planning.epic.minted",
        "planning.ws.minted",
    }
)


class UnknownEventError(ValueError):
    """A caller named a dev-cycle event outside the closed vocabulary.

    The USER-FACING spelling of the closed-vocabulary guard: raised by the
    constrained write path (``shipit log event``) when the asked-for name is
    not in :data:`EVENT_NAMES`, and mapped to a clean ``error: …`` + exit 1 by
    the CLI error shell (ADR-0030). Distinct from the plain :class:`ValueError`
    :func:`emit` raises for the same condition — an in-code emit with an
    unregistered name is a BUG that must stay a loud traceback, not an input
    error to be dressed up.
    """


class EventNotRecordedError(RuntimeError):
    """A dev-cycle emission failed past name validation.

    The constrained write path's residual-failure refusal (the identity and
    binding seams around the emission — the durable write itself already fails
    open at logging setup): raised by ``shipit log event`` outside hook
    context so the error shell renders it uniformly; hook context swallows the
    same failure to exit 0 instead (fail-open, per the hook canon).
    """


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


class Sightings:
    """The first-sight registry behind :func:`emit_once` — a passed VALUE.

    Holds the ``(name, *key)`` tuples already emitted through it. It is minted
    at a boundary and threaded, never a module global (ADR-0021 rule 4): the
    readiness ``gather`` stamps one onto the snapshot it builds (so evaluating
    the same view twice tags each milestone once), and a verb that gathers more
    than once per invocation (``pr next``: gather → act → the guarded flip's
    re-gather) mints ONE and threads it through every gather, giving "first
    sight" exactly the invocation scope the old process-global set approximated
    — with nothing for a test suite to reset. Never persisted: cross-process
    dedup would need the side store ADR-0029/0032 reject; a fresh invocation
    re-witnesses what it re-reads, carrying the identity fields that let a
    reader dedupe on data.
    """

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
    """:func:`emit` the event only on FIRST SIGHT of ``(name, *key)`` in ``sightings``.

    ``key`` is the milestone's identity — the tuple that makes two sightings the
    SAME milestone (``(slug, pr, review_id)`` for a landed review; ``(slug, pr,
    head_sha)`` for a reviewed head). Include the repo slug where the other
    halves are only repo-unique: one invocation can touch several repos. Returns
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
    if marker in sightings._seen:
        return False
    sightings._seen.add(marker)
    emit(log, name, msg, *args, extra=extra)
    return True
