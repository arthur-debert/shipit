"""The build-step planner (TOL01-WS02) — pure fixture-driven coverage.

The leg×artifact join and its per-toolchain shaping rules: target narrowing
(rust ``-p``, go package-last, npm ``--workspace``), go's static env, and the
ADR-0041 version injection (``-X`` rides the existing ``-ldflags`` value, and
appears ONLY when a version is supplied AND the target declares its var).
Prior art: the leg planner tests — no I/O, values in, values out.
"""

import pytest

from shipit import config
from shipit.tools import build as build_mod
from shipit.tools import legs as legs_mod
from shipit.tools import registry


def _entry(toolchain: str, path: str = ".") -> config.ToolchainEntry:
    return config.ToolchainEntry(path=path, toolchain=toolchain, commands={})


def _leg(toolchain: str, path: str = ".", argv: tuple[str, ...] = ()) -> legs_mod.Leg:
    return legs_mod.Leg(
        path=path,
        toolchain=toolchain,
        tool="build",
        argv=argv or registry.toolchain(toolchain).command("build"),
    )


def _artifact(name: str, *targets: config.BuildTarget) -> config.Artifact:
    return config.Artifact(name=name, build=tuple(targets))


# --------------------------------------------------------------------------
# The no-artifact fallback: a leg builds whole, once
# --------------------------------------------------------------------------


def test_leg_without_artifacts_runs_its_base_command_once():
    (step,) = build_mod.plan_build([_leg("python")], [])
    assert step.argv == ("uv", "build")
    assert step.artifact is None
    assert step.env == ()
    assert step.label == "python (.)"


def test_leg_order_is_the_map_order_and_legs_without_targets_still_build():
    steps = build_mod.plan_build(
        [_leg("rust"), _leg("npm", path="web")],
        [_artifact("cli", config.BuildTarget(toolchain="rust", package="cli"))],
    )
    assert [s.label for s in steps] == ["rust (.) [cli]", "npm (web)"]


# --------------------------------------------------------------------------
# Target narrowing per toolchain
# --------------------------------------------------------------------------


def test_rust_target_package_appends_dash_p():
    (step,) = build_mod.plan_build(
        [_leg("rust")],
        [_artifact("lex-cli", config.BuildTarget(toolchain="rust", package="lex-cli"))],
    )
    assert step.argv == ("cargo", "build", "--release", "-p", "lex-cli")
    assert step.artifact == "lex-cli"


def test_two_artifacts_from_one_rust_workspace_are_two_steps():
    # ADR-0007's many-to-many: one workspace -> a CLI and an LSP binary.
    steps = build_mod.plan_build(
        [_leg("rust")],
        [
            _artifact("cli", config.BuildTarget(toolchain="rust", package="cli")),
            _artifact("lsp", config.BuildTarget(toolchain="rust", package="lsp")),
        ],
    )
    assert [s.argv[-1] for s in steps] == ["cli", "lsp"]
    assert [s.artifact for s in steps] == ["cli", "lsp"]


def test_go_target_package_path_lands_last():
    (step,) = build_mod.plan_build(
        [_leg("go")],
        [_artifact("mycli", config.BuildTarget(toolchain="go", package="./cmd/mycli"))],
    )
    assert step.argv == (
        "go",
        "build",
        "-trimpath",
        "-ldflags",
        "-s -w",
        "./cmd/mycli",
    )


def test_npm_target_package_is_the_workspace():
    (step,) = build_mod.plan_build(
        [_leg("npm")],
        [_artifact("web", config.BuildTarget(toolchain="npm", package="web"))],
    )
    assert step.argv == ("npm", "run", "build", "--workspace", "web")


def test_bare_toolchain_target_keeps_the_base_command():
    (step,) = build_mod.plan_build(
        [_leg("python")], [_artifact("dist", config.BuildTarget(toolchain="python"))]
    )
    assert step.argv == ("uv", "build")
    assert step.artifact == "dist"


# --------------------------------------------------------------------------
# go: the static env and the supplied-version injection (ADR-0041)
# --------------------------------------------------------------------------


def test_go_legs_carry_the_static_env_and_others_do_not():
    steps = build_mod.plan_build([_leg("go"), _leg("rust", path="cli")], [])
    assert steps[0].env == (("CGO_ENABLED", "0"),)
    assert steps[1].env == ()


