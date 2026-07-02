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

from shipit import cli, gh
from shipit.verbs import logs


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
    assert "owner/repo" in capsys.readouterr().err


def test_gh_error_resolving_repo_is_graceful(tmp_path, capsys):
    # No explicit repo and the cwd resolution shells out to gh and fails (not a
    # checkout / gh missing). That must be a clean usage error, never a traceback.
    def boom() -> str:
        raise gh.GhError("not a git repository")

    rc = logs.run(base_dir=tmp_path, current_repo=boom)
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not determine the current repo" in err
    assert "owner/repo" in err


# --------------------------------------------------------------------------
# Single source of truth — path comes from logsetup, never recomputed here
# --------------------------------------------------------------------------


def test_path_comes_from_logsetup_log_file_path(tmp_path, monkeypatch, capsys):
    sentinel = tmp_path / "sentinel" / "shipit.log"
    seen: dict[str, object] = {}

    def fake_log_file_path(owner_repo, *, base_dir=None):
        seen["owner_repo"] = owner_repo
        seen["base_dir"] = base_dir
        return sentinel

    monkeypatch.setattr(logs.logsetup, "log_file_path", fake_log_file_path)
    rc = logs.run("o/r", path_only=True, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(sentinel)
    # The reader hands the parsed slug + injected base straight to WS01's accessor.
    assert seen["owner_repo"] == ("o", "r")
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

    handler = logsetup.build_file_handler(("o", "r"), base_dir=tmp_path)
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
