"""`shipit e2e` — the effectful shell over the e2e planner (TOL01-WS03).

Recorded-invocation tests through the injected artifact source and harness
runner (prior art: the test/build verb tests over the one-exec seam,
ADR-0028): the `<NAME>_BIN` env injection with the resolved absolute path,
the no-declaration clean exit (PRD story 11), the legacy hard error on a
missing/non-executable harness script, the uniform 0/1/2 exit contract
shared with test/build (the harness verdict IS the tool's verdict), and one
full-stack run with the real local-build source and a real bats-shaped
consumer script.
"""

from pathlib import Path

from shipit import execrun
from shipit.verbs import e2e as e2e_verb


class _FakeSource:
    """A scripted artifact source: name → an absolute Path to return or an
    exception to raise. Records every resolve."""

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
    """A fake harness runner: records (argv, cwd, env), returns scripted
    outcomes keyed by argv head (int rc, `(rc, output)`, or an exception);
    unmapped heads pass."""

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


# --------------------------------------------------------------------------
# No declaration -> no e2e lane (PRD story 11): a report and exit 0
# --------------------------------------------------------------------------


def test_repo_without_any_e2e_declaration_reports_and_exits_0(
    tmp_path, monkeypatch, capsys
):
    # Artifacts exist, none declares e2e — opting out is the ABSENCE of
    # config: rc 0, nothing resolved, nothing run.
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
    # Naming an artifact when NONE declares e2e is a usage error, NOT the
    # clean no-op: `shipit e2e cli` must not exit 0 green (a silent CI no-op
    # because `cli` forgot its e2e table) — it is rc 2, nothing resolved.
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


# --------------------------------------------------------------------------
# The <NAME>_BIN injection — the recorded-env test (issue #556's core)
# --------------------------------------------------------------------------


def test_harness_runs_with_name_bin_set_to_the_resolved_absolute_path(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    binary = root / "padz"
    rec = _HarnessRecorder()
    rc = e2e_verb.run((), source=_FakeSource({"padz": binary}), run_harness=rec)
    assert rc == 0
    # The default harness, from the repo root, with exactly the injection.
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
        check_e2e=False,  # a bare-name head needs no script on disk
    )
    binary = root / "target" / "release" / "lex-cli"
    rec = _HarnessRecorder()
    rc = e2e_verb.run(
        ("lex-cli", "--tap"), source=_FakeSource({"lex-cli": binary}), run_harness=rec
    )
    assert rc == 0
    ((argv, cwd, env),) = rec.calls
    assert argv == ("bats", "tests/e2e.bats", "--tap")  # verbatim, appended
    assert cwd == root
    assert env == {"LEX_CLI_BIN": str(binary)}  # `-` -> `_`, legacy contract


def test_harness_output_prints_verbatim_even_when_green(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch, PADZ_TOML)
    rec = _HarnessRecorder(outcomes={"bin/check-e2e": (0, "1..3\nok 1\nok 2\nok 3")})
    assert (
        e2e_verb.run((), source=_FakeSource({"padz": root / "padz"}), run_harness=rec)
        == 0
    )
    assert "ok 3" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The legacy hard error: missing / non-executable harness script
# --------------------------------------------------------------------------


def test_missing_default_harness_script_is_a_hard_error_naming_the_path(
    tmp_path, monkeypatch, capsys
):
    _repo(tmp_path, monkeypatch, PADZ_TOML, check_e2e=False)
    rec = _HarnessRecorder()
    source = _FakeSource({"padz": tmp_path / "padz"})
    rc = e2e_verb.run((), source=source, run_harness=rec)
    assert rc == 1
    # Fail-fast: checked before any (expensive) build — nothing resolved.
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


# --------------------------------------------------------------------------
# The exit contract (ADR-0030), shared with test/build
# --------------------------------------------------------------------------


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
    # The broken job never reached its harness; the healthy one still ran.
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
    assert source.resolved == []  # rejected before any build
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
    # An e2e artifact with no binary-producing target: ConfigError through
    # the cli_errors shell — `error: …` + rc 1 (with the real source; the
    # pure rule lives in tools/e2e and fires inside resolve()).
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


# --------------------------------------------------------------------------
# The exec boundary — the stated timeout and env ride the wire (ADR-0028)
# --------------------------------------------------------------------------


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
    # An e2e suite is a legitimate long-runner: the bound is the verb's own
    # (an hour), stated on the wire, never the runner's default; the env is
    # MERGED over the parent's (the runner's default), so the harness keeps
    # PATH etc. and gains only the injection.
    assert captured["timeout"] == e2e_verb.E2E_TIMEOUT
    assert captured["check"] is False
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"] == {"PADZ_BIN": "/x/padz"}


# --------------------------------------------------------------------------
# Full stack: the real local-build source + a real bats-shaped consumer
# --------------------------------------------------------------------------


def test_full_local_flow_builds_injects_and_runs_the_harness(
    tmp_path, monkeypatch, capsys
):
    # The whole seam, no fakes: a rust-declared artifact whose build command
    # is overridden to a no-op (the binary is pre-placed where cargo would
    # leave it), and a check-e2e script that PASSES only if <NAME>_BIN is an
    # executable absolute path — the legacy consumer contract end to end.
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
