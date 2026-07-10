"""Unit tests for `shipit logs` — the reader VERB on the ADR-0030 contract.

The verb layer only (CLI02-WS05): click glue, the frozen-query minting at
parse, the pure per-record renderers, and ``run()``'s rendering over the
engine's iterators. The engine itself — filters, tail, follow's
rotation/torn-write behavior — is covered in :mod:`tests.test_logread`
against the domain package, without a terminal; here capsys asserts what the
TERMINAL sees. Boundaries stay injected: the log base via ``base_dir``, the
follow poll via ``sleep``, the flow clock via ``now``, the session resolver
via ``current_session`` — nothing reads a real ``$HOME`` or a live session.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import click
import pytest

from shipit import cli, logread
from shipit.identity import repo_from_slug
from shipit.verbs import logs

#: The repo whose log the tests read; explicit and typed — the ambient
#: default is the shared parameter library's, covered by test_cli_seam.
REPO = repo_from_slug("o/r")


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


def _write_log(tmp_path, text: str) -> Path:
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text)
    return log


# --------------------------------------------------------------------------
# --path — the resolved absolute log file path
# --------------------------------------------------------------------------


def test_path_prints_absolute_per_repo_path(tmp_path, capsys):
    rc = logs.run(REPO, path_only=True, base_dir=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "o" / "r" / "shipit.log")


def test_path_succeeds_even_when_log_absent(tmp_path, capsys):
    # --path locates; it never depends on the file existing yet.
    rc = logs.run(REPO, path_only=True, base_dir=tmp_path)
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/o/r/shipit.log")


def test_path_is_a_pure_locator_ignoring_reader_only_flags(capsys, monkeypatch):
    # --path prints the resolved path and exits before any reader-only
    # validation — the CLI callback does not even BUILD the query — so
    # combining it with a flag that would fail WHEN READING (`--session
    # current` outside a session) still returns the path, not a usage error.
    monkeypatch.delenv("SHIPIT_LOG_CTX_SESSION", raising=False)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", "/nonexistent-trees-root")
    rc = cli.main(["logs", "--path", "--session", "current", "o/r"])
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/o/r/shipit.log")


# --------------------------------------------------------------------------
# The frozen query — minted at parse (usage tier, exit 2)
# --------------------------------------------------------------------------


def test_build_query_resolves_the_current_sentinel_via_the_injected_boundary():
    query = logs.build_query(session="current", current_session=lambda: "sess-1")
    assert query.record_filter.fields["session"] == "sess-1"


def test_session_current_outside_a_session_is_a_usage_error():
    with pytest.raises(click.UsageError) as exc:
        logs.build_query(session="current", current_session=lambda: None)
    message = str(exc.value)
    assert "--session current" in message
    assert "SHIPIT_LOG_CTX_SESSION" in message


def test_bad_ws_form_is_a_usage_error():
    for bad in ("WSx", "zero", "WS00", "0"):
        with pytest.raises(click.UsageError) as exc:
            logs.build_query(ws=bad)
        assert "--ws" in str(exc.value)


def test_flow_refuses_raw_and_follow():
    for kwargs in ({"raw": True}, {"follow": True}):
        with pytest.raises(click.UsageError) as exc:
            logs.build_query(flow=True, **kwargs)
        assert "--flow" in str(exc.value)


def test_cli_bad_ws_is_exit_2(capsys):
    # The whole usage tier through the real CLI entry: parse-time failure,
    # exit 2, the message on stderr — never verb-body code.
    rc = cli.main(["logs", "--ws", "WSx", "o/r"])
    assert rc == 2
    assert "--ws" in capsys.readouterr().err


def test_cli_flow_raw_contradiction_is_exit_2(capsys):
    rc = cli.main(["logs", "--flow", "--raw", "o/r"])
    assert rc == 2
    assert "--flow" in capsys.readouterr().err


def test_cli_bad_repo_slug_is_exit_2(capsys):
    # The slug is minted to a Repo at parse by the shared parameter library.
    rc = cli.main(["logs", "--path", "not-a-slug"])
    assert rc == 2
    assert "owner/name" in capsys.readouterr().err


def test_outside_a_checkout_without_a_repo_is_the_uniform_refusal(capsys):
    # No explicit repo and no ambient checkout: the ONE uniform refusal
    # through the error shell — a runtime outcome (exit 1, ADR-0030's two-tier
    # contract; deliberately no longer the pre-promotion exit 2).
    rc = logs.run(None, path_only=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "not inside a repository checkout" in err


# --------------------------------------------------------------------------
# Default view — path + the last N records, rendered for humans
# --------------------------------------------------------------------------


def test_default_prints_path_then_last_n_records(tmp_path, capsys):
    log = _write_log(tmp_path, "\n".join(_record(f"msg{i}") for i in range(10)) + "\n")

    rc = logs.run(REPO, query=logread.build_query(tail=3), base_dir=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(log)
    assert [line.split()[-1] for line in out[1:]] == ["msg7", "msg8", "msg9"]


def test_render_shows_ts_level_logger_msg_and_domain_keys(tmp_path, capsys):
    _write_log(tmp_path, _record("tree created", pr=231, session="work") + "\n")

    rc = logs.run(REPO, base_dir=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # One record → one rendered line: the contract fields up front, the bound
    # domain keys trailing sorted — no JSON braces leak into the human view.
    assert (
        out[1]
        == "2026-07-02T12:00:00Z INFO shipit.tree: tree created [pr=231 session=work]"
    )


def test_render_record_is_a_pure_formatter():
    # The render seam: string in, string out — no terminal in the loop.
    rendered = logs.render_record(_record("tree created", pr=231))
    assert rendered == "2026-07-02T12:00:00Z INFO shipit.tree: tree created [pr=231]"
    assert logs.render_record("{ torn") is None
    assert logs.render_record('"a bare string"') is None


def test_render_puts_exception_on_following_lines(tmp_path, capsys):
    _write_log(
        tmp_path,
        _record("boom", exception="Traceback (most recent call last):\n  KaboomError")
        + "\n",
    )

    rc = logs.run(REPO, base_dir=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].endswith("boom")
    assert out[2] == "Traceback (most recent call last):"
    assert out[3] == "  KaboomError"
    # The flattened traceback renders as lines, not as a trailing key=value.
    assert "exception=" not in "\n".join(out)


def test_malformed_line_is_skipped_with_note_never_a_crash(tmp_path, capsys):
    _write_log(
        tmp_path,
        _record("good before")
        + '\n{ torn json record\n"a bare string"\n'
        + _record("good after")
        + "\n",
    )

    rc = logs.run(REPO, base_dir=tmp_path)
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


def test_malformed_note_is_redacted_before_truncation():
    # The skip note is the one path that echoes raw file content the writer's
    # redaction pipeline never finished with (a torn write, a pre-cutover
    # freeform line) — a secret in it must be masked, not sprayed onto stderr.
    token = "ghp_" + "a1B2c3D4e5" * 4
    note = logs.malformed_note(f"{{ torn write carrying {token}")
    assert "skipped malformed line" in note
    assert token not in note
    assert "***" in note


def test_emit_line_flushes_stdout_for_live_piping(monkeypatch):
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
    logs._emit_line(_record("live"), raw=True)
    logs._emit_line(_record("rendered"), raw=False)
    assert fake_out.flushes >= 2


def test_blank_lines_are_dropped_silently(tmp_path, capsys):
    _write_log(tmp_path, _record("only") + "\n\n\n")

    rc = logs.run(REPO, base_dir=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    # A blank is padding, not a record: no rendered line, no malformed note.
    assert len(captured.out.splitlines()) == 2
    assert "malformed" not in captured.err


def test_tail_zero_prints_path_only_not_whole_file(tmp_path, capsys):
    # Regression: `lines[-0:]` is the whole file — `-n 0` must print NO log lines.
    log = _write_log(tmp_path, _record("a") + "\n" + _record("b") + "\n")

    rc = logs.run(REPO, query=logread.build_query(tail=0), base_dir=tmp_path)
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == [str(log)]


# --------------------------------------------------------------------------
# --raw — unmodified JSONL passthrough for jq/tooling
# --------------------------------------------------------------------------


def test_raw_emits_unmodified_jsonl_and_no_path_header(tmp_path, capsys):
    lines = [_record("one", pr=231), _record("two")]
    _write_log(tmp_path, "\n".join(lines) + "\n")

    rc = logs.run(REPO, query=logread.build_query(raw=True), base_dir=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # Byte-for-byte the stored lines, nothing else: stdout is pure JSONL, so
    # `shipit logs --raw | jq .` just works.
    assert out == lines
    assert all(json.loads(line) for line in out)


def test_raw_passes_malformed_lines_through_untouched(tmp_path, capsys):
    # Raw is a passthrough: it parses nothing, so even a torn line reaches the
    # downstream tool exactly as stored (jq's error is the right error).
    _write_log(tmp_path, "{ torn\n")

    rc = logs.run(REPO, query=logread.build_query(raw=True), base_dir=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["{ torn"]
    assert "malformed" not in captured.err


def test_active_filter_drops_malformed_lines_silently(tmp_path, capsys):
    # A field filter cannot be evaluated on a torn line — under an active
    # filter it is dropped in BOTH modes (no false positive, no stderr note);
    # the no-filter contracts (raw passthrough, rendered skip-note) are pinned
    # by the tests above.
    _write_log(tmp_path, "{ torn\n" + _record("tagged", event="pr.ready") + "\n")

    rc = logs.run(
        REPO, query=logread.build_query(events_only=True, raw=True), base_dir=tmp_path
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [_record("tagged", event="pr.ready")]
    assert captured.err == ""


# --------------------------------------------------------------------------
# Filters through the verb — selection is the query's, uniform across views
# --------------------------------------------------------------------------


def _fixture_log(tmp_path) -> Path:
    """A fixture JSONL log mixing plain records, event records, and PRs."""
    return _write_log(
        tmp_path,
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
        + "\n",
    )


def test_events_and_pr_compose_as_and(tmp_path, capsys):
    # The demo read: `shipit logs --events --pr 231` → that PR's milestones.
    _fixture_log(tmp_path)
    rc = logs.run(
        REPO, query=logread.build_query(events_only=True, pr=231), base_dir=tmp_path
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
    rc = logs.run(REPO, query=logread.build_query(pr=231, tail=1), base_dir=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert "review request from copilot" in out[1]


def test_domain_filters_behave_identically_under_raw(tmp_path, capsys):
    # Uniformity: --raw changes the output MODE, never the selection — the
    # same query value drives both views.
    _write_log(
        tmp_path,
        "\n".join(
            [
                _record("implementer spawned", epic="LOG04", ws=1),
                _record("other epic", epic="RVW01", ws=1),
            ]
        )
        + "\n",
    )
    rc = logs.run(
        REPO,
        query=logread.build_query(epic="LOG04", ws="WS01", raw=True),
        base_dir=tmp_path,
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["msg"] for line in out] == ["implementer spawned"]


def test_cli_logs_help_shows_filter_flags(capsys):
    rc = cli.main(["logs", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--events" in out
    assert "--pr" in out


# --------------------------------------------------------------------------
# --flow — the rendered session story (implies --events)
# --------------------------------------------------------------------------


def _domain_fixture_log(tmp_path) -> Path:
    """A fixture JSONL log spanning two sessions, two epics, three Work Streams."""
    return _write_log(
        tmp_path,
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
        + "\n",
    )


def _flow_now():
    # 1h34m after the fixture records' shared ts (2026-07-02T12:00:00Z).
    return datetime(2026, 7, 2, 13, 34, 0, tzinfo=UTC)


def test_flow_implies_events_and_renders_the_story(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        REPO,
        query=logread.build_query(flow=True, session="sess-1"),
        base_dir=tmp_path,
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
        REPO, query=logread.build_query(flow=True), base_dir=tmp_path, now=_flow_now
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "session on LOG04, RVW01"


def test_flow_hides_agent_ids_until_asked(tmp_path, capsys):
    _domain_fixture_log(tmp_path)
    rc = logs.run(
        REPO, query=logread.build_query(flow=True), base_dir=tmp_path, now=_flow_now
    )
    assert rc == 0
    assert "run-a1" not in capsys.readouterr().out

    rc = logs.run(
        REPO,
        query=logread.build_query(flow=True, show_agents=True),
        base_dir=tmp_path,
        now=_flow_now,
    )
    assert rc == 0
    assert "[agent=run-a1]" in capsys.readouterr().out


def test_flow_skips_malformed_records_never_crashes(tmp_path, capsys):
    # The reader resilience contract continues into the story view: a torn
    # line has no fields to select on, so it drops silently (the flow filter
    # is always active) and the view renders the survivors.
    _write_log(
        tmp_path,
        "{ torn mid-write\n"
        + '"a bare string"\n'
        + _record("survivor", epic="LOG04", ws=4, event="pr.ready")
        + "\n",
    )
    rc = logs.run(
        REPO, query=logread.build_query(flow=True), base_dir=tmp_path, now=_flow_now
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "LOG04-WS04: survivor" in captured.out
    assert "torn" not in captured.out
    assert captured.err == ""


def test_flow_applies_the_tail_count_to_the_filtered_records(tmp_path, capsys):
    _write_log(
        tmp_path,
        "\n".join(
            _record(f"milestone {i}", epic="LOG04", ws=4, event="pr.ready")
            for i in range(5)
        )
        + "\n",
    )
    rc = logs.run(
        REPO,
        query=logread.build_query(flow=True, tail=2),
        base_dir=tmp_path,
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
    _write_log(
        tmp_path,
        _record("tuning the review loop", session="s1", event="session.intent")
        + "\n"
        + "\n".join(
            _record(
                f"milestone {i}", session="s1", epic="LOG04", ws=4, event="pr.ready"
            )
            for i in range(3)
        )
        + "\n",
    )
    rc = logs.run(
        REPO,
        query=logread.build_query(flow=True, tail=2),
        base_dir=tmp_path,
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


def test_cli_logs_help_shows_domain_key_and_flow_flags(capsys):
    rc = cli.main(["logs", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    for flag in (
        "--session",
        "--epic",
        "--ws",
        "--agent",
        "--role",
        "--reviewer",
        "--run",
        "--round",
        "--flow",
    ):
        assert flag in out
    assert "--agent-ids" in out


# --------------------------------------------------------------------------
# -f/--follow — the verb renders the engine's live iterator
# --------------------------------------------------------------------------


def test_follow_streams_appended_records_rendered(tmp_path, capsys):
    log = _write_log(tmp_path, _record("old1") + "\n" + _record("old2") + "\n")

    appended = [_record("new line A", pr=231), _record("new line B")]

    def fake_sleep(_interval: float) -> None:
        # Drive the poll loop: append a line per tick, then end like Ctrl-C.
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        REPO,
        query=logread.build_query(follow=True, tail=1),
        base_dir=tmp_path,
        sleep=fake_sleep,
    )
    assert rc == 0  # Ctrl-C ends a follow cleanly, the way `tail -f` does
    out = capsys.readouterr().out
    # The appended records streamed through — rendered, not raw JSON.
    assert "new line A [pr=231]" in out
    assert "new line B" in out
    assert "{" not in out.replace(str(log), "")
    # The pre-follow tail honored N=1 (only the last existing record, not old1).
    assert "old2" in out
    assert "old1" not in out


def test_follow_skips_malformed_lines_with_note(tmp_path, capsys):
    log = _write_log(tmp_path, _record("pre") + "\n")

    appended = ["{ torn mid-write", _record("post")]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        REPO,
        query=logread.build_query(follow=True, tail=-1),
        base_dir=tmp_path,
        sleep=fake_sleep,
    )
    assert rc == 0
    captured = capsys.readouterr()
    # The stream survives the torn line: the record after it still renders...
    assert "post" in captured.out
    assert "torn mid-write" not in captured.out
    # ...and the skip is noted, not crashed.
    assert "skipped malformed line" in captured.err


def test_raw_follow_streams_pure_jsonl(tmp_path, capsys):
    log = _write_log(tmp_path, _record("old") + "\n")

    appended = [_record("streamed", pr=7)]

    def fake_sleep(_interval: float) -> None:
        if appended:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(appended.pop(0) + "\n")
        else:
            raise KeyboardInterrupt

    rc = logs.run(
        REPO,
        query=logread.build_query(follow=True, raw=True, tail=-1),
        base_dir=tmp_path,
        sleep=fake_sleep,
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # No path header, and every emitted line — pre-existing and streamed — is
    # the stored JSONL verbatim.
    assert str(log) not in out
    assert [json.loads(line)["msg"] for line in out] == ["old", "streamed"]


# --------------------------------------------------------------------------
# Missing file — graceful, never a traceback
# --------------------------------------------------------------------------


def test_missing_log_file_is_graceful(tmp_path, capsys):
    rc = logs.run(REPO, base_dir=tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no log yet" in err
    assert "o/r" in err


# --------------------------------------------------------------------------
# Single source of truth — path comes from logsetup, never recomputed
# --------------------------------------------------------------------------


def test_path_comes_from_logsetup_log_file_path(tmp_path, monkeypatch, capsys):
    sentinel = tmp_path / "sentinel" / "shipit.log"
    seen: dict[str, object] = {}

    def fake_log_file_path(repo, *, base_dir=None):
        seen["repo"] = repo
        seen["base_dir"] = base_dir
        return sentinel

    monkeypatch.setattr(logs.logsetup, "log_file_path", fake_log_file_path)
    rc = logs.run(REPO, path_only=True, base_dir=tmp_path)
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(sentinel)
    # The reader hands the typed Repo + injected base straight to WS01's accessor.
    assert seen["repo"] == REPO
    assert seen["base_dir"] == tmp_path


def test_reader_does_not_recompute_path_or_add_env_override():
    # The reader consumes logsetup's resolution; neither the verb nor the
    # engine may sniff platformdirs, the platform, or a bespoke log-dir env
    # var of its own.
    from shipit.logread import engine

    for module in (logs, engine):
        src = Path(module.__file__).read_text()
        assert "platformdirs" not in src
        assert "user_log_dir" not in src
        assert "SHIPIT_LOG_DIR" not in src
        assert "sys.platform" not in src
    # ...and the VERB does call WS01's single-source-of-truth accessor.
    assert "log_file_path" in Path(logs.__file__).read_text()


def test_engine_never_prints():
    # The acceptance criterion, pinned at the source seam: records come out of
    # the engine's iterators; every terminal write is the verb's.
    from shipit.logread import engine, query, records

    for module in (engine, query, records):
        src = Path(module.__file__).read_text()
        assert "print(" not in src
        assert "sys.stdout" not in src
        assert "sys.stderr" not in src


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

    handler = logsetup.build_file_handler(REPO, base_dir=tmp_path)
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

    rc = logs.run(REPO, base_dir=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    rendered = captured.out.splitlines()[1]
    assert "INFO shipit.roundtrip: round trip" in rendered
    assert "[pr=231]" in rendered
    assert "malformed" not in captured.err


def test_cli_logs_path_smoke(capsys):
    # Explicit slug (parsed to a Repo at the boundary) + --path (no FS write):
    # a pure path computation through the real CLI entry.
    rc = cli.main(["logs", "--path", "octocat/hello-world"])
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/octocat/hello-world/shipit.log")
