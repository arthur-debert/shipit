from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shipit import flowview

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _ts(ago: timedelta) -> str:
    return (NOW - ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(msg: str, *, ago: timedelta = timedelta(minutes=5), **fields: object):
    return {
        "ts": _ts(ago),
        "level": "info",
        "logger": "shipit.prstate",
        "msg": msg,
        "event": "review.requested",
        **fields,
    }


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
    assert lines[1].endswith("RVW01-WS01: a")
    assert lines[2].endswith("LOG04-WS02: b")
    assert lines[3].endswith("RVW01-WS03: c")


def test_keyless_stream_falls_back_to_the_bare_header():
    lines = flowview.render([_event("no keys bound")], now=NOW)
    assert lines[0] == "session"


def test_ws_prefix_is_zero_padded_from_the_int_domain_key():
    lines = flowview.render([_event("spawned", epic="LOG04", ws=1)], now=NOW)
    assert "LOG04-WS01: spawned" in lines[1]


def test_ws_prefix_widens_past_two_digits():
    lines = flowview.render([_event("spawned", epic="BIG", ws=100)], now=NOW)
    assert "BIG-WS100: spawned" in lines[1]


def test_epic_only_record_renders_epic_prefix():
    lines = flowview.render([_event("umbrella work", epic="LOG04")], now=NOW)
    assert "LOG04: umbrella work" in lines[1]


def test_record_without_domain_keys_renders_bare_msg():
    lines = flowview.render([_event("session started")], now=NOW)
    assert lines[1] == "5m ago  session started"


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
    fresh = flowview.render([_event("f", ago=timedelta(0))], now=NOW)
    future = flowview.render([_event("g", ago=timedelta(seconds=-30))], now=NOW)
    assert fresh[1].startswith("just now")
    assert future[1].startswith("just now")


def test_unparseable_ts_renders_the_line_without_a_time():
    record = _event("still told", epic="LOG04", ws=4)
    record["ts"] = "not-a-timestamp"
    lines = flowview.render([record], now=NOW)
    assert lines[1] == "LOG04-WS04: still told"


def test_non_mapping_records_are_skipped_never_crash():
    records = [None, "torn", 42, _event("survivor", epic="LOG04")]
    lines = flowview.render(records, now=NOW)
    assert len(lines) == 2
    assert "survivor" in lines[1]


def test_missing_msg_renders_an_empty_story_line_not_a_crash():
    record = _event("gone", epic="LOG04", ws=4)
    del record["msg"]
    lines = flowview.render([record], now=NOW)
    assert lines[1].endswith("LOG04-WS04: ")
