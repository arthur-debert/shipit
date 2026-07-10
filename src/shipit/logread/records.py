"""Records and selection — the pure halves LOG04 built, in their domain home.

One JSONL line becomes at most one record (:func:`parse_record`), a set of
CLI filters becomes ONE predicate (:class:`Filter`), and a Work Stream's
display form becomes the int the record carries (:func:`normalize_ws`).
Moved as-is from the verb layer (CLI02 / ADR-0030) — same contracts, now
importable without a terminal in sight.
"""

from __future__ import annotations

import json
from typing import Any


def parse_record(line: str) -> dict[str, Any] | None:
    """The line as ONE JSONL record (a JSON object), or ``None``.

    The single parse every selecting/rendering path shares: only a JSON object
    is a record — any other parse (a torn write, a bare JSON string) is the
    caller's cue to apply its own resilience contract (skip with a note, drop
    silently under a filter), never a crash.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def normalize_ws(value: int | str) -> int:
    """The Work Stream index ``value`` names, as the INT the record carries.

    The CLI never punishes the display form (PRD): ``1``, ``01``, and ``WS01``
    (any case) all name Work Stream 1 — the ``WS`` prefix and zero-padding are
    rendering, stripped here, because the durable record's ``ws`` domain key is
    int-typed (ADR-0032) and selection compares against THAT. Anything else —
    garbage text, or a non-positive index the branch grammar could never have
    written (``shipit.branchid`` derives ``WS00`` to nothing for the same
    reason) — raises :class:`ValueError` for the caller to report as a usage
    error.
    """
    text = str(value).strip()
    if text.upper().startswith("WS"):
        text = text[2:]
    if not text.isdigit():
        raise ValueError(
            f"--ws must be a Work Stream index (1, 01, or WS01); got {value!r}"
        )
    index = int(text)
    if index < 1:
        raise ValueError(
            f"--ws must be a positive Work Stream index (the branch grammar "
            f"starts at WS01); got {value!r}"
        )
    return index


class Filter:
    """The record filters (LOG04) as ONE predicate.

    Filters compose as AND and are applied BEFORE the tail count and before
    either output mode, so ``-n 5 --pr 231`` means "the last 5 records about
    pr#231" and ``--raw`` pipes exactly the matching stored lines to jq.
    Selection is on the record's flat fields: ``events_only`` keeps only
    records carrying an ``event`` field (a dev-cycle event, ADR-0032 —
    presence is the test, never a name list of the reader's own); each
    domain-key filter (``pr``, ``session``, ``epic``, ``ws``, ``agent``,
    ``role``) keeps records whose key EQUALS the value — typed as the record
    carries it (``pr``/``ws`` int, the rest strings, ADR-0029/0032), which is
    why the CLI boundary normalizes ``WS01`` to ``1`` before it gets here. A
    record without the key cannot match it: absent means unbound, not
    wildcard.

    The review-observability trio (RVW03-WS02) selects the same way on the
    review sub-agent EXTRAS the fan-out stamps per record — not domain keys,
    but flat fields all the same: ``reviewer`` (the reviewing agent),
    ``run_id`` (one pass/calibrator run), ``round_id`` (one fan-out round) —
    so ``shipit logs --run <id>`` isolates one pass's interleaved lines and
    ``--round <id>`` groups a whole round's.

    Filtering requires parsing, so with any filter ACTIVE a non-record line
    (blank padding, a torn write) simply cannot match and is dropped silently —
    in both modes: a malformed line's fields are unknowable, and surfacing it
    under a field filter would be a false positive. With NO filter active the
    predicate is vacuously true and both modes keep their unfiltered contracts
    (raw passes malformed lines through; rendered notes them on stderr).
    """

    def __init__(
        self,
        *,
        events_only: bool = False,
        pr: int | None = None,
        session: str | None = None,
        epic: str | None = None,
        ws: int | None = None,
        agent: str | None = None,
        role: str | None = None,
        reviewer: str | None = None,
        run_id: str | None = None,
        round_id: str | None = None,
    ) -> None:
        self.events_only = events_only
        self.fields = {
            name: value
            for name, value in {
                "pr": pr,
                "session": session,
                "epic": epic,
                "ws": ws,
                "agent": agent,
                "role": role,
                "reviewer": reviewer,
                "run_id": run_id,
                "round_id": round_id,
            }.items()
            if value is not None
        }

    @property
    def active(self) -> bool:
        return self.events_only or bool(self.fields)

    def matches_record(self, record: dict[str, Any]) -> bool:
        if self.events_only and "event" not in record:
            return False
        return all(record.get(name) == value for name, value in self.fields.items())

    def matches(self, line: str) -> bool:
        if not self.active:
            return True
        record = parse_record(line)
        if record is None:
            return False
        return self.matches_record(record)
