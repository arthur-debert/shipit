"""Unit tests for the flow renderer (LOG04-WS04) — record streams in, views out.

The renderer is a PURE function (:func:`shipit.flowview.render`): no I/O, no
clock (``now`` injected), no filtering of its own. Tests feed parsed records
and assert the rendered view as external behavior — the intent header, the
inferred-theme fallback, a multi-epic session, the agent-id display toggle,
relative-time formatting, and the never-crash posture on degraded records.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shipit import flowview

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _ts(ago: timedelta) -> str:
    """An ISO-8601-UTC ``ts`` the file sink's shape, ``ago`` before :data:`NOW`."""
    return (NOW - ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(msg: str, *, ago: timedelta = timedelta(minutes=5), **fields: object):
    """One parsed event record the reader would hand the renderer."""
    return {
        "ts": _ts(ago),
        "level": "info",
        "logger": "shipit.prstate",
        "msg": msg,
        "event": "review.requested",
        **fields,
    }


# --------------------------------------------------------------------------
# The header — session.intent when present, inferred theme otherwise
# --------------------------------------------------------------------------


def test_intent_header_opens_the_view():
    records = [
        _event(
            "planning session: reviewer symmetry",
            event="session.intent",
            ago=timedelta(hours=2),
        ),
        _event("review request attached", epic="RVW01", ws=1, pr=368),
    ]
    lines = flowview.render(records, now=NOW)
    assert lines[0] == "planning session: reviewer symmetry"


def test_latest_intent_wins_when_it_crystallizes_twice():
    records = [
        _event("session", event="session.intent", ago=timedelta(hours=3)),
        _event("tuning the review loop", event="session.intent"),
    ]
    lines = flowview.render(records, now=NOW)
    assert lines[0] == "tuning the review loop"


def test_theme_is_inferred_from_epics_when_no_intent():
    records = [_event("spawned", epic="LOG04", ws=4)]
    lines = flowview.render(records, now=NOW)
    assert lines[0] == "session on LOG04"


def test_multi_epic_session_names_every_epic_in_first_appearance_order():
    records = [
        _event("a", epic="RVW01", ws=1),
        _event("b", epic="LOG04", ws=2),
        _event("c", epic="RVW01", ws=3),
    ]
    lines = flowview.render(records, now=NOW)
    assert lines[0] == "session on RVW01, LOG04"
    # ...and each line keeps its own thread's prefix — parallel Work Streams
    # read as separate threads of one story.
    assert lines[1].endswith("RVW01-WS01: a")
    assert lines[2].endswith("LOG04-WS02: b")
    assert lines[3].endswith("RVW01-WS03: c")


def test_keyless_stream_falls_back_to_the_bare_header():
    lines = flowview.render([_event("no keys bound")], now=NOW)
    assert lines[0] == "session"


# --------------------------------------------------------------------------
# Line composition — EPIC-WSnn prefixes minted at render time
# --------------------------------------------------------------------------


def test_ws_prefix_is_zero_padded_from_the_int_domain_key():
    # ws is DATA as an int (ADR-0032); "WS01" is minted here, at render time.
    lines = flowview.render([_event("spawned", epic="LOG04", ws=1)], now=NOW)
    assert "LOG04-WS01: spawned" in lines[1]


def test_ws_prefix_widens_past_two_digits():
    lines = flowview.render([_event("spawned", epic="BIG", ws=100)], now=NOW)
    assert "BIG-WS100: spawned" in lines[1]


def test_epic_only_record_renders_epic_prefix():
    # An umbrella-branch record carries epic but no ws (branchid's truth table).
    lines = flowview.render([_event("umbrella work", epic="LOG04")], now=NOW)
    assert "LOG04: umbrella work" in lines[1]


def test_record_without_domain_keys_renders_bare_msg():
    lines = flowview.render([_event("session started")], now=NOW)
    # No epic key → no thread prefix; the msg follows the time directly.
    assert lines[1] == "5m ago  session started"


# --------------------------------------------------------------------------
# Agent ids — collected always, displayed behind the flag
# --------------------------------------------------------------------------


def test_agent_id_is_hidden_by_default():
    records = [_event("spawned", epic="LOG04", ws=4, agent="run-af12")]
    lines = flowview.render(records, now=NOW)
    assert "run-af12" not in "\n".join(lines)


def test_agent_id_shows_behind_the_flag():
    records = [_event("spawned", epic="LOG04", ws=4, agent="run-af12")]
    lines = flowview.render(records, now=NOW, show_agents=True)
    assert "[agent=run-af12]" in lines[1]


def test_agent_flag_leaves_agentless_records_unmarked():
    lines = flowview.render([_event("no agent here")], now=NOW, show_agents=True)
    assert "[agent=" not in lines[1]


# --------------------------------------------------------------------------
# Relative times — friendly ages, no ISO-8601 arithmetic
# --------------------------------------------------------------------------


def test_relative_time_renders_hours_and_minutes():
    records = [_event("x", ago=timedelta(hours=1, minutes=34))]
    lines = flowview.render(records, now=NOW)
    assert lines[1].startswith("1h34m ago")


def test_relative_time_drops_a_zero_minor_unit():
    records = [_event("x", ago=timedelta(hours=2))]
    lines = flowview.render(records, now=NOW)
    assert lines[1].startswith("2h ago")


def test_relative_time_renders_minutes_seconds_and_days():
    minutes = flowview.render([_event("m", ago=timedelta(minutes=5))], now=NOW)
    seconds = flowview.render([_event("s", ago=timedelta(seconds=42))], now=NOW)
    days = flowview.render([_event("d", ago=timedelta(days=2, hours=4))], now=NOW)
    assert minutes[1].startswith("5m ago")
    assert seconds[1].startswith("42s ago")
    assert days[1].startswith("2d4h ago")


def test_sub_second_and_future_ts_render_just_now():
    # A clock-skewed future ts must not render a negative age.
    fresh = flowview.render([_event("f", ago=timedelta(0))], now=NOW)
    future = flowview.render([_event("g", ago=timedelta(seconds=-30))], now=NOW)
    assert fresh[1].startswith("just now")
    assert future[1].startswith("just now")


# --------------------------------------------------------------------------
# Resilience — degraded records degrade their line, never the view
# --------------------------------------------------------------------------


def test_unparseable_ts_renders_the_line_without_a_time():
    record = _event("still told", epic="LOG04", ws=4)
    record["ts"] = "not-a-timestamp"
    lines = flowview.render([record], now=NOW)
    assert lines[1] == "LOG04-WS04: still told"


def test_non_mapping_records_are_skipped_never_crash():
    records = [None, "torn", 42, _event("survivor", epic="LOG04")]
    lines = flowview.render(records, now=NOW)
    assert len(lines) == 2  # header + the one real record
    assert "survivor" in lines[1]


def test_missing_msg_renders_an_empty_story_line_not_a_crash():
    record = _event("gone", epic="LOG04", ws=4)
    del record["msg"]
    lines = flowview.render([record], now=NOW)
    assert lines[1].endswith("LOG04-WS04: ")
