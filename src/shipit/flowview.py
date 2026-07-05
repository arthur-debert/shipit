"""Flow view (LOG04 / ADR-0032) — render filtered event records as the session story.

The deep-module half of ``shipit logs --flow``: a PURE function from parsed
JSONL records to the rendered story — no I/O, no clock read (``now`` is an
argument), no filtering of its own. The reader (:mod:`shipit.verbs.logs`) owns
selection (``--flow`` implies ``--events``, the domain-key filters compose as
AND) and hands the surviving records here in file order; this module owns only
how a story LOOKS:

- a **header** line: the session's ``session.intent`` event when one is present
  (its ``msg`` — the operator's one-line purpose, the latest winning because
  intent crystallizes over a session), else a theme INFERRED from the stream's
  epics (``session on LOG04, RVW01`` in order of first appearance), else the
  bare ``session``;
- one line per record: a friendly **relative time** (``1h34m ago`` — staleness
  without ISO-8601 arithmetic), an **``EPIC-WSnn:`` prefix** composed from the
  record's domain keys (the ``WS`` prefix and zero-padding are RENDERING,
  applied here — ``ws`` is an int on the record, never ``"WS01"``; an
  epic-only record renders ``EPIC:``, a keyless one no prefix), and the
  record's human ``msg``;
- **agent ids** displayed only behind the ``show_agents`` flag — the data rides
  every record regardless (collected always), the default view stays clean.

Total on wire data, the reader's resilience contract continued: a non-mapping
record is skipped, a missing ``msg`` renders empty, an unparseable ``ts``
renders with no time — a corrupt record degrades its own line, never the view.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

#: The event whose ``msg`` becomes the header verbatim (ADR-0032: emitted when
#: a session's purpose crystallizes; skill-scripted, so best-effort by design).
_INTENT_EVENT = "session.intent"

#: Header fallback when the stream carries neither an intent nor any epic key.
_BARE_HEADER = "session"


def render(
    records: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    show_agents: bool = False,
    header_from: Iterable[Mapping[str, Any]] | None = None,
) -> list[str]:
    """The flow view of ``records``: a header line, then one story line each.

    ``records`` are parsed JSONL records in file order (the reader already
    filtered them); anything that is not a mapping is skipped — same
    never-crash posture as the reader's malformed-line contract. ``now`` is the
    reference instant for the relative times (injected: this function never
    reads a clock, so a test pins the rendering exactly). ``show_agents``
    appends ``[agent=<id>]`` to lines whose record carries the ``agent`` domain
    key — the id is on the record either way; the flag is display only.

    ``header_from`` is the set the HEADER themes from (its intent/epics), for
    when ``records`` is a tail of a longer session: the body lists the tail but
    the header still opens on the whole session's intent, even when the
    ``session.intent`` event fell before the tail window. It defaults to
    ``records`` (header and body over the same set — the reader owns whether
    they differ, keeping the tail a selection concern out here).

    Returns the rendered LINES (header first) rather than one string, so the
    caller owns line emission (flushing per line, the reader's piping
    contract).
    """
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
    """The story's opening line: intent when present, inferred theme otherwise.

    The LATEST ``session.intent`` wins — intent crystallizes (a session may
    restate its purpose as planning sharpens it), so the freshest statement is
    the truest. With no intent, the theme is the stream's epics in order of
    first appearance: multiple epics are one session's story too (a coordinator
    spans them), so all are named rather than guessing a primary.
    """
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
    """The ``EPIC-WSnn:`` line prefix a record's domain keys compose, or ``""``.

    ``ws`` is an int on the record (ADR-0032: ``WS01`` is a display form, never
    data), so the display form is minted HERE — ``WS`` prefix, ``%02d``
    zero-padding, widening naturally past 99 (``WS100``). A ``ws`` without an
    epic composes nothing (a bare Work Stream index names no thread), and a
    non-positive or non-int ``ws`` — out of the branch grammar, so only a
    hand-forged record — degrades to the epic-only form rather than rendering
    nonsense.
    """
    epic = record.get("epic")
    if not isinstance(epic, str) or not epic:
        return ""
    ws = record.get("ws")
    if isinstance(ws, int) and not isinstance(ws, bool) and ws >= 1:
        return f"{epic}-WS{ws:02d}: "
    return f"{epic}: "


def _relative_time(ts: Any, now: datetime) -> str:
    """``ts`` as a friendly age relative to ``now`` (``1h34m ago``), or ``""``.

    Coarse by design — the two coarsest units, and no finer. The day and hour
    tiers pair their unit with the next one down (``2d4h``, ``1h34m``) and drop
    a zero minor unit (``2h ago``, not ``2h0m ago``); under an hour it is a
    single unit, minutes then seconds (``5m ago``, ``42s ago`` — seconds never
    trail a minute). Anything under a second — including a clock-skewed FUTURE
    ``ts``, which must not render a negative age — is ``just now``. A missing or
    unparseable ``ts`` renders as no time at all: the record's story line
    survives on its other fields.
    """
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
