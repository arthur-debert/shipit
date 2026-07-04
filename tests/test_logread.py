"""Unit tests for the ``shipit.logread`` domain package (CLI02-WS05).

The reader engine on the ADR-0030 seam, tested prstate-style: typed values
in, values (yielded lines) out — no capsys, no terminal, no monkeypatching.
The follow tests drive the generator with an injected ``sleep`` that mutates
the file per poll tick and ends the stream with ``KeyboardInterrupt``,
proving live behavior (rotation, torn writes, filter uniformity) without a
live terminal loop.
"""

from __future__ import annotations

import json

import pytest

from shipit.logread import (
    Filter,
    LogQuery,
    build_query,
    follow_lines,
    last_n,
    normalize_ws,
    parse_record,
    read_lines,
)


def _record(msg: str, **fields: object) -> str:
    """One JSONL log line the way WS01's file sink writes it — flat fields,
    domain keys present-when-bound."""
    return json.dumps(
        {
            "ts": "2026-07-02T12:00:00Z",
            "level": "info",
            "logger": "shipit.tree",
            "msg": msg,
            **fields,
        }
    )


def _drain(iterator) -> list[str]:
    """Every line the follow generator yields until the injected sleep ends
    the stream the way Ctrl-C would — the iterator seam's test harness."""
    got: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        for line in iterator:
            got.append(line)
    return got


# --------------------------------------------------------------------------
# parse_record — one line, at most one record
# --------------------------------------------------------------------------


def test_parse_record_accepts_only_json_objects():
    assert parse_record(_record("hi", pr=7)) == {
        "ts": "2026-07-02T12:00:00Z",
        "level": "info",
        "logger": "shipit.tree",
        "msg": "hi",
        "pr": 7,
    }
    assert parse_record("{ torn mid-write") is None
    assert parse_record('"a bare string"') is None
    assert parse_record("[1, 2]") is None


# --------------------------------------------------------------------------
# normalize_ws — display forms collapse to the int the record carries
# --------------------------------------------------------------------------


def test_ws_normalizes_from_all_three_input_forms():
    assert normalize_ws("1") == normalize_ws("01") == normalize_ws("WS01") == 1
    assert normalize_ws("ws07") == 7  # any case; display form is never data
    assert normalize_ws(4) == 4


@pytest.mark.parametrize("bad", ["WSx", "zero", "WS00", "0", "-1"])
def test_out_of_grammar_ws_raises_for_the_boundary_to_report(bad):
    with pytest.raises(ValueError) as exc:
        normalize_ws(bad)
    assert "--ws" in str(exc.value)


# --------------------------------------------------------------------------
# Filter — the AND-composed predicate
# --------------------------------------------------------------------------


def test_inactive_filter_is_vacuously_true_even_for_malformed_lines():
    flt = Filter()
    assert not flt.active
    assert flt.matches(_record("anything"))
    assert flt.matches("{ torn")
    assert flt.matches("")


def test_events_only_selects_on_field_presence():
    flt = Filter(events_only=True)
    assert flt.matches(_record("tagged", event="pr.ready"))
    assert not flt.matches(_record("plain mechanics"))


def test_domain_key_filters_compose_as_and_typed_as_the_record_carries():
    flt = Filter(epic="LOG04", ws=1)
    assert flt.matches(_record("in", epic="LOG04", ws=1))
    assert not flt.matches(_record("other ws", epic="LOG04", ws=2))
    # Absent means unbound, not wildcard: a record without the key can't match.
    assert not flt.matches(_record("no ws bound", epic="LOG04"))
    # Typed equality: the record's int ws never matches a string.
    assert not flt.matches(_record("stringly", epic="LOG04", ws="1"))


def test_active_filter_drops_malformed_lines():
    flt = Filter(events_only=True)
    assert not flt.matches("{ torn")
    assert not flt.matches('"a bare string"')


# --------------------------------------------------------------------------
# last_n — the one tail helper
# --------------------------------------------------------------------------


