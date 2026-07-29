from pathlib import Path

from shipit import execrun
from shipit.verbs import e2e as e2e_verb


class _FakeSource:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.resolved: list[str] = []

    def resolve(self, artifact):
        self.resolved.append(artifact.name)
        outcome = self.outcomes[artifact.name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _HarnessRecorder:
    def __init__(self, outcomes=None):
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
        self.outcomes = outcomes or {}

    def __call__(self, argv, cwd, env):
        self.calls.append((tuple(argv), Path(cwd), dict(env)))
        outcome = self.outcomes.get(argv[0], 0)
        if isinstance(outcome, Exception):
            raise outcome
        rc, out = outcome if isinstance(outcome, tuple) else (outcome, "1..1 ok")
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout=out, stderr="", duration_ms=1
        )


def _repo(tmp_path, monkeypatch, toml, *, check_e2e=True):
    (tmp_path / ".shipit.toml").write_text(toml, encoding="utf-8")
    if check_e2e:
        script = tmp_path / "bin" / "check-e2e"
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    return tmp_path


PADZ_TOML = (
    "[toolchains]\n"
    '"." = "go"\n'
    "[artifacts.padz]\n"
    'build = [{ toolchain = "go", package = "./cmd/padz" }]\n'
    "e2e = {}\n"
)


def test_repo_without_any_e2e_declaration_reports_and_exits_0(
    tmp_path, monkeypatch, capsys
):
    _repo(
        tmp_path,
        monkeypatch,
        '[toolchains]\n"." = "rust"\n'
        "[artifacts.cli]\n"
        'build = [{ toolchain = "rust" }]\n',
    )
    rec = _HarnessRecorder()
    source = _FakeSource({})
    rc = e2e_verb.run((), source=source, run_harness=rec)
    assert rc == 0
    assert source.resolved == []
    assert rec.calls == []
    assert "e2e: no e2e declared" in capsys.readouterr().out


def test_explicit_selector_on_a_repo_with_no_e2e_is_usage_rc2(
    tmp_path, monkeypatch, capsys
):
    _repo(
        tmp_path,
        monkeypatch,
        '[toolchains]\n"." = "rust"\n'
        "[artifacts.cli]\n"
        'build = [{ toolchain = "rust" }]\n',
    )
    source = _FakeSource({})
    rc = e2e_verb.run(("cli",), source=source, run_harness=_HarnessRecorder())
    assert rc == 2
    assert source.resolved == []
    err = capsys.readouterr().err
    assert "unknown e2e artifact 'cli'" in err
    assert "no artifact" in err


def test_repo_without_any_config_at_all_reports_and_exits_0(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    assert e2e_verb.run((), source=_FakeSource({}), run_harness=_HarnessRecorder()) == 0
    assert "no e2e declared" in capsys.readouterr().out


def test_harness_runs_with_name_bin_set_to_the_resolved_absolute_path(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    binary = root / "padz"
    rec = _HarnessRecorder()
    rc = e2e_verb.run((), source=_FakeSource({"padz": binary}), run_harness=rec)
    assert rc == 0
    assert rec.calls == [
        (("bin/check-e2e",), root, {"PADZ_BIN": str(binary)}),
    ]
    out = capsys.readouterr().out
    assert f"e2e: padz: bin/check-e2e [PADZ_BIN={binary}]" in out
    assert "E2E: OK (1 harness)" in out


def test_declared_harness_replaces_the_default_and_gets_passthrough(
    tmp_path, monkeypatch
):
    root = _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"." = "rust"\n'
        "[artifacts.lex-cli]\n"
        'build = [{ toolchain = "rust", package = "lex-cli" }]\n'
        'e2e = { harness = ["bats", "tests/e2e.bats"] }\n',
        check_e2e=False,
    )
    binary = root / "target" / "release" / "lex-cli"
    rec = _HarnessRecorder()
    rc = e2e_verb.run(
        ("lex-cli", "--tap"), source=_FakeSource({"lex-cli": binary}), run_harness=rec
    )
    assert rc == 0
    ((argv, cwd, env),) = rec.calls
    assert argv == ("bats", "tests/e2e.bats", "--tap")
    assert cwd == root
    assert env == {"LEX_CLI_BIN": str(binary)}


def test_gui_harness_injects_the_e2e_env_alongside_name_bin(
    tmp_path, monkeypatch, capsys
):
    root = _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"." = "rust"\n'
        "[artifacts.gal]\n"
        'build = [{ toolchain = "rust", package = "gal" }]\n'
        'e2e = { harness = "electron" }\n',
        check_e2e=False,
    )
    binary = root / "target" / "release" / "gal"
    rec = _HarnessRecorder()
    rc = e2e_verb.run((), source=_FakeSource({"gal": binary}), run_harness=rec)
    assert rc == 0
    ((argv, cwd, env),) = rec.calls
    assert argv == ("npm", "exec", "--", "playwright", "test")
    assert cwd == root
    assert env == {
        "E2E": "1",
        "E2E_HIDE_WINDOW": "1",
        "E2E_DISABLE_PERSISTENCE": "1",
        "GAL_BIN": str(binary),
    }
    out = capsys.readouterr().out
    assert (
        "e2e: gal: npm exec -- playwright test "
        f"[E2E=1 E2E_HIDE_WINDOW=1 E2E_DISABLE_PERSISTENCE=1 GAL_BIN={binary}]"
    ) in out


