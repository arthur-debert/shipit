"""`shipit build` — the effectful shell over the build planner (TOL01-WS02).

Recorded-invocation tests through the injected exec boundary (prior art: the
`shipit test` verb tests over the one-exec seam, ADR-0028): exact builder
command lines — direct argv, NEVER `pixi run`-wrapped (PRD story 9) — env per
step (go's CGO_ENABLED=0), version injection only when supplied (ADR-0041),
the uniform exit contract (0 all steps build / 1 any fails / 2 usage), the
hard-fail on a missing builder, and the pointed missing-map error.
"""

from pathlib import Path

import pytest

from shipit import execrun
from shipit.verbs import build as build_verb


class _Recorder:
    """A fake exec boundary: records (argv, cwd, env), returns scripted
    outcomes. ``outcomes`` maps a binary name to an int rc, a ``(rc, output)``
    pair, or an exception to raise; unmapped binaries succeed."""

    def __init__(self, outcomes=None):
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.outcomes = outcomes or {}

    def __call__(self, argv, cwd, env):
        self.calls.append((tuple(argv), Path(cwd), dict(env)))
        outcome = self.outcomes.get(argv[0], 0)
        if isinstance(outcome, Exception):
            raise outcome
        rc, out = outcome if isinstance(outcome, tuple) else (outcome, f"{argv[0]} ran")
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout=out, stderr="", duration_ms=1
        )


@pytest.fixture
def tauri_repo(tmp_path, monkeypatch):
    """A two-leg repo (rust root + npm web path) with a declared artifact."""
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "rust"\n'
        '"web" = "npm"\n'
        "[artifacts.app]\n"
        'build = [{ toolchain = "rust", package = "app" }, { toolchain = "npm" }]\n'
        'endpoints = ["gh-release"]\n',
        encoding="utf-8",
    )
    (tmp_path / "web").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def go_repo(tmp_path, monkeypatch):
    """A single-leg go repo with a version-var-declaring artifact."""
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "go"\n'
        "[artifacts.mycli]\n"
        'build = [{ toolchain = "go", package = "./cmd/mycli",'
        ' version-var = "example.com/mycli/internal/version.Version" }]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# Recorded invocations — direct builder argv, cwd per leg, env per step
# --------------------------------------------------------------------------


def test_bare_run_dispatches_every_step_narrowed_by_the_artifact_map(
    tauri_repo, capsys
):
    rec = _Recorder()
    rc = build_verb.run((), run_step=rec)
    assert rc == 0
    assert rec.calls == [
        (("cargo", "build", "--release", "-p", "app"), tauri_repo, {}),
        (("npm", "run", "build"), tauri_repo / "web", {}),
    ]
    out = capsys.readouterr().out
    assert "build: rust (.) [app]: cargo build --release -p app" in out
    assert "build: npm (web) [app]: npm run build" in out
    assert "BUILD: OK (2 steps)" in out


def test_pixi_is_never_the_build_backend(tauri_repo):
    # PRD story 9: the verb execs the real builder directly; pixi provisions,
    # never builds — no argv is `pixi run`-wrapped.
    rec = _Recorder()
    assert build_verb.run((), run_step=rec) == 0
    assert all(argv[0] != "pixi" for argv, _, _ in rec.calls)


