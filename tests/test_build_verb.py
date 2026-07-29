from pathlib import Path

import pytest

from shipit import execrun
from shipit.verbs import build as build_verb


class _Recorder:
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


def test_target_cross_compiles_the_rust_leg_and_leaves_npm_alone(tauri_repo):
    rec = _Recorder()
    rc = build_verb.run((), target="x86_64-unknown-linux-musl", run_step=rec)
    assert rc == 0
    assert rec.calls == [
        (
            (
                "cargo",
                "build",
                "--release",
                "-p",
                "app",
                "--target",
                "x86_64-unknown-linux-musl",
            ),
            tauri_repo,
            {},
        ),
        (("npm", "run", "build"), tauri_repo / "web", {}),
    ]


def test_pixi_is_never_the_build_backend(tauri_repo):
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
    assert env == {"CGO_ENABLED": "0"}
    assert "-trimpath" in argv
    assert "-s -w" in argv
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
    assert rec.calls == []
    err = capsys.readouterr().err
    assert "unknown leg 'python'" in err
    assert "rust (.)" in err and "npm (web)" in err


def test_multi_leg_passthrough_without_selector_is_usage_rc2(tauri_repo, capsys):
    rec = _Recorder()
    rc = build_verb.run(("--frozen",), run_step=rec)
    assert rc == 2
    assert rec.calls == []
    assert "rust (.)" in capsys.readouterr().err


def test_missing_builder_is_hard_127_never_a_skip(tauri_repo, capsys):
    boom = execrun.ExecError(["cargo"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _Recorder(outcomes={"cargo": boom})
    rc = build_verb.run((), run_step=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "pixi.toml#shipit-rust-release-toolchain" in out
    assert "`shipit install --pr`" in out
    assert "BUILD: FAILED (rust (.) [app])" in out
    assert any(argv[0] == "npm" for argv, _, _ in rec.calls)


def test_missing_unmanaged_builder_keeps_the_generic_provision_note(go_repo, capsys):
    boom = execrun.ExecError(["go"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _Recorder(outcomes={"go": boom})
    rc = build_verb.run((), run_step=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found on PATH" in out
    assert "shipit install" not in out


def test_missing_tree_sitter_gets_the_reconcile_remedy(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "tree-sitter"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    boom = execrun.ExecError(
        ["tree-sitter"], rc=None, cause=execrun.CAUSE_MISSING_BINARY
    )
    rec = _Recorder(outcomes={"tree-sitter": boom})
    rc = build_verb.run((), run_step=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "pixi.toml#shipit-tree-sitter-release-deps" in out
    assert "`shipit install --pr`" in out
    assert "`shipit install --local`" in out
    assert "pixi.lock" in out
    assert "BUILD: FAILED" in out


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


def test_whitespace_version_is_usage_rc2_before_any_build(go_repo, capsys):
    rec = _Recorder()
    assert build_verb.run((), version="1.2.3 -X evil=pwned", run_step=rec) == 2
    assert rec.calls == []
    assert "contains whitespace" in capsys.readouterr().err


def _two_go_paths_one_target(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"svc-a" = "go"\n'
        '"svc-b" = "go"\n'
        "[artifacts.x]\n"
        'build = [{ toolchain = "go", package = "./cmd/x" }]\n',
        encoding="utf-8",
    )
    (tmp_path / "svc-a").mkdir()
    (tmp_path / "svc-b").mkdir()


def test_target_toolchain_on_multiple_selected_paths_is_refused(
    tmp_path, monkeypatch, capsys
):
    _two_go_paths_one_target(tmp_path)
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    assert build_verb.run((), run_step=rec) == 1
    assert rec.calls == []
    err = capsys.readouterr().err
    assert "ambiguous" in err and "go (2 paths)" in err


def test_a_path_selector_resolves_the_ambiguity_to_one_leg(tmp_path, monkeypatch):
    _two_go_paths_one_target(tmp_path)
    monkeypatch.chdir(tmp_path)
    rec = _Recorder()
    assert build_verb.run(("svc-a",), run_step=rec) == 0
    assert rec.calls == [
        (
            ("go", "build", "-trimpath", "-ldflags", "-s -w", "./cmd/x"),
            tmp_path / "svc-a",
            {"CGO_ENABLED": "0"},
        )
    ]


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


def test_run_step_states_its_timeout_check_false_and_env(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw, argv=argv)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(build_verb.execrun, "run", fake_run)
    build_verb._run_step(("go", "build"), tmp_path, {"CGO_ENABLED": "0"})
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
    assert captured["env"] is None
