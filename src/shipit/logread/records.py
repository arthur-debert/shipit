"""``logread/records`` — parse one JSONL line into a record, and AND-compose the selection."""

from __future__ import annotations

import json
from typing import Any


def parse_record(line: str) -> dict[str, Any] | None:
    """The line as ONE record, or ``None``: only a JSON object counts, and nothing raises."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def normalize_ws(value: int | str) -> int:
    """The Work Stream index as the INT the record carries: ``1``, ``01``, ``WS01`` all mean 1."""
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
    """The record filters as ONE predicate, AND-composed and applied BEFORE the tail count.

    Each filter compares a flat record field for EQUALITY, typed as the record
    carries it; an absent key means unbound, never wildcard. With any filter
    active a non-record line cannot match and is dropped silently.
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