def test_repo_without_an_artifact_map_builds_each_leg_whole(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "python"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    assert build_verb.run((), run_step=rec) == 0
    assert rec.calls == [(("uv", "build"), tmp_path, {})]


def test_go_step_records_the_static_env_and_no_injection_without_a_version(
    go_repo,
):
    rec = _Recorder()
    assert build_verb.run((), run_step=rec) == 0
    ((argv, cwd, env),) = rec.calls
    # go-cli's legacy contract, pinned: static build, trimpath, stripped.
    assert env == {"CGO_ENABLED": "0"}
    assert "-trimpath" in argv
    assert "-s -w" in argv
    # ADR-0041: no supplied version -> the -X injection NEVER appears.
    assert not any("-X" in a for a in argv)


def test_supplied_version_is_injected_into_the_declared_go_var(go_repo):
    rec = _Recorder()
    assert build_verb.run((), version="1.2.3", run_step=rec) == 0
    ((argv, _, env),) = rec.calls
    assert argv == (
        "go",
        "build",
        "-trimpath",
        "-ldflags",
        "-s -w -X example.com/mycli/internal/version.Version=1.2.3",
        "./cmd/mycli",
    )
    assert env == {"CGO_ENABLED": "0"}


def test_per_path_override_replaces_the_registry_default(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = { toolchain = "rust", build = ["cargo", "zigbuild", "--release"] }\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    assert build_verb.run((), run_step=rec) == 0
    assert rec.calls == [(("cargo", "zigbuild", "--release"), tmp_path, {})]


def test_selector_with_passthrough_forwards_verbatim(tauri_repo):
    rec = _Recorder()
    rc = build_verb.run(("npm", "--if-present"), run_step=rec)
    assert rc == 0
    (call,) = rec.calls
    assert call[0][:4] == ("npm", "run", "build", "--if-present")


def test_leg_output_prints_verbatim_even_when_green(tauri_repo, capsys):
    rec = _Recorder(outcomes={"cargo": (0, "Finished `release` profile")})
    assert build_verb.run(("rust",), run_step=rec) == 0
    assert "Finished `release` profile" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The exit contract (ADR-0030): 0 / 1 / 2, and the hard-fail
# --------------------------------------------------------------------------


def test_any_failing_step_fails_the_run_naming_the_step(tauri_repo, capsys):
    rec = _Recorder(outcomes={"npm": (1, "build script failed")})
    rc = build_verb.run((), run_step=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "BUILD: FAILED (npm (web) [app])" in out
    assert "build script failed" in out


def test_unknown_selector_is_usage_rc2_naming_known_legs(tauri_repo, capsys):
    rec = _Recorder()
    rc = build_verb.run(("python",), run_step=rec)
    assert rc == 2
    assert rec.calls == []  # rejected before any step runs
    err = capsys.readouterr().err
    assert "unknown leg 'python'" in err
    assert "rust (.)" in err and "npm (web)" in err


def test_multi_leg_passthrough_without_selector_is_usage_rc2(tauri_repo, capsys):
    rec = _Recorder()
    rc = build_verb.run(("--frozen",), run_step=rec)
    assert rc == 2
    assert rec.calls == []  # a hard error, never a broadcast
    assert "rust (.)" in capsys.readouterr().err


def test_missing_builder_is_hard_127_never_a_skip(tauri_repo, capsys):
    boom = execrun.ExecError(["cargo"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _Recorder(outcomes={"cargo": boom})
    rc = build_verb.run((), run_step=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found on PATH" in out
    assert "BUILD: FAILED (rust (.) [app])" in out
    # The other step still ran: one broken step never silences the rest.
    assert any(argv[0] == "npm" for argv, _, _ in rec.calls)


# --------------------------------------------------------------------------
# The map reads — pointed errors, through the cli_errors shell (exit 1)
# --------------------------------------------------------------------------


def test_missing_map_is_a_pointed_error_naming_the_verb(tmp_path, monkeypatch, capsys):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert build_verb.run((), run_step=_Recorder()) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: no [toolchains]")
    assert "`shipit build`" in err
    assert '"go.mod" -> go' in err


def test_orphaned_build_target_toolchain_is_refused_loudly(
    tmp_path, monkeypatch, capsys
):
    # An artifact whose target names a toolchain with no [toolchains] leg
    # would silently never build — a config inconsistency, not a quiet skip.
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "python"\n'
        "[artifacts.cli]\n"
        'build = [{ toolchain = "rust", package = "cli" }]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    assert build_verb.run((), run_step=rec) == 1
    assert rec.calls == []
    err = capsys.readouterr().err
    assert "no [toolchains] leg" in err and "cli -> rust" in err


def test_malformed_artifact_map_is_one_clean_error_line(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "python"\n[artifacts.x]\nendpoints = ["snapstore"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert build_verb.run((), run_step=_Recorder()) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "unknown endpoint `snapstore`" in err


# --------------------------------------------------------------------------
# The exec boundary — the stated timeout and env ride the wire (ADR-0028)
# --------------------------------------------------------------------------


def test_run_step_states_its_timeout_check_false_and_env(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw, argv=argv)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(build_verb.execrun, "run", fake_run)
    build_verb._run_step(("go", "build"), tmp_path, {"CGO_ENABLED": "0"})
    # A build legitimately compiles a workspace cold — the bound is the
    # verb's own (an hour), stated on the wire, never the runner's default.
    assert captured["timeout"] == build_verb.BUILD_TIMEOUT
    assert captured["check"] is False
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"] == {"CGO_ENABLED": "0"}


def test_run_step_passes_no_env_when_the_step_adds_none(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(build_verb.execrun, "run", fake_run)
    build_verb._run_step(("uv", "build"), tmp_path, {})
    # None (inherit untouched), not {} — {} with replace_env=False is merged
    # anyway, but None keeps the runner's "no env shaping" fast path honest.
    assert captured["env"] is None
