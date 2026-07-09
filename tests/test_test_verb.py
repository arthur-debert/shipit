"""`shipit test` — the effectful shell over the leg planner (TOL01-WS01).

Recorded-invocation tests through the injected exec boundary (prior art: the
lint tool-runner tests over the one-exec seam, ADR-0028): exact command lines
including passthrough placement and cwd per leg, the uniform exit contract
(0 all legs pass / 1 any leg fails / 2 usage), the hard-fail on a missing
tool binary, and the pointed missing-map error.
"""

from pathlib import Path

import pytest

from shipit import execrun
from shipit.verbs import test as test_verb


class _Recorder:
    """A fake exec boundary: records (argv, cwd), returns scripted outcomes.

    ``outcomes`` maps a binary name to an int rc, a ``(rc, output)`` pair, or
    an exception to raise; unmapped binaries succeed with a canned output.
    """

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
    """A two-leg repo (rust root + npm web path), cwd'd into."""
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "rust"\n"web" = "npm"\n', encoding="utf-8"
    )
    (tmp_path / "web").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def python_repo(tmp_path, monkeypatch):
    """A single-leg python repo, cwd'd into."""
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "python"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# split_args — the selector/passthrough boundary (click consumes the `--`)
# --------------------------------------------------------------------------


def test_split_args_first_bare_token_is_the_selector():
    assert test_verb.split_args(("rust", "--no-capture")) == (
        "rust",
        ("--no-capture",),
    )


def test_split_args_leading_dash_means_no_selector():
    assert test_verb.split_args(("-k", "foo")) == (None, ("-k", "foo"))


def test_split_args_empty_is_the_bare_fan_out():
    assert test_verb.split_args(()) == (None, ())


# --------------------------------------------------------------------------
# Recorded invocations — exact command lines and cwd per leg
# --------------------------------------------------------------------------


def test_bare_run_dispatches_every_leg_with_registry_defaults(tauri_repo, capsys):
    rec = _Recorder()
    rc = test_verb.run((), run_leg=rec)
    assert rc == 0
    assert rec.calls == [
        (("cargo", "nextest", "run"), tauri_repo),
        (("npm", "test"), tauri_repo / "web"),
    ]
    out = capsys.readouterr().out
    # Per-leg reporting names leg and command; the summary counts legs.
    assert "test: rust (.): cargo nextest run" in out
    assert "test: npm (web): npm test" in out
    assert "TEST: OK (2 legs)" in out


def test_leg_output_prints_verbatim_even_when_green(tauri_repo, capsys):
    # Unlike lint, a passing test run's report IS the product — never swallowed.
    rec = _Recorder(outcomes={"cargo": (0, "12 tests run: 12 passed")})
    assert test_verb.run(("rust",), run_leg=rec) == 0
    assert "12 tests run: 12 passed" in capsys.readouterr().out


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


# --------------------------------------------------------------------------
# The exit contract (ADR-0030): 0 / 1 / 2, and the hard-fail
# --------------------------------------------------------------------------


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
    assert rec.calls == []  # rejected before any leg runs
    err = capsys.readouterr().err
    assert "unknown leg 'python'" in err
    assert "rust (.)" in err and "npm (web)" in err


def test_multi_leg_passthrough_without_selector_is_usage_rc2_listing_legs(
    tauri_repo, capsys
):
    rec = _Recorder()
    rc = test_verb.run(("-k", "foo"), run_leg=rec)
    assert rc == 2
    assert rec.calls == []  # a hard error, never a broadcast
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
    # The other leg still ran: one broken leg never silences the rest.
    assert (("npm", "test"), tauri_repo / "web") in rec.calls


def test_unlaunchable_leg_carries_the_transport_detail(tauri_repo, capsys):
    boom = execrun.ExecError(
        ["cargo"], rc=None, stderr="Permission denied", cause=execrun.CAUSE_OS
    )
    rec = _Recorder(outcomes={"cargo": boom})
    assert test_verb.run(("rust",), run_leg=rec) == 1
    out = capsys.readouterr().out
    assert "could not run" in out and "Permission denied" in out


# --------------------------------------------------------------------------
# The map read — pointed errors, through the cli_errors shell (exit 1)
# --------------------------------------------------------------------------


def test_missing_map_is_a_pointed_error_naming_the_signals(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = test_verb.run((), run_leg=_Recorder())
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: no [toolchains]")
    # The repo's own manifests point at the likely declaration.
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


# --------------------------------------------------------------------------
# The exec boundary — the stated timeout rides the wire (ADR-0028)
# --------------------------------------------------------------------------


def test_run_leg_states_its_timeout_and_check_false(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw, argv=argv)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(test_verb.execrun, "run", fake_run)
    test_verb._run_leg(("pytest",), tmp_path)
    # A test leg legitimately compiles then runs a suite — the bound is the
    # verb's own (an hour), stated on the wire, never the runner's default.
    assert captured["timeout"] == test_verb.TEST_TIMEOUT
    assert captured["check"] is False
    assert captured["cwd"] == str(tmp_path)
