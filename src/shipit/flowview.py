"""Flow view — the pure rendering of filtered event records as a session story.

See docs/adr/0032-dev-cycle-events-tagged-records-witness-tiers.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

_INTENT_EVENT = "session.intent"

_BARE_HEADER = "session"


def render(
    records: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    show_agents: bool = False,
    header_from: Iterable[Mapping[str, Any]] | None = None,
) -> list[str]:
    """The flow view of ``records`` as lines, header first; ``now`` is injected, and ``header_from`` themes the header when it differs from the body."""
    story = [r for r in records if isinstance(r, Mapping)]
    header_records = (
        story
        if header_from is None
        else [r for r in header_from if isinstance(r, Mapping)]
    )
    lines = [_header(header_records)]
    for record in story:
        when = _relative_time(record.get("ts"), now)
        prefix = _prefix(record)
        body = f"{prefix}{record.get('msg', '')}"
        line = f"{when}  {body}" if when else body
        if show_agents and record.get("agent") is not None:
            line = f"{line}  [agent={record['agent']}]"
        lines.append(line)
    return lines


def _header(records: list[Mapping[str, Any]]) -> str:
    """The story's opening line: the latest ``session.intent`` when present, else the stream's epics in first-appearance order."""
    intent = None
    epics: list[str] = []
    for record in records:
        if record.get("event") == _INTENT_EVENT and record.get("msg"):
            intent = str(record["msg"])
        epic = record.get("epic")
        if isinstance(epic, str) and epic and epic not in epics:
            epics.append(epic)
    if intent:
        return intent
    if epics:
        return "session on " + ", ".join(epics)
    return _BARE_HEADER


def _prefix(record: Mapping[str, Any]) -> str:
    """The ``EPIC-WSnn:`` line prefix a record's domain keys compose, or ``""``; the ``WS`` display form is minted here."""
    epic = record.get("epic")
    if not isinstance(epic, str) or not epic:
        return ""
    ws = record.get("ws")
    if isinstance(ws, int) and not isinstance(ws, bool) and ws >= 1:
        return f"{epic}-WS{ws:02d}: "
    return f"{epic}: "


def _relative_time(ts: Any, now: datetime) -> str:
    """``ts`` as a friendly age relative to ``now`` (``1h34m ago``); ``""`` when it is missing or unparseable."""
    if not isinstance(ts, str):
        return ""
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((now - then).total_seconds())
    if seconds < 1:
        return "just now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h ago" if hours else f"{days}d ago"
    if hours:
        return f"{hours}h{minutes}m ago" if minutes else f"{hours}h ago"
    if minutes:
        return f"{minutes}m ago"
    return f"{secs}s ago"
