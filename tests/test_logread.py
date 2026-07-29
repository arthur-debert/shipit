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
    got: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        for line in iterator:
            got.append(line)
    return got


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


def test_ws_normalizes_from_all_three_input_forms():
    assert normalize_ws("1") == normalize_ws("01") == normalize_ws("WS01") == 1
    assert normalize_ws("ws07") == 7
    assert normalize_ws(4) == 4


@pytest.mark.parametrize("bad", ["WSx", "zero", "WS00", "0", "-1"])
def test_out_of_grammar_ws_raises_for_the_boundary_to_report(bad):
    with pytest.raises(ValueError) as exc:
        normalize_ws(bad)
    assert "--ws" in str(exc.value)


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
    assert not flt.matches(_record("no ws bound", epic="LOG04"))
    assert not flt.matches(_record("stringly", epic="LOG04", ws="1"))


def test_active_filter_drops_malformed_lines():
    flt = Filter(events_only=True)
    assert not flt.matches("{ torn")
    assert not flt.matches('"a bare string"')


def test_review_correlation_filters_select_on_the_pass_extras():
    flt = Filter(run_id="abc123")
    assert flt.matches(_record("pass line", run_id="abc123", dimension="bugs"))
    assert not flt.matches(_record("other pass", run_id="def456"))
    assert not flt.matches(_record("uncorrelated line"))

    both = Filter(reviewer="codex", round_id="r-1")
    assert both.matches(_record("in", reviewer="codex", round_id="r-1"))
    assert not both.matches(_record("other round", reviewer="codex", round_id="r-2"))
    assert not both.matches(_record("other reviewer", reviewer="agy", round_id="r-1"))


def test_build_query_threads_the_review_correlation_filters():
    query = build_query(reviewer="codex", run_id="run-1", round_id="round-1")
    assert query.record_filter.fields["reviewer"] == "codex"
    assert query.record_filter.fields["run_id"] == "run-1"
    assert query.record_filter.fields["round_id"] == "round-1"
    assert query.record_filter.active


def test_last_n_tail_semantics_including_the_minus_zero_trap():
    items = ["a", "b", "c"]
    assert last_n(items, -1) == items
    assert last_n(items, 0) == []
    assert last_n(items, 2) == ["b", "c"]
    assert last_n(items, 9) == items


def test_build_query_normalizes_ws_and_flow_implies_events():
    query = build_query(flow=True, ws="WS03", tail=7)
    assert query.record_filter.events_only
    assert query.record_filter.fields["ws"] == 3
    assert query.tail == 7
    assert query.flow and not query.raw and not query.follow


@pytest.mark.parametrize("kwargs", [{"raw": True}, {"follow": True}])
def test_flow_contradictions_are_unbuildable(kwargs):
    with pytest.raises(ValueError) as exc:
        build_query(flow=True, **kwargs)
    assert "--flow" in str(exc.value)
    with pytest.raises(ValueError):
        LogQuery(flow=True, **kwargs)


def test_log_query_is_frozen():
    query = build_query(pr=231)
    with pytest.raises(AttributeError):
        query.tail = 3  # type: ignore[misc]


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
    assert read_lines(log, Filter(pr=231), tail=1) == [_record("about 231", pr=231)]


def test_read_lines_without_filter_passes_blank_and_malformed_through(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("good") + "\n\n{ torn\n")
    assert read_lines(log, Filter()) == [_record("good"), "", "{ torn"]
    assert read_lines(log, Filter(events_only=True)) == []


def test_read_lines_tail_zero_is_no_lines(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("a") + "\n" + _record("b") + "\n")
    assert read_lines(log, Filter(), tail=0) == []


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
    log = tmp_path / "shipit.log"
    log.write_text(_record("pre", pr=231, event="pr.ready") + "\n")

    whole = _record("torn but tagged", pr=231, event="pr.ready")
    cut = len(whole) // 2
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
    assert whole in got


def test_follow_reassembles_a_torn_line_present_at_start(tmp_path):
    log = tmp_path / "shipit.log"
    whole = _record("torn at start", pr=231, event="pr.ready")
    cut = len(whole) // 2
    log.write_text(whole[:cut])
    remainder = [whole[cut:] + "\n"]

    def fake_sleep(_interval: float) -> None:
        if remainder:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(remainder.pop(0))
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=-1, sleep=fake_sleep))
    assert got == [whole]


def test_follow_reopens_after_in_place_truncation(tmp_path):
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


def test_follow_drops_a_stale_torn_fragment_on_rotation(tmp_path):
    log = tmp_path / "shipit.log"
    log.write_text(_record("old-complete") + "\n")

    def append_fragment() -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("{ torn-in-old-file")

    def rotate() -> None:
        log.rename(log.with_name("shipit.log.1"))
        log.write_text(_record("first-in-new-file") + "\n")

    steps = [append_fragment, rotate]

    def fake_sleep(_interval: float) -> None:
        if steps:
            steps.pop(0)()
        else:
            raise KeyboardInterrupt

    got = _drain(follow_lines(log, Filter(), tail=0, sleep=fake_sleep))
    assert got == [_record("first-in-new-file")]


def test_follow_yields_stored_lines_verbatim_including_malformed(tmp_path):
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
