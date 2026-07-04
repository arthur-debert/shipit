"""Unit tests for `shipit logs` — the reader half of WS01's file sink (LOG01-WS04).

Asserts external behavior in shipit's style. Every boundary is injected: the
platformdirs base via ``base_dir``, the ``gh`` repo resolution via
``current_repo``, the follow-loop poll via ``sleep`` — so nothing reads a real
``$HOME`` or shells out to ``gh``. The log content under test is JSONL — the
only format the verb reads (hard cutover, ADR-0029).
"""

from __future__ import annotations

import json
from pathlib import Path

from shipit import cli
from shipit.verbs import logs
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug


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


# --------------------------------------------------------------------------
# --path — the resolved absolute log file path (defaulting to the cwd repo)
# --------------------------------------------------------------------------


def test_path_prints_absolute_per_repo_path_for_cwd_repo(tmp_path, capsys):
    rc = logs.run(
        path_only=True,
        base_dir=tmp_path,
        current_repo=lambda: "arthur-debert/shipit",
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "arthur-debert" / "shipit" / "shipit.log")


def test_explicit_repo_overrides_cwd_default(tmp_path, capsys):
    called = []

    def boom() -> str:
        called.append(True)
        return "should/not-be-used"

    rc = logs.run(
        "octocat/hello-world",
        path_only=True,
        base_dir=tmp_path,
        current_repo=boom,
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "octocat" / "hello-world" / "shipit.log")
    # The cwd boundary is never consulted when an explicit slug is given.
    assert called == []


def test_path_succeeds_even_when_log_absent(tmp_path, capsys):
    # --path locates; it never depends on the file existing yet.
    rc = logs.run("o/r", path_only=True, base_dir=tmp_path, current_repo=lambda: "o/r")
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/o/r/shipit.log")


def test_path_is_a_pure_locator_ignoring_reader_only_flags(tmp_path, capsys):
    # --path prints the resolved path and exits before any reader-only
    # validation, so combining it with a flag that would fail WHEN READING —
    # `--session current` outside a session — still returns the path, not the
    # usage error the reader would raise.
    rc = logs.run(
        "o/r",
        path_only=True,
        session="current",
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        current_session=lambda: None,
    )
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/o/r/shipit.log")


# --------------------------------------------------------------------------
# Default view — path + the last N records, rendered for humans
# --------------------------------------------------------------------------


def test_default_prints_path_then_last_n_records(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(_record(f"msg{i}") for i in range(10)) + "\n")

    rc = logs.run("o/r", tail=3, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(log)
    assert [line.split()[-1] for line in out[1:]] == ["msg7", "msg8", "msg9"]


def test_render_shows_ts_level_logger_msg_and_domain_keys(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("tree created", pr=231, session="work") + "\n")

    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # One record → one rendered line: the contract fields up front, the bound
    # domain keys trailing sorted — no JSON braces leak into the human view.
    assert (
        out[1]
        == "2026-07-02T12:00:00Z INFO shipit.tree: tree created [pr=231 session=work]"
    )


def test_render_puts_exception_on_following_lines(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        _record("boom", exception="Traceback (most recent call last):\n  KaboomError")
        + "\n"
    )

    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].endswith("boom")
    assert out[2] == "Traceback (most recent call last):"
    assert out[3] == "  KaboomError"
    # The flattened traceback renders as lines, not as a trailing key=value.
    assert "exception=" not in "\n".join(out)


def test_malformed_line_is_skipped_with_note_never_a_crash(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        _record("good before")
        + '\n{ torn json record\n"a bare string"\n'
        + _record("good after")
        + "\n"
    )

    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out.splitlines()
    # Both well-formed neighbors render; the torn line and the non-object JSON
    # are absent from stdout...
    assert out[1].endswith("good before")
    assert out[2].endswith("good after")
    assert len(out) == 3
    # ...and each earns a stderr note quoting the offender.
    assert captured.err.count("skipped malformed line") == 2
    assert "torn json" in captured.err


def test_malformed_line_snippet_is_redacted_before_stderr(tmp_path, capsys):
    # The skip note is the one path that echoes raw file content the writer's
    # redaction pipeline never finished with (a torn write, a pre-cutover
    # freeform line) — a secret in it must be masked, not sprayed onto stderr.
    token = "ghp_" + "a1B2c3D4e5" * 4
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(f"{{ torn write carrying {token}\n")

    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    captured = capsys.readouterr()
    assert "skipped malformed line" in captured.err
    assert token not in captured.err
    assert "***" in captured.err