def test_last_n_tail_semantics_including_the_minus_zero_trap():
    items = ["a", "b", "c"]
    assert last_n(items, -1) == items
    assert last_n(items, 0) == []  # not items[-0:] == everything
    assert last_n(items, 2) == ["b", "c"]
    assert last_n(items, 9) == items


# --------------------------------------------------------------------------
# LogQuery / build_query — one frozen value per read, minted at parse
# --------------------------------------------------------------------------


def test_build_query_normalizes_ws_and_flow_implies_events():
    query = build_query(flow=True, ws="WS03", tail=7)
    assert query.record_filter.events_only  # --flow implies --events
    assert query.record_filter.fields["ws"] == 3
    assert query.tail == 7
    assert query.flow and not query.raw and not query.follow


@pytest.mark.parametrize("kwargs", [{"raw": True}, {"follow": True}])
def test_flow_contradictions_are_unbuildable(kwargs):
    with pytest.raises(ValueError) as exc:
        build_query(flow=True, **kwargs)
    assert "--flow" in str(exc.value)
    with pytest.raises(ValueError):
        LogQuery(flow=True, **kwargs)  # construction IS the validation


def test_log_query_is_frozen():
    query = build_query(pr=231)
    with pytest.raises(AttributeError):
        query.tail = 3  # type: ignore[misc]


# --------------------------------------------------------------------------
# read_lines — the static read: filter, THEN tail
# --------------------------------------------------------------------------


def test_read_lines_filters_before_the_tail_count(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(
        "\n".join(
            [
                _record("about 231", pr=231),
                _record("noise", pr=7),
                _record("more noise", pr=7),
            ]
        )
        + "\n"
    )
    # tail 1 + filter means "the last line ABOUT pr 231", not "the last line,
    # if it happens to match".
    assert read_lines(log, Filter(pr=231), tail=1) == [_record("about 231", pr=231)]


def test_read_lines_without_filter_passes_blank_and_malformed_through(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("good") + "\n\n{ torn\n")
    # Their treatment (passthrough, note, drop) is the caller's rendering
    # contract — the engine yields the stored lines as they are.
    assert read_lines(log, Filter()) == [_record("good"), "", "{ torn"]
    assert read_lines(log, Filter(events_only=True)) == []


def test_read_lines_tail_zero_is_no_lines(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("a") + "\n" + _record("b") + "\n")
    assert read_lines(log, Filter(), tail=0) == []


# --------------------------------------------------------------------------
# follow_lines — the live read as an iterator (no terminal anywhere)
# --------------------------------------------------------------------------


def test_follow_yields_the_tail_then_each_appended_line(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("old1") + "\n" + _record("old2") + "\n")

    appended = [_record("new line A", pr=231), _record("new line B")]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=1, sleep=fake_sleep))
    # The pre-follow tail honored N=1 (old2 only), then the appends streamed.
    assert [json.loads(line)["msg"] for line in got] == [
        "old2",
        "new line A",
        "new line B",
    ]


def test_follow_applies_the_same_filter_to_appended_lines(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("review requested", pr=231, event="review.requested") + "\n")
    appended = [
        _record("noise while following", pr=231),
        _record("pr#231 flipped ready", pr=231, event="pr.ready"),
        _record("other pr's event", pr=7, event="pr.ready"),
    ]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    got = _drain(
        follow_lines(log, Filter(events_only=True, pr=231), tail=-1, sleep=fake_sleep)
    )
    assert [json.loads(line)["msg"] for line in got] == [
        "review requested",
        "pr#231 flipped ready",
    ]


