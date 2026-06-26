"""Unit tests for `shipit logs` — the reader half of WS01's file sink (OBS01-WS04).

Asserts external behavior in shipit's style. Every boundary is injected: the
platformdirs base via ``base_dir``, the ``gh`` repo resolution via
``current_repo``, the follow-loop poll via ``sleep`` — so nothing reads a real
``$HOME`` or shells out to ``gh``.
"""

from __future__ import annotations

from pathlib import Path

from shipit import cli
from shipit.verbs import logs


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
# Default view — path + the last N lines
# --------------------------------------------------------------------------


def test_default_prints_path_then_last_n_lines(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")

    rc = logs.run("o/r", tail=3, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(log)
    assert out[1:] == ["line7", "line8", "line9"]


def test_tail_zero_prints_path_only_not_whole_file(tmp_path, capsys):
    # Regression: `lines[-0:]` is the whole file — `-n 0` must print NO log lines.
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text("a\nb\nc\n")

    rc = logs.run("o/r", tail=0, base_dir=tmp_path, current_repo=lambda: "x/y")
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [str(log)]


# --------------------------------------------------------------------------
# -f/--follow — stream appended lines live
# --------------------------------------------------------------------------


def test_follow_streams_appended_lines(tmp_path, capsys):
    log = tmp_path / "o" / "r" / "shipit.log"
    log.parent.mkdir(parents=True)
    log.write_text("old1\nold2\n")

    appended = ["new line A", "new line B"]

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
    # The appended lines streamed through.
    assert "new line A" in out
    assert "new line B" in out
    # The pre-follow tail honored N=1 (only the last existing line, not old1).
    assert "old2" in out
    assert "old1" not in out


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


def test_cli_logs_path_smoke(capsys):
    # Explicit slug (no gh) + --path (no FS write): a pure path computation.
    rc = cli.main(["logs", "--path", "octocat/hello-world"])
    assert rc == 0
    assert capsys.readouterr().out.strip().endswith("/octocat/hello-world/shipit.log")