def test_supplied_version_rides_the_existing_ldflags_value():
    # go takes the LAST -ldflags, so -X must extend the value, never add a
    # second flag that would drop the strip flags.
    (step,) = build_mod.plan_build(
        [_leg("go")],
        [
            _artifact(
                "mycli",
                config.BuildTarget(
                    toolchain="go", package="./cmd/mycli", version_var="pkg.Version"
                ),
            )
        ],
        version="1.2.3",
    )
    assert step.argv == (
        "go",
        "build",
        "-trimpath",
        "-ldflags",
        "-s -w -X pkg.Version=1.2.3",
        "./cmd/mycli",
    )


def test_no_version_means_no_injection_even_with_a_declared_var():
    # The legacy contract: no supplied version -> the binary keeps its
    # embedded default; nothing version-shaped appears in the argv.
    (step,) = build_mod.plan_build(
        [_leg("go")],
        [_artifact("x", config.BuildTarget(toolchain="go", version_var="pkg.V"))],
    )
    assert "-X" not in " ".join(step.argv)


def test_version_without_a_declared_var_is_never_injected():
    (step,) = build_mod.plan_build(
        [_leg("go")],
        [_artifact("x", config.BuildTarget(toolchain="go"))],
        version="1.2.3",
    )
    assert "1.2.3" not in " ".join(step.argv)


def test_version_never_touches_non_go_legs():
    (step,) = build_mod.plan_build(
        [_leg("rust")],
        [_artifact("cli", config.BuildTarget(toolchain="rust", package="cli"))],
        version="1.2.3",
    )
    assert "1.2.3" not in " ".join(step.argv)


def test_injection_into_an_override_without_ldflags_appends_the_flag():
    # A per-path override may drop -ldflags entirely; injection then appends
    # a fresh flag rather than silently skipping the supplied version.
    leg = _leg("go", argv=("go", "build"))
    (step,) = build_mod.plan_build(
        [leg],
        [_artifact("x", config.BuildTarget(toolchain="go", version_var="p.V"))],
        version="2.0.0",
    )
    assert step.argv == ("go", "build", "-ldflags", "-X p.V=2.0.0")


def test_injection_extends_a_joined_form_ldflags_value():
    # A per-path override may use go's joined -ldflags=<value> single-token
    # spelling (here also LAST in the argv). The -X must ride that value too,
    # never append a second -ldflags that go would let win — dropping -s -w.
    leg = _leg("go", argv=("go", "build", "-ldflags=-s -w"))
    (step,) = build_mod.plan_build(
        [leg],
        [_artifact("x", config.BuildTarget(toolchain="go", version_var="p.V"))],
        version="3.1.0",
    )
    assert step.argv == ("go", "build", "-ldflags=-s -w -X p.V=3.1.0")


# --------------------------------------------------------------------------
# Passthrough interplay: the planner narrows AFTER plan_legs appended args
# --------------------------------------------------------------------------


def test_passthrough_args_stay_ahead_of_the_go_package_path():
    # plan_legs appends passthrough to the leg argv; go's package path must
    # still land last (flags precede the package on the go command line).
    leg = _leg("go", argv=(*registry.GO.command("build"), "-v"))
    (step,) = build_mod.plan_build(
        [leg], [_artifact("x", config.BuildTarget(toolchain="go", package="./cmd/x"))]
    )
    assert step.argv[-2:] == ("-v", "./cmd/x")


# --------------------------------------------------------------------------
# check_targets_mapped: the shared orphan-target gate (verb + e2e source)
# --------------------------------------------------------------------------


def test_check_targets_mapped_passes_when_every_target_has_a_leg():
    # All targets mapped -> silent (returns None); the whole map is consulted.
    build_mod.check_targets_mapped(
        [_artifact("app", config.BuildTarget("rust"), config.BuildTarget("npm"))],
        [_entry("rust"), _entry("npm", path="web")],
    )


def test_check_targets_mapped_refuses_an_orphaned_target_naming_it():
    # A target whose toolchain has no [toolchains] leg would silently never
    # build: refused loudly, naming `<artifact> -> <toolchain>`.
    with pytest.raises(config.ConfigError, match=r"no \[toolchains\] leg.*app -> npm"):
        build_mod.check_targets_mapped(
            [_artifact("app", config.BuildTarget("rust"), config.BuildTarget("npm"))],
            [_entry("rust")],
        )
