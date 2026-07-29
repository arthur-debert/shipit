from pathlib import Path

import pytest

from shipit import config, execrun
from shipit.verbs import test as test_verb

_RUST = config.ToolchainEntry(path=".", toolchain="rust", commands={})
_WEB_NPM = config.ToolchainEntry(path="web", toolchain="npm", commands={})
_PY = config.ToolchainEntry(path=".", toolchain="python", commands={})


class _Recorder:
    def __init__(self, outcomes=None):
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.outcomes = outcomes or {}

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), Path(cwd)))
        outcome = self.outcomes.get(argv[0], 0)
        if isinstance(outcome, Exception):
            raise outcome
        rc, out = outcome if isinstance(outcome, tuple) else (outcome, f"{argv[0]} ran")
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout=out, stderr="", duration_ms=1
        )


@pytest.fixture
def tauri_repo(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "rust"\n"web" = "npm"\n', encoding="utf-8"
    )
    (tmp_path / "web").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def python_repo(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "python"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_split_args_first_bare_token_naming_a_leg_is_the_selector():
    assert test_verb.split_args(("rust", "--no-capture"), (_RUST, _WEB_NPM)) == (
        "rust",
        ("--no-capture",),
    )


def test_split_args_leading_dash_means_no_selector():
    assert test_verb.split_args(("-k", "foo"), (_PY,)) == (None, ("-k", "foo"))


def test_split_args_empty_is_the_bare_fan_out():
    assert test_verb.split_args((), (_PY,)) == (None, ())


def test_split_args_positional_on_single_leg_repo_is_passthrough():
    assert test_verb.split_args(("tests/foo.py",), (_PY,)) == (
        None,
        ("tests/foo.py",),
    )


def test_split_args_unknown_first_token_on_multi_leg_repo_stays_a_selector():
    assert test_verb.split_args(("tests/foo.py",), (_RUST, _WEB_NPM)) == (
        "tests/foo.py",
        (),
    )


def test_bare_run_dispatches_every_leg_with_registry_defaults(tauri_repo, capsys):
    rec = _Recorder()
    rc = test_verb.run((), run_leg=rec)
    assert rc == 0
    assert rec.calls == [
        (("cargo", "nextest", "run"), tauri_repo),
        (("npm", "test"), tauri_repo / "web"),
    ]
    out = capsys.readouterr().out
    assert "test: rust (.): cargo nextest run" in out
    assert "test: npm (web): npm test" in out
    assert "TEST: OK (2 legs)" in out


def test_leg_output_prints_verbatim_even_when_green(tauri_repo, capsys):
    rec = _Recorder(outcomes={"cargo": (0, "12 tests run: 12 passed")})
    assert test_verb.run(("rust",), run_leg=rec) == 0
    assert "12 tests run: 12 passed" in capsys.readouterr().out


def test_leg_output_is_verbatim_and_the_trailing_newline_is_not_doubled(
    python_repo, capsys
):
    rec = _Recorder(outcomes={"pytest": (0, "line one\nline two\n")})
    assert test_verb.run((), run_leg=rec) == 0
    out = capsys.readouterr().out
    assert "line one\nline two\n  ok   python (.)" in out


def test_selector_with_passthrough_places_args_verbatim_at_the_end(tauri_repo, capsys):
    rec = _Recorder()
    rc = test_verb.run(("rust", "--no-capture", "-E", "test(x)"), run_leg=rec)
    assert rc == 0
    assert rec.calls == [
        (("cargo", "nextest", "run", "--no-capture", "-E", "test(x)"), tauri_repo)
    ]


def test_per_path_override_replaces_the_registry_default(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = { toolchain = "python", test = ["python", "-m", "pytest", "-q"] }\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    assert test_verb.run((), run_leg=rec) == 0
    assert rec.calls == [(("python", "-m", "pytest", "-q"), tmp_path)]


def test_single_leg_repo_takes_passthrough_without_a_selector(python_repo):
    rec = _Recorder()
    assert test_verb.run(("-k", "foo"), run_leg=rec) == 0
    assert rec.calls == [(("pytest", "-k", "foo"), python_repo)]


def test_single_leg_repo_forwards_a_positional_path_to_the_runner(python_repo):
    rec = _Recorder()
    assert test_verb.run(("tests/test_foo.py",), run_leg=rec) == 0
    assert rec.calls == [(("pytest", "tests/test_foo.py"), python_repo)]


def test_any_failing_leg_fails_the_run_naming_the_leg(tauri_repo, capsys):
    rec = _Recorder(outcomes={"npm": (1, "1 failing")})
    rc = test_verb.run((), run_leg=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "TEST: FAILED (npm (web))" in out
    assert "1 failing" in out


def test_unknown_selector_is_usage_rc2_naming_known_legs(tauri_repo, capsys):
    rec = _Recorder()
    rc = test_verb.run(("python",), run_leg=rec)
    assert rc == 2
    assert rec.calls == []
    err = capsys.readouterr().err
    assert "unknown leg 'python'" in err
    assert "rust (.)" in err and "npm (web)" in err


def test_multi_leg_passthrough_without_selector_is_usage_rc2_listing_legs(
    tauri_repo, capsys
):
    rec = _Recorder()
    rc = test_verb.run(("-k", "foo"), run_leg=rec)
    assert rc == 2
    assert rec.calls == []
    err = capsys.readouterr().err
    assert "rust (.)" in err and "npm (web)" in err


def test_missing_binary_is_hard_127_never_a_skip(tauri_repo, capsys):
    boom = execrun.ExecError(["cargo"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _Recorder(outcomes={"cargo": boom})
    rc = test_verb.run((), run_leg=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found on PATH" in out
    assert "TEST: FAILED (rust (.))" in out
    assert (("npm", "test"), tauri_repo / "web") in rec.calls


def test_unlaunchable_leg_carries_the_transport_detail(tauri_repo, capsys):
    boom = execrun.ExecError(
        ["cargo"], rc=None, stderr="Permission denied", cause=execrun.CAUSE_OS
    )
    rec = _Recorder(outcomes={"cargo": boom})
    assert test_verb.run(("rust",), run_leg=rec) == 1
    out = capsys.readouterr().out
    assert "could not run" in out and "Permission denied" in out


def test_missing_map_is_a_pointed_error_naming_the_signals(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = test_verb.run((), run_leg=_Recorder())
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: no [toolchains]")
    assert '"Cargo.toml" -> rust' in err
    assert "[toolchains]" in err


def test_empty_map_in_an_existing_config_is_the_same_error(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".shipit.toml").write_text("[lint]\nignore = []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert test_verb.run((), run_leg=_Recorder()) == 1
    assert "no [toolchains]" in capsys.readouterr().err


def test_malformed_config_is_one_clean_error_line(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "not-a-toolchain"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert test_verb.run((), run_leg=_Recorder()) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "unknown toolchain `not-a-toolchain`" in err


def test_run_leg_states_its_timeout_and_check_false(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw, argv=argv)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(test_verb.execrun, "run", fake_run)
    test_verb._run_leg(("pytest",), tmp_path)
    assert captured["timeout"] == test_verb.TEST_TIMEOUT
    assert captured["check"] is False
    assert captured["cwd"] == str(tmp_path)