def test_follow_reassembles_a_torn_write_before_filtering(tmp_path):
    # A concurrent write can be read mid-line, so readline() returns a fragment
    # with no trailing newline. A field filter parses to select, so a naive read
    # would drop the fragment AND its remainder (neither half is valid JSON) —
    # losing the record permanently. The engine buffers until the newline
    # lands, then judges the whole line. Regression for the torn-read drop (agy).
    log = tmp_path / "shipit.log"
    log.write_text(_record("pre", pr=231, event="pr.ready") + "\n")

    whole = _record("torn but tagged", pr=231, event="pr.ready")
    cut = len(whole) // 2
    # Tick 1 writes the first half (no newline); tick 2 completes it.
    fragments = [whole[:cut], whole[cut:] + "\n"]

    def fake_sleep(_interval: float) -> None:
        if fragments:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(fragments.pop(0))
        else:
            raise KeyboardInterrupt

    got = _drain(
        follow_lines(log, Filter(events_only=True, pr=231), tail=-1, sleep=fake_sleep)
    )
    assert whole in got  # survived being split across two reads


def test_follow_reassembles_a_torn_line_present_at_start(tmp_path):
    # agy [ERROR]: the initial tail read must be buffer-aware too, not just the
    # append loop. If the file ALREADY ends in a torn write (no newline) when
    # follow opens, a naive `read().splitlines()` yields the head now, then the
    # append loop reads the remainder and yields it — one record split into two.
    # The initial read seeds the same `pending` buffer, so the halves reunite.
    log = tmp_path / "shipit.log"
    whole = _record("torn at start", pr=231, event="pr.ready")
    cut = len(whole) // 2
    # File opens with only the first half on disk (no trailing newline).
    log.write_text(whole[:cut])
    remainder = [whole[cut:] + "\n"]

    def fake_sleep(_interval: float) -> None:
        if remainder:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(remainder.pop(0))
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=-1, sleep=fake_sleep))
    # The record comes out exactly once, whole — not head then tail.
    assert got == [whole]


def test_follow_reopens_after_in_place_truncation(tmp_path):
    # The writer is a RotatingFileHandler: the active shipit.log can be rolled
    # over mid-follow. A follow that holds one handle would then track the stale
    # renamed file and go silent — so it must reopen when the file shrinks.
    log = tmp_path / "shipit.log"
    log.write_text(_record("before-rotation-with-some-padding-to-be-longer") + "\n")

    steps = [lambda: log.write_text(_record("after") + "\n")]

    def fake_sleep(_interval: float) -> None:
        if steps:
            steps.pop(0)()
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=0, sleep=fake_sleep))
    assert got == [_record("after")]


def test_follow_reopens_after_rename_rotation_even_when_new_file_is_larger(tmp_path):
    # RotatingFileHandler rotates by RENAME + fresh create. A size-only check
    # races: a busy fresh file can outgrow the old read offset between polls,
    # so the shrink is never observed and the follow clings to the renamed
    # handle forever. Identity (inode) must be what detects the swap — here the
    # replacement file is deliberately LARGER than the followed offset.
    log = tmp_path / "shipit.log"
    log.write_text(_record("old") + "\n")

    def rotate() -> None:
        log.rename(log.with_name("shipit.log.1"))
        lines = [
            _record(f"fresh-{i}-padding-so-the-new-file-is-bigger") for i in range(20)
        ]
        log.write_text("\n".join(lines) + "\n")

    steps = [rotate]

    def fake_sleep(_interval: float) -> None:
        if steps:
            steps.pop(0)()
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=0, sleep=fake_sleep))
    msgs = [json.loads(line)["msg"] for line in got]
    assert msgs[0] == "fresh-0-padding-so-the-new-file-is-bigger"
    assert msgs[-1] == "fresh-19-padding-so-the-new-file-is-bigger"


def test_follow_yields_stored_lines_verbatim_including_malformed(tmp_path):
    # The engine renders nothing: with no filter active, even a torn appended
    # line is yielded exactly as stored — noting or dropping it is the verb's
    # rendering contract over this iterator's output.
    log = tmp_path / "shipit.log"
    log.write_text(_record("pre") + "\n")
    appended = ["{ torn mid-write", _record("post")]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=-1, sleep=fake_sleep))
    assert got == [_record("pre"), "{ torn mid-write", _record("post")]