def test_unknown_named_harness_is_one_clean_config_error(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        '[toolchains]\n"." = "rust"\n[artifacts.x]\n'
        'build = [{ toolchain = "rust" }]\ne2e = { harness = "qt" }\n',
        check_e2e=False,
    )
    rec = _HarnessRecorder()
    rc = e2e_verb.run((), source=_FakeSource({}), run_harness=rec)
    assert rc == 1
    assert rec.calls == []
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "unknown e2e harness 'qt'" in err
    assert "electron" in err


def test_harness_output_prints_verbatim_even_when_green(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    rec = _HarnessRecorder(outcomes={"bin/check-e2e": (0, "1..2\n\nok 1\nok 2 # ")})
    assert (
        e2e_verb.run((), source=_FakeSource({"padz": root / "padz"}), run_harness=rec)
        == 0
    )
    out = capsys.readouterr().out
    assert "1..2\n\nok 1\nok 2 # \n" in out


def test_missing_default_harness_script_is_a_hard_error_naming_the_path(
    tmp_path, monkeypatch, capsys
):
    _repo(tmp_path, monkeypatch, PADZ_TOML, check_e2e=False)
    rec = _HarnessRecorder()
    source = _FakeSource({"padz": tmp_path / "padz"})
    rc = e2e_verb.run((), source=source, run_harness=rec)
    assert rc == 1
    assert source.resolved == []
    assert rec.calls == []
    err = capsys.readouterr().err
    assert err.startswith("error: e2e harness script")
    assert str(tmp_path / "bin" / "check-e2e") in err
    assert "does not exist" in err


def test_non_executable_harness_script_is_a_hard_error(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    (root / "bin" / "check-e2e").chmod(0o644)
    rc = e2e_verb.run(
        (), source=_FakeSource({"padz": root / "padz"}), run_harness=_HarnessRecorder()
    )
    assert rc == 1
    assert "not executable" in capsys.readouterr().err


def test_harness_failure_is_the_tools_verdict_rc1(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    rec = _HarnessRecorder(outcomes={"bin/check-e2e": (1, "not ok 1 boots")})
    rc = e2e_verb.run((), source=_FakeSource({"padz": root / "padz"}), run_harness=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "not ok 1 boots" in out
    assert "E2E: FAILED (padz)" in out


def test_unresolvable_artifact_hard_fails_the_job_but_not_the_others(
    tmp_path, monkeypatch, capsys
):
    from shipit.tools import artifact_source

    root = _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"." = "go"\n'
        "[artifacts.broken]\n"
        'build = [{ toolchain = "go" }]\ne2e = {}\n'
        "[artifacts.padz]\n"
        'build = [{ toolchain = "go", package = "./cmd/padz" }]\ne2e = {}\n',
    )
    rec = _HarnessRecorder()
    source = _FakeSource(
        {
            "broken": artifact_source.ArtifactSourceError(
                "local build of artifact broken failed"
            ),
            "padz": root / "padz",
        }
    )
    rc = e2e_verb.run((), source=source, run_harness=rec)
    assert rc == 1
    assert [argv[0] for argv, _, _ in rec.calls] == ["bin/check-e2e"]
    out = capsys.readouterr().out
    assert "FAIL broken" in out
    assert "E2E: FAILED (broken)" in out


def test_unknown_artifact_selector_is_usage_rc2_naming_declared(
    tmp_path, monkeypatch, capsys
):
    _repo(tmp_path, monkeypatch, PADZ_TOML)
    source = _FakeSource({})
    rc = e2e_verb.run(("dodot",), source=source, run_harness=_HarnessRecorder())
    assert rc == 2
    assert source.resolved == []
    err = capsys.readouterr().err
    assert "unknown e2e artifact 'dodot'" in err
    assert "padz" in err


def test_multi_artifact_passthrough_without_selector_is_usage_rc2(
    tmp_path, monkeypatch, capsys
):
    _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"." = "go"\n'
        "[artifacts.a]\n"
        'build = [{ toolchain = "go" }]\ne2e = {}\n'
        "[artifacts.b]\n"
        'build = [{ toolchain = "go" }]\ne2e = {}\n',
    )
    rc = e2e_verb.run(
        ("--tap",), source=_FakeSource({}), run_harness=_HarnessRecorder()
    )
    assert rc == 2
    assert "exactly one" in capsys.readouterr().err


def test_missing_harness_binary_is_hard_127_never_a_skip(tmp_path, monkeypatch, capsys):
    root = _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"." = "rust"\n'
        "[artifacts.cli]\n"
        'build = [{ toolchain = "rust" }]\n'
        'e2e = { harness = ["bats", "tests"] }\n',
        check_e2e=False,
    )
    boom = execrun.ExecError(["bats"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _HarnessRecorder(outcomes={"bats": boom})
    rc = e2e_verb.run((), source=_FakeSource({"cli": root / "t"}), run_harness=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found on PATH" in out
    assert "E2E: FAILED (cli)" in out


def test_config_inconsistency_is_one_clean_error_line(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        '[toolchains]\n"." = "npm"\n[artifacts.site]\n'
        'build = [{ toolchain = "npm" }]\ne2e = {}\n',
    )
    rc = e2e_verb.run((), run_harness=_HarnessRecorder())
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no binary-producing" in err


def test_config_validation_is_fail_fast_before_any_healthy_job_runs(
    tmp_path, monkeypatch, capsys
):
    _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"." = "go"\n'
        "[artifacts.padz]\n"
        'build = [{ toolchain = "go", package = "./cmd/padz" }]\ne2e = {}\n'
        "[artifacts.site]\n"
        'build = [{ toolchain = "npm" }]\ne2e = {}\n',
    )
    rec = _HarnessRecorder()
    rc = e2e_verb.run((), run_harness=rec)
    assert rc == 1
    assert rec.calls == []
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no [toolchains] leg" in err and "site -> npm" in err


def test_ambiguous_producing_path_is_refused_up_front(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        "[toolchains]\n"
        '"svc-a" = "go"\n'
        '"svc-b" = "go"\n'
        "[artifacts.x]\n"
        'build = [{ toolchain = "go", package = "./cmd/x" }]\ne2e = {}\n',
    )
    rec = _HarnessRecorder()
    rc = e2e_verb.run((), run_harness=rec)
    assert rc == 1
    assert rec.calls == []
    err = capsys.readouterr().err
    assert "ambiguous" in err and "go (2 paths)" in err


def test_runs_out_is_an_output_sink_never_aliased(tmp_path, monkeypatch, capsys):
    from shipit import config
    from shipit.tools import e2e as e2e_mod

    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    stale = e2e_verb.HarnessRun(
        e2e_mod.E2eJob(config.Artifact(name="old"), harness=("x",), env_var="OLD_BIN"),
        returncode=1,
        output="stale failure",
    )
    runs_out = [stale]
    rc = e2e_verb.run(
        (),
        source=_FakeSource({"padz": root / "padz"}),
        run_harness=_HarnessRecorder(),
        runs_out=runs_out,
    )
    assert rc == 0
    assert runs_out[0] is stale
    assert len(runs_out) == 2
    assert "E2E: OK (1 harness)" in capsys.readouterr().out


def test_run_harness_states_its_timeout_check_false_and_merged_env(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw, argv=argv)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(e2e_verb.execrun, "run", fake_run)
    e2e_verb._run_harness(("bin/check-e2e",), tmp_path, {"PADZ_BIN": "/x/padz"})
    assert captured["timeout"] == e2e_verb.E2E_TIMEOUT
    assert captured["check"] is False
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"] == {"PADZ_BIN": "/x/padz"}


def test_full_local_flow_builds_injects_and_runs_the_harness(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = { toolchain = "rust", build = ["true"] }\n'
        "[artifacts.mytool]\n"
        'build = [{ toolchain = "rust", package = "mytool" }]\n'
        "e2e = {}\n",
        encoding="utf-8",
    )
    binary = tmp_path / "target" / "release" / "mytool"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho mytool-ok\n", encoding="utf-8")
    binary.chmod(0o755)
    script = tmp_path / "bin" / "check-e2e"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        'case "$MYTOOL_BIN" in /*) ;; *) echo "not absolute"; exit 1;; esac\n'
        '[ -x "$MYTOOL_BIN" ] || { echo "not executable"; exit 1; }\n'
        '"$MYTOOL_BIN"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    rc = e2e_verb.run(())
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "e2e: build rust (.) [mytool]: true" in out
    assert "mytool-ok" in out
    assert "E2E: OK (1 harness)" in out
