"""The artifact-source seam's local-build source (TOL01-WS03).

Recorded-invocation tests through the injected step runner (prior art: the
build verb tests over the one-exec seam, ADR-0028): the source drives the
SAME WS02 build join `shipit build` runs — narrowed builder argv, direct
(never pixi-wrapped), only the artifact's own toolchains — and returns the
built binary's absolute, verified path; every could-not-produce outcome is
a loud ArtifactSourceError, never a quiet skip.
"""

import os
from pathlib import Path

import pytest

from shipit import config, execrun
from shipit.tools import artifact_source


class _Recorder:
    """A fake step runner: records (argv, cwd, env), returns scripted
    outcomes keyed by binary name (int rc, `(rc, output)`, or an exception);
    unmapped binaries succeed."""

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
    # The WS02 build join, byte-for-byte: the narrowed release build.
    assert rec.calls == [
        (("cargo", "build", "--release", "-p", "app"), tmp_path, {}),
    ]
    assert resolved == binary.resolve()
    assert resolved.is_absolute()
    # The source reports its steps and the builder's verbatim output.
    assert "e2e: build rust (.) [app]: cargo build --release -p app" in lines
    assert "cargo ran" in lines


def test_resolve_builds_every_declared_target_but_only_the_artifacts_legs(tmp_path):
    # A Tauri-shaped repo: rust + npm are the artifact's targets and both
    # build; the python leg is NOT the artifact's and never runs.
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
    # PRD story 9 (pixi provisions, never builds) and ADR-0041: e2e builds
    # the working tree's binary — no version is supplied, so a go target's
    # declared version-var never produces a -X injection here.
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


def test_failed_build_step_raises_naming_the_step_and_rc(tmp_path):
    rec = _Recorder(outcomes={"cargo": (101, "compile error")})
    source, lines = _source(tmp_path, (_entry(".", "rust"),), rec)
    with pytest.raises(
        artifact_source.ArtifactSourceError,
        match=r"artifact app failed: rust \(\.\) \[app\].*exited 101",
    ):
        source.resolve(_rust_artifact())
    # The builder's output still surfaced before the refusal.
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
    # A Tauri-shaped artifact declares rust + npm, but only rust is mapped:
    # the npm target would be SILENTLY dropped by the leg narrowing and the
    # harness would still run against a partial build. The source refuses it
    # loudly — the same orphan-target gate `shipit build` runs — and no
    # builder is invoked (checked against the WHOLE map, before planning).
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
    # The artifact targets `rust`, but the map carries TWO rust legs: the join
    # keys on toolchain (ADR-0007), so the build would run in BOTH paths' cwd
    # while `binary_location` verifies only the first — the wrong-cwd build the
    # `shipit build` verb also refuses. The source applies the SAME guard
    # before planning, so e2e's build really is the join `shipit build` runs;
    # no builder is invoked.
    _place_binary(tmp_path, "target/release/app")
    rec = _Recorder()
    source, _ = _source(
        tmp_path, (_entry("svc-a", "rust"), _entry("svc-b", "rust")), rec
    )
    with pytest.raises(config.ConfigError, match=r"ambiguous.*rust \(2 paths\)"):
        source.resolve(_rust_artifact())
    assert rec.calls == []


def test_declaration_inconsistencies_surface_as_config_errors(tmp_path):
    # ConfigError from the pure rules surfaces through the seam untouched.
    # An e2e artifact with no binary-producing target (npm mapped, so the
    # orphan gate passes and binary_location does the refusing):
    source, _ = _source(tmp_path, (_entry(".", "npm"),), _Recorder())
    no_binary = config.Artifact(
        name="site", build=(config.BuildTarget("npm"),), e2e=config.E2eSpec()
    )
    with pytest.raises(config.ConfigError, match="no binary-producing"):
        source.resolve(no_binary)
    # A target whose toolchain has no map leg: the shared orphan gate catches
    # it first (before binary_location), naming `<artifact> -> <toolchain>`.
    with pytest.raises(config.ConfigError, match=r"no \[toolchains\] leg.*app -> rust"):
        source.resolve(_rust_artifact())


def test_the_seam_signature_is_the_wf02_boundary():
    # PRD story 12, pinned structurally: the protocol's one method takes the
    # artifact declaration and returns a Path — later sources (CI-artifact
    # download, the content-key store) implement exactly this.
    assert isinstance(
        artifact_source.LocalBuildSource(
            root=Path("."), entries=(), run_step=_Recorder()
        ),
        artifact_source.ArtifactSource,
    )


def test_os_access_x_ok_is_the_executability_check(tmp_path):
    # Sanity-pin the platform assumption the two refusal tests rely on.
    binary = _place_binary(tmp_path, "bin")
    assert os.access(binary, os.X_OK)
    binary.chmod(0o644)
    assert not os.access(binary, os.X_OK)
