import os
from pathlib import Path

import pytest

from shipit import config, execrun
from shipit.tools import artifact_source


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


def _entry(path, toolchain):
    return config.ToolchainEntry(path=path, toolchain=toolchain, commands={})


def _rust_artifact(name="app", package="app"):
    return config.Artifact(
        name=name,
        build=(config.BuildTarget("rust", package=package),),
        e2e=config.E2eSpec(),
    )


def _place_binary(root: Path, relpath: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _source(root, entries, run_step, echo=None):
    lines = [] if echo is None else echo
    return (
        artifact_source.LocalBuildSource(
            root=root, entries=entries, run_step=run_step, echo=lines.append
        ),
        lines,
    )


def test_resolve_builds_the_artifact_and_returns_the_absolute_binary(tmp_path):
    binary = _place_binary(tmp_path, "target/release/app")
    rec = _Recorder()
    source, lines = _source(tmp_path, (_entry(".", "rust"),), rec)
    resolved = source.resolve(_rust_artifact())
    assert rec.calls == [
        (("cargo", "build", "--release", "-p", "app"), tmp_path, {}),
    ]
    assert resolved == binary.resolve()
    assert resolved.is_absolute()
    assert "e2e: build rust (.) [app]: cargo build --release -p app" in lines
    assert "cargo ran" in lines


def test_resolve_builds_every_declared_target_but_only_the_artifacts_legs(tmp_path):
    _place_binary(tmp_path, "target/release/app")
    (tmp_path / "web").mkdir()
    entries = (_entry(".", "rust"), _entry("web", "npm"), _entry("docs", "python"))
    artifact = config.Artifact(
        name="app",
        build=(config.BuildTarget("rust", package="app"), config.BuildTarget("npm")),
        e2e=config.E2eSpec(),
    )
    rec = _Recorder()
    source, _ = _source(tmp_path, entries, rec)
    source.resolve(artifact)
    assert [(argv[0], cwd) for argv, cwd, _ in rec.calls] == [
        ("cargo", tmp_path),
        ("npm", tmp_path / "web"),
    ]


def test_resolve_never_wraps_the_builder_in_pixi_and_supplies_no_version(tmp_path):
    artifact = config.Artifact(
        name="padz",
        build=(
            config.BuildTarget("go", package="./cmd/padz", version_var="main.version"),
        ),
        e2e=config.E2eSpec(),
    )
    _place_binary(tmp_path, "padz")
    rec = _Recorder()
    source, _ = _source(tmp_path, (_entry(".", "go"),), rec)
    source.resolve(artifact)
    ((argv, _, env),) = rec.calls
    assert argv[0] == "go"
    assert not any("-X" in a for a in argv)
    assert env == {"CGO_ENABLED": "0"}


def test_builder_output_is_echoed_verbatim(tmp_path):
    _place_binary(tmp_path, "target/release/app")
    rec = _Recorder(outcomes={"cargo": (0, "warning: unused  \n")})
    source, lines = _source(tmp_path, (_entry(".", "rust"),), rec)
    source.resolve(_rust_artifact())
    assert "warning: unused  " in lines


def test_failed_build_step_raises_naming_the_step_and_rc(tmp_path):
    rec = _Recorder(outcomes={"cargo": (101, "compile error")})
    source, lines = _source(tmp_path, (_entry(".", "rust"),), rec)
    with pytest.raises(
        artifact_source.ArtifactSourceError,
        match=r"artifact app failed: rust \(\.\) \[app\].*exited 101",
    ):
        source.resolve(_rust_artifact())
    assert "compile error" in lines


def test_missing_builder_raises_the_hard_provision_note(tmp_path):
    boom = execrun.ExecError(["cargo"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _Recorder(outcomes={"cargo": boom})
    source, _ = _source(tmp_path, (_entry(".", "rust"),), rec)
    with pytest.raises(artifact_source.ArtifactSourceError, match="not found on PATH"):
        source.resolve(_rust_artifact())


def test_green_build_with_no_binary_at_the_expected_path_is_refused(tmp_path):
    source, _ = _source(tmp_path, (_entry(".", "rust"),), _Recorder())
    with pytest.raises(
        artifact_source.ArtifactSourceError,
        match=r"built green but its binary is not at .*target/release/app",
    ):
        source.resolve(_rust_artifact())


def test_non_executable_binary_is_refused(tmp_path):
    binary = _place_binary(tmp_path, "target/release/app")
    binary.chmod(0o644)
    source, _ = _source(tmp_path, (_entry(".", "rust"),), _Recorder())
    with pytest.raises(artifact_source.ArtifactSourceError, match="not executable"):
        source.resolve(_rust_artifact())


def test_orphaned_build_target_toolchain_is_refused_before_any_build(tmp_path):
    _place_binary(tmp_path, "target/release/app")
    artifact = config.Artifact(
        name="app",
        build=(config.BuildTarget("rust", package="app"), config.BuildTarget("npm")),
        e2e=config.E2eSpec(),
    )
    rec = _Recorder()
    source, _ = _source(tmp_path, (_entry(".", "rust"),), rec)
    with pytest.raises(config.ConfigError, match=r"no \[toolchains\] leg.*app -> npm"):
        source.resolve(artifact)
    assert rec.calls == []


def test_ambiguous_producing_path_is_refused_before_any_build(tmp_path):
    _place_binary(tmp_path, "target/release/app")
    rec = _Recorder()
    source, _ = _source(
        tmp_path, (_entry("svc-a", "rust"), _entry("svc-b", "rust")), rec
    )
    with pytest.raises(config.ConfigError, match=r"ambiguous.*rust \(2 paths\)"):
        source.resolve(_rust_artifact())
    assert rec.calls == []


def test_declaration_inconsistencies_surface_as_config_errors(tmp_path):
    source, _ = _source(tmp_path, (_entry(".", "npm"),), _Recorder())
    no_binary = config.Artifact(
        name="site", build=(config.BuildTarget("npm"),), e2e=config.E2eSpec()
    )
    with pytest.raises(config.ConfigError, match="no binary-producing"):
        source.resolve(no_binary)
    with pytest.raises(config.ConfigError, match=r"no \[toolchains\] leg.*app -> rust"):
        source.resolve(_rust_artifact())


def test_the_seam_signature_is_the_wf02_boundary():
    assert isinstance(
        artifact_source.LocalBuildSource(
            root=Path("."), entries=(), run_step=_Recorder()
        ),
        artifact_source.ArtifactSource,
    )


def test_os_access_x_ok_is_the_executability_check(tmp_path):
    binary = _place_binary(tmp_path, "bin")
    assert os.access(binary, os.X_OK)
    binary.chmod(0o644)
    assert not os.access(binary, os.X_OK)