def test_emit_flushes_stdout_for_live_piping(monkeypatch):
    # `logs -f --raw | jq .` attaches stdout to a pipe, which Python
    # block-buffers: without an explicit flush per record, a followed stream
    # sits invisible in the buffer instead of arriving live.
    import io
    import sys as _sys

    class _Recorder(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:  # noqa: A003 - mirrors TextIOBase
            self.flushes += 1
            super().flush()

    fake_out = _Recorder()
    monkeypatch.setattr(_sys, "stdout", fake_out)
    logs._emit(_record("live"), raw=True)
    logs._emit(_record("rendered"), raw=False)
    assert fake_out.flushes >= 2


def test_blank_lines_are_dropped_silently(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("only") + "\n\n\n")

    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    captured = capsys.readouterr()
    # A blank is padding, not a record: no rendered line, no malformed note.
    assert len(captured.out.splitlines()) == 2
    assert "malformed" not in captured.err


def test_tail_zero_prints_path_only_not_whole_file(tmp_path, capsys):
    # Regression: `lines[-0:]` is the whole file — `-n 0` must print NO log lines.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("a") + "\n" + _record("b") + "\n")

    rc = logs.run("o/r", tail=0, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [str(log)]


# --------------------------------------------------------------------------
# --raw — unmodified JSONL passthrough for jq/tooling
# --------------------------------------------------------------------------


def test_raw_emits_unmodified_jsonl_and_no_path_header(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    lines = [_record("one", pr=231), _record("two")]
    log.write_text("\n".join(lines) + "\n")

    rc = logs.run("o/r", raw=True, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # Byte-for-byte the stored lines, nothing else: stdout is pure JSONL, so
    # `shipit logs --raw | jq .` just works.
    assert out == lines
    assert all(json.loads(line) for line in out)


def test_raw_passes_malformed_lines_through_untouched(tmp_path, capsys):
    # Raw is a passthrough: it parses nothing, so even a torn line reaches the
    # downstream tool exactly as stored (jq's error is the right error).
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text("{ torn\n")

    rc = logs.run("o/r", raw=True, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["{ torn"]
    assert "malformed" not in captured.err


def test_raw_follow_streams_pure_jsonl(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("old") + "\n")

    appended = [_record("streamed", pr=7)]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        raw=True,
        tail=-1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # No path header, and every emitted line — pre-existing and streamed — is
    # the stored JSONL verbatim.
    assert str(log) not in out
    assert [json.loads(line)["msg"] for line in out] == ["old", "streamed"]


# --------------------------------------------------------------------------
# --events / --pr — the LOG04 record filters (AND, before the tail count)
# --------------------------------------------------------------------------


def _fixture_log(tmp_path) -> "Path":
    """A fixture JSONL log mixing plain records, event records, and PRs."""
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                _record("snapshot gathered", pr=231),
                _record(
                    "review request from copilot attached on pr#231 (verified)",
                    pr=231,
                    event="review.requested",
                    reviewer="copilot",
                ),
                _record("tree created", tree="/trees/x"),
                _record(
                    "review in flight from codex on pr#7 (detached)",
                    pr=7,
                    event="review.requested",
                    reviewer="codex",
                ),
                _record("plain mechanics", pr=7),
            ]
        )
        + "\n"
    )
    return log


def test_events_keeps_only_event_tagged_records(tmp_path, capsys):
    _fixture_log(tmp_path)
    rc = logs.run(
        "o/r", events_only=True, base_dir=tmp_path, current_repo=lambda: "x/y"
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # Path header + exactly the two event records, in file order.
    assert len(out) == 3
    assert "review request from copilot" in out[1]
    assert "review in flight from codex" in out[2]
    assert "snapshot gathered" not in "\n".join(out)


def test_pr_filter_keeps_only_that_prs_records(tmp_path, capsys):
    _fixture_log(tmp_path)
    rc = logs.run("o/r", pr=7, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3
    assert "review in flight from codex" in out[1]
    assert "plain mechanics" in out[2]
    assert "231" not in "\n".join(out[1:])


def test_events_and_pr_compose_as_and(tmp_path, capsys):
    # The demo read: `shipit logs --events --pr 231` → that PR's milestones.
    _fixture_log(tmp_path)
    rc = logs.run(
        "o/r", events_only=True, pr=231, base_dir=tmp_path, current_repo=lambda: "x/y"
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert "review request from copilot" in out[1]
    assert "[event=review.requested pr=231 reviewer=copilot]" in out[1]


def test_filters_apply_before_the_tail_count(tmp_path, capsys):
    # -n 1 --pr 231 means "the last record ABOUT pr 231", not "the last record,
    # if it happens to match".
    _fixture_log(tmp_path)
    rc = logs.run("o/r", pr=231, tail=1, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert "review request from copilot" in out[1]


def test_filters_compose_with_raw(tmp_path, capsys):
    # `shipit logs --events --raw | jq .` — stdout is exactly the matching
    # stored lines, nothing else.
    _fixture_log(tmp_path)
    rc = logs.run(
        "o/r", events_only=True, raw=True, base_dir=tmp_path, current_repo=lambda: "x/y"
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert [json.loads(line)["event"] for line in out] == [
        "review.requested",
        "review.requested",
    ]


def test_filters_compose_with_follow(tmp_path, capsys):
    # A followed stream applies the same filter to appended lines: only the
    # matching record streams through, live.
    log = _fixture_log(tmp_path)
    appended = [
        _record("noise while following", pr=231),
        _record("pr#231 flipped ready", pr=231, event="pr.ready"),
    ]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        events_only=True,
        pr=231,
        tail=-1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "review request from copilot" in out  # the pre-follow matching tail
    assert "flipped ready" in out  # the appended matching record
    assert "noise while following" not in out
    assert "codex" not in out  # pr 7's event fails the AND


def test_follow_reassembles_a_torn_write_before_filtering(tmp_path, capsys):
    # A concurrent write can be read mid-line, so readline() returns a fragment
    # with no trailing newline. A field filter parses to select, so a naive read
    # would drop the fragment AND its remainder (neither half is valid JSON) —
    # losing the record permanently. The follow loop buffers until the newline
    # lands, then judges the whole line. Regression for the torn-read drop (agy).
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
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

    rc = logs.run(
        "o/r",
        follow=True,
        events_only=True,
        pr=231,
        tail=-1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The record survives being split across two reads under an active filter.
    assert "torn but tagged" in out


def test_follow_reassembles_a_torn_line_present_at_start(tmp_path, capsys):
    # agy [ERROR]: the initial tail read must be buffer-aware too, not just the
    # append loop. If the file ALREADY ends in a torn write (no newline) when
    # follow opens, a naive `read().splitlines()` emits the head now, then the
    # append loop reads the remainder and emits it — one record split into two.
    # The initial read seeds the same `pending` buffer, so the halves reunite.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    whole = _record("torn at start", pr=231, event="pr.ready")
    cut = len(whole) // 2
    # File opens with only the first half on disk (no trailing newline).
    log.write_text(whole[:cut])
    remainder = whole[cut:] + "\n"

    def fake_sleep(_interval: float) -> None:
        nonlocal remainder
        if remainder:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(remainder)
            remainder = ""
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        raw=True,
        tail=-1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    # Raw passthrough emits the record exactly once, whole — not head then tail.
    assert capsys.readouterr().out.splitlines() == [whole]


def test_active_filter_drops_malformed_lines_silently(tmp_path, capsys):
    # A field filter cannot be evaluated on a torn line — under an active
    # filter it is dropped in BOTH modes (no false positive, no stderr note),
    # while the no-filter contracts (raw passthrough, rendered skip-note) are
    # pinned by the earlier tests.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text("{ torn\n" + _record("tagged", event="pr.ready") + "\n")

    rc = logs.run(
        "o/r", events_only=True, raw=True, base_dir=tmp_path, current_repo=lambda: "x/y"
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [_record("tagged", event="pr.ready")]
    assert captured.err == ""


def test_cli_logs_help_shows_filter_flags(capsys):
    rc = cli.main(["logs", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--events" in out
    assert "--pr" in out


# --------------------------------------------------------------------------
# Domain-key filters (LOG04-WS04) — session/epic/ws/agent/role, AND-composed
# --------------------------------------------------------------------------


def _domain_fixture_log(tmp_path) -> "Path":
    """A fixture JSONL log spanning two sessions, two epics, three Work Streams."""
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                _record("session started", session="sess-1", event="session.started"),
                _record(
                    "implementer spawned",
                    session="sess-1",
                    epic="LOG04",
                    ws=1,
                    agent="run-a1",
                    role="implementer",
                    event="agent.spawned",
                ),
                _record("mechanics inside WS1", session="sess-1", epic="LOG04", ws=1),
                _record(
                    "review requested on pr#401",
                    session="sess-1",
                    epic="LOG04",
                    ws=2,
                    agent="run-b2",
                    role="shepherd",
                    pr=401,
                    event="review.requested",
                ),
                _record(
                    "other session's spawn",
                    session="sess-2",
                    epic="RVW01",
                    ws=1,
                    agent="run-c3",
                    role="implementer",
                    event="agent.spawned",
                ),
            ]
        )
        + "\n"
    )
    return log


def test_epic_filter_keeps_only_that_epics_records(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run("o/r", epic="LOG04", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out
    assert "implementer spawned" in out
    assert "mechanics inside WS1" in out
    assert "review requested" in out
    assert "other session's spawn" not in out
    assert "session started" not in out  # no epic bound → cannot match


def test_domain_filters_compose_as_and(tmp_path, capsys):
    # The demo slice: `shipit logs --epic LOG04 --ws 1 --events` — one Work
    # Stream's milestones, nothing else.
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r",
        epic="LOG04",
        ws=1,
        events_only=True,
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2  # path header + the one matching record
    assert "implementer spawned" in out[1]


def test_agent_and_role_filters_select_on_their_keys(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run("o/r", agent="run-b2", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    assert "review requested" in capsys.readouterr().out

    rc = logs.run(
        "o/r", role="implementer", base_dir=tmp_path, current_repo=lambda: "x/y"
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "implementer spawned" in out
    assert "other session's spawn" in out
    assert "review requested" not in out


def test_session_filter_slices_one_session(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r", session="sess-2", base_dir=tmp_path, current_repo=lambda: "x/y"
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "other session's spawn" in out
    assert "sess-1" not in out


def test_ws_normalizes_from_all_three_input_forms(tmp_path, capsys):
    # `1`, `01`, and `WS01` name the same Work Stream: the display form is
    # never data (ADR-0032), so all three select the SAME int-typed records.
    _domain_fixture_log(tmp_path)
    outputs = []
    for form in ("1", "01", "WS01"):
        rc = logs.run(
            "o/r", epic="LOG04", ws=form, base_dir=tmp_path, current_repo=lambda: "x/y"
        )
        assert rc == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1] == outputs[2]
    assert "mechanics inside WS1" in outputs[0]
    assert "review requested" not in outputs[0]  # ws=2 fails the AND


def test_bad_ws_form_is_a_usage_error(tmp_path, capsys):
    for bad in ("WSx", "zero", "WS00", "0"):
        rc = logs.run("o/r", ws=bad, base_dir=tmp_path, current_repo=lambda: "x/y")
        assert rc == 2
        assert "--ws" in capsys.readouterr().err


def test_session_current_resolves_via_the_injected_boundary(tmp_path, capsys):
    # `--session current` means "the session THIS process is in": the sentinel
    # resolves through the one resolver (shipit.session.current — injected
    # here) and then filters like any explicit id.
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r",
        session="current",
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        current_session=lambda: "sess-1",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "session started" in out
    assert "other session's spawn" not in out


def test_session_current_outside_a_session_is_a_usage_error(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r",
        session="current",
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        current_session=lambda: None,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--session current" in err
    assert "SHIPIT_LOG_CTX_SESSION" in err


def test_domain_filters_behave_identically_under_raw(tmp_path, capsys):
    # Uniformity: --raw changes the output MODE, never the selection.
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r",
        epic="LOG04",
        ws="WS01",
        raw=True,
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["msg"] for line in out] == [
        "implementer spawned",
        "mechanics inside WS1",
    ]


def test_domain_filters_behave_identically_under_follow(tmp_path, capsys):
    # A followed stream applies the same domain-key selection to appended lines.
    log = _domain_fixture_log(tmp_path)
    appended = [
        _record("noise from another epic", session="sess-2", epic="RVW01", ws=1),
        _record("late WS1 record", session="sess-1", epic="LOG04", ws=1),
    ]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        epic="LOG04",
        ws="01",
        tail=-1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "implementer spawned" in out  # the pre-follow matching tail
    assert "late WS1 record" in out  # the appended matching record
    assert "noise from another epic" not in out


# --------------------------------------------------------------------------
# --flow — the rendered session story (implies --events)
# --------------------------------------------------------------------------


def _flow_now():
    from datetime import datetime, timezone

    # 1h34m after the fixture records' shared ts (2026-07-02T12:00:00Z).
    return datetime(2026, 7, 2, 13, 34, 0, tzinfo=timezone.utc)


def test_flow_implies_events_and_renders_the_story(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r",
        flow=True,
        session="sess-1",
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        now=_flow_now,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Event records only — --flow implied --events without it being asked for.
    assert "mechanics inside WS1" not in out
    # The story lines: EPIC-WSnn prefixes minted from the int domain keys,
    # friendly relative times, no raw JSON and no path header.
    assert "LOG04-WS01: implementer spawned" in out
    assert "LOG04-WS02: review requested on pr#401" in out
    assert "1h34m ago" in out
    assert str(tmp_path) not in out
    assert "{" not in out


def test_flow_infers_the_theme_header_from_epics(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r", flow=True, base_dir=tmp_path, current_repo=lambda: "x/y", now=_flow_now
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "session on LOG04, RVW01"


def test_flow_opens_with_the_session_intent_when_present(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        _record("tuning the review loop", session="s1", event="session.intent")
        + "\n"
        + _record("spawned", session="s1", epic="LOG04", ws=4, event="agent.spawned")
        + "\n"
    )
    rc = logs.run(
        "o/r", flow=True, base_dir=tmp_path, current_repo=lambda: "x/y", now=_flow_now
    )
    assert rc == 0
    assert capsys.readouterr().out.splitlines()[0] == "tuning the review loop"


def test_flow_hides_agent_ids_until_asked(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        "o/r", flow=True, base_dir=tmp_path, current_repo=lambda: "x/y", now=_flow_now
    )
    assert rc == 0
    assert "run-a1" not in capsys.readouterr().out

    rc = logs.run(
        "o/r",
        flow=True,
        show_agents=True,
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        now=_flow_now,
    )
    assert rc == 0
    assert "[agent=run-a1]" in capsys.readouterr().out


def test_flow_skips_malformed_records_never_crashes(tmp_path, capsys):
    # The reader resilience contract continues into the story view: a torn
    # line has no fields to select on, so it drops silently (the flow filter
    # is always active) and the view renders the survivors.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "{ torn mid-write\n"
        + '"a bare string"\n'
        + _record("survivor", epic="LOG04", ws=4, event="pr.ready")
        + "\n"
    )
    rc = logs.run(
        "o/r", flow=True, base_dir=tmp_path, current_repo=lambda: "x/y", now=_flow_now
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "LOG04-WS04: survivor" in captured.out
    assert "torn" not in captured.out
    assert captured.err == ""


def test_flow_applies_the_tail_count_to_the_filtered_records(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            _record(f"milestone {i}", epic="LOG04", ws=4, event="pr.ready")
            for i in range(5)
        )
        + "\n"
    )
    rc = logs.run(
        "o/r",
        flow=True,
        tail=2,
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        now=_flow_now,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "milestone 4" in out
    assert "milestone 3" in out
    assert "milestone 2" not in out


def test_flow_header_survives_a_tail_that_cuts_the_intent(tmp_path, capsys):
    # The intent event is the OLDEST record; a tail that drops it from the body
    # must still open the story on it — the header themes the whole session,
    # only the body lines are tailed.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        _record("tuning the review loop", session="s1", event="session.intent")
        + "\n"
        + "\n".join(
            _record(
                f"milestone {i}", session="s1", epic="LOG04", ws=4, event="pr.ready"
            )
            for i in range(3)
        )
        + "\n"
    )
    rc = logs.run(
        "o/r",
        flow=True,
        tail=2,
        base_dir=tmp_path,
        current_repo=lambda: "x/y",
        now=_flow_now,
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "tuning the review loop"  # header from the full session
    body = "\n".join(out[1:])
    assert "milestone 2" in body
    assert "milestone 1" in body
    assert "milestone 0" not in body  # tailed out of the body
    assert "tuning the review loop" not in body  # intent lives only in the header


def test_flow_refuses_raw_and_follow(tmp_path, capsys):
    for kwargs in ({"raw": True}, {"follow": True}):
        rc = logs.run(
            "o/r", flow=True, base_dir=tmp_path, current_repo=lambda: "x/y", **kwargs
        )
        assert rc == 2
        assert "--flow" in capsys.readouterr().err


def test_cli_logs_help_shows_domain_key_and_flow_flags(capsys):
    rc = cli.main(["logs", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    for flag in ("--session", "--epic", "--ws", "--agent", "--role", "--flow"):
        assert flag in out
    assert "--agent-ids" in out


# --------------------------------------------------------------------------
# -f/--follow — stream appended lines live
# --------------------------------------------------------------------------


def test_follow_streams_appended_records_rendered(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("old1") + "\n" + _record("old2") + "\n")

    appended = [_record("new line A", pr=231), _record("new line B")]

    def fake_sleep(_interval: float) -> None:
        # Drive the poll loop: append a line per tick, then end like Ctrl-C.
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        tail=1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The appended records streamed through — rendered, not raw JSON.
    assert "new line A [pr=231]" in out
    assert "new line B" in out
    assert "{" not in out.replace(str(log), "")
    # The pre-follow tail honored N=1 (only the last existing record, not old1).
    assert "old2" in out
    assert "old1" not in out


def test_follow_skips_malformed_lines_with_note(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("pre") + "\n")

    appended = ["{ torn mid-write", _record("post")]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        tail=-1,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    captured = capsys.readouterr()
    # The stream survives the torn line: the record after it still renders...
    assert "post" in captured.out
    assert "torn mid-write" not in captured.out
    # ...and the skip is noted, not crashed.
    assert "skipped malformed line" in captured.err


def test_follow_reopens_after_rotation(tmp_path, capsys):
    # The writer is a RotatingFileHandler: the active shipit.log can be rolled
    # over mid-follow. A follow that holds one handle would then track the stale
    # renamed file and go silent — so it must reopen when the file shrinks.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("before-rotation-with-some-padding-to-be-longer") + "\n")

    steps = [
        # Simulate the rollover: replace shipit.log with a fresh, SMALLER file.
        lambda: log.write_text(_record("after") + "\n"),
    ]

    def fake_sleep(_interval: float) -> None:
        if steps:
            steps.pop(0)()
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        "o/r",
        follow=True,
        tail=0,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    # The line written into the post-rotation file streamed through, proving the
    # follow loop reopened rather than clinging to the original handle.
    assert "after" in capsys.readouterr().out


def test_follow_reopens_after_rename_rotation_even_when_new_file_is_larger(
    tmp_path, capsys
):
    # RotatingFileHandler rotates by RENAME + fresh create. A size-only check
    # races: a busy fresh file can outgrow the old read offset between polls,
    # so the shrink is never observed and the follow clings to the renamed
    # handle forever. Identity (inode) must be what detects the swap — here the
    # replacement file is deliberately LARGER than the followed offset.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
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

    rc = logs.run(
        "o/r",
        follow=True,
        tail=0,
        base_dir=tmp_path,
        current_repo=lambda: "o/r",
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "fresh-0" in out
    assert "fresh-19" in out


# --------------------------------------------------------------------------
# Missing file + bad slug — graceful, never a traceback
# --------------------------------------------------------------------------


def test_missing_log_file_is_graceful(tmp_path, capsys):
    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "o/r")
    assert rc == 1
    err = capsys.readouterr().err
    assert "no log yet" in err
    assert "o/r" in err


def test_bad_repo_slug_is_usage_error(tmp_path, capsys):
    rc = logs.run("not-a-slug", path_only=True, base_dir=tmp_path)
    assert rc == 2
    assert "owner/name" in capsys.readouterr().err


def test_gh_error_resolving_repo_is_graceful(tmp_path, capsys):
    # No explicit repo and the cwd resolution shells out to gh and fails (not a
    # checkout / gh missing). That must be a clean usage error, never a traceback.
    def boom() -> str:
        raise ExecError(["gh"], rc=1, stderr="not a git repository")

    rc = logs.run(base_dir=tmp_path, current_repo=boom)
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not determine the current repo" in err
    assert "owner/repo" in err


def test_default_repo_resolves_locally_not_via_gh(tmp_path, capsys, monkeypatch):
    # Reader/writer agreement (COR02): with no explicit repo, the DEFAULT resolves
    # the cwd repo the SAME way the WRITER namespaces its log — LOCALLY off the
    # origin remote (identity.resolve_repo) — NOT gh.current_repo. So a log written
    # in a checkout where `gh` is unavailable is still found. gh.current_repo is
    # wired to explode to prove the reader never touches it.
    from shipit import gh, identity

    def gh_boom(*args, **kwargs) -> str:
        raise ExecError(["gh"], rc=1, stderr="gh unavailable")

    monkeypatch.setattr(gh, "current_repo", gh_boom)
    monkeypatch.setattr(identity, "resolve_repo", lambda *a, **k: repo_from_slug("o/r"))

    # The writer left a JSONL log under the locally-resolved repo's dir.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text(_record("tree opened", tree="w1") + "\n", encoding="utf-8")

    rc = logs.run(base_dir=tmp_path)
    assert rc == 0
    assert "tree opened" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Single source of truth — path comes from logsetup, never recomputed here
# --------------------------------------------------------------------------


def test_path_comes_from_logsetup_log_file_path(tmp_path, monkeypatch, capsys):
    sentinel = tmp_path / "sentinel" / "shipit.log"
    seen: dict[str, object] = {}

    def fake_log_file_path(repo, *, base_dir=None):
        seen["repo"] = repo
        seen["base_dir"] = base_dir
        return sentinel

    monkeypatch.setattr(logs.logsetup, "log_file_path", fake_log_file_path)
    rc = logs.run("o/r", path_only=True, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(sentinel)
    # The reader hands the canonically-parsed Repo + injected base straight to
    # WS01's accessor.
    assert seen["repo"] == repo_from_slug("o/r")
    assert seen["base_dir"] == tmp_path


def test_reader_does_not_recompute_path_or_add_env_override():
    # The reader consumes logsetup's resolution; it must not sniff platformdirs,
    # the platform, or a bespoke log-dir env var of its own.
    src = Path(logs.__file__).read_text()
    assert "platformdirs" not in src
    assert "user_log_dir" not in src
    assert "SHIPIT_LOG_DIR" not in src
    assert "sys.platform" not in src
    # ...and it DOES call WS01's single-source-of-truth accessor.
    assert "log_file_path" in src


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_cli_help_lists_logs(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "logs" in capsys.readouterr().out


def test_cli_logs_help_shows_flags(capsys):
    rc = cli.main(["logs", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--path" in out
    assert "--follow" in out
    assert "--raw" in out


def test_writer_to_reader_round_trip(tmp_path, capsys):
    # End-to-end across the WS01 seam: a record written through the real file
    # sink (JSONL formatter, bound domain key, rotation handler and all)
    # renders legibly here.
    import logging

    import structlog

    from shipit import logsetup

    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
    logger = logging.getLogger("shipit.roundtrip")
    logger.setLevel(logging.DEBUG)
    # Process-lifetime logger: keep the record off any handlers other tests may
    # have hung on the parent `shipit` logger — this test owns its one handler.
    logger.propagate = False
    logger.addHandler(handler)
    structlog.contextvars.bind_contextvars(pr=231)
    try:
        logger.info("round trip")
    finally:
        structlog.contextvars.clear_contextvars()
        logger.removeHandler(handler)
        handler.close()

    rc = logs.run("o/r", base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    captured = capsys.readouterr()
    rendered = captured.out.splitlines()[1]
    assert "INFO shipit.roundtrip: round trip" in rendered
    assert "[pr=231]" in rendered
    assert "malformed" not in captured.err


def test_cli_logs_path_smoke(capsys):
    # Explicit slug (no gh) + --path (no FS write): a pure path computation.
    rc = cli.main(["logs", "--path", "octocat/hello-world"])
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/octocat/hello-world/shipit.log")
