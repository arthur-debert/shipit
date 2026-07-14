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


def test_go_leg_without_artifacts_builds_every_package():
    # #608 (supage's red build cell): a bare `go build` compiles only the root
    # package — "no Go files in ." on a repo whose packages live under cmd/…
    # — so the whole-leg default targets ./... like the test slot does.
    (step,) = build_mod.plan_build([_leg("go")], [])
    assert step.argv == ("go", "build", "-trimpath", "-ldflags", "-s -w", "./...")


def test_go_whole_leg_build_keeps_all_packages_last_after_passthrough():
    # #608 review: plan_legs appends passthrough VERBATIM after the leg argv, so
    # a flag forwarded to a whole-leg go build lands after the registry
    # default's ./... — where `go build` reads it as another package and errors.
    # The build planner must keep the whole-tree pattern LAST.
    (leg,) = legs_mod.plan_legs(
        [_entry("go")], tool="build", selector=None, passthrough=("-v",)
    )
    (step,) = build_mod.plan_build([leg], [])
    assert step.argv == ("go", "build", "-trimpath", "-ldflags", "-s -w", "-v", "./...")


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


def test_go_target_package_path_lands_last_replacing_the_whole_tree_target():
    # The artifact's package SUPERSEDES the default ./... target — a narrowed
    # step builds exactly that unit, never the union of tree and package.
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


def test_go_target_without_a_package_builds_the_module_root():
    # The dodot shape: a go artifact with no declared package is the module
    # root's binary. The whole-tree ./... default must still be dropped — go
    # discards binaries when several packages compile at once, so leaving it
    # would build green yet write no binary for the e2e local-build source.
    (step,) = build_mod.plan_build(
        [_leg("go")], [_artifact("dodot", config.BuildTarget(toolchain="go"))]
    )
    assert step.argv == ("go", "build", "-trimpath", "-ldflags", "-s -w")
    assert step.artifact == "dodot"


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


def test_injection_extends_the_last_ldflags_when_several_are_present():
    # go takes the LAST -ldflags, so when an override/passthrough adds a second
    # one the injection must ride THAT one — extending an earlier flag would let
    # go's own last-wins rule silently discard the injected -X.
    leg = _leg("go", argv=("go", "build", "-ldflags", "-s -w", "-ldflags=-w"))
    (step,) = build_mod.plan_build(
        [leg],
        [_artifact("x", config.BuildTarget(toolchain="go", version_var="p.V"))],
        version="9.9.9",
    )
    assert step.argv == (
        "go",
        "build",
        "-ldflags",
        "-s -w",
        "-ldflags=-w -X p.V=9.9.9",
    )


# --------------------------------------------------------------------------
# Passthrough interplay: the planner narrows AFTER plan_legs appended args
# --------------------------------------------------------------------------


def test_passthrough_args_stay_ahead_of_the_go_package_path():
    # plan_legs appends passthrough to the leg argv; go's package path must
    # still land last (flags precede the package on the go command line), and
    # the default ./... target is dropped from WHEREVER passthrough left it —
    # never carried alongside the narrowing package.
    leg = _leg("go", argv=(*registry.GO.command("build"), "-v"))
    (step,) = build_mod.plan_build(
        [leg], [_artifact("x", config.BuildTarget(toolchain="go", package="./cmd/x"))]
    )
    assert step.argv[-2:] == ("-v", "./cmd/x")
    assert "./..." not in step.argv


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


# --------------------------------------------------------------------------
# check_targets_unambiguous: the shared ambiguous-path gate (verb + e2e source)
# --------------------------------------------------------------------------


def test_check_targets_unambiguous_passes_when_each_toolchain_is_one_leg():
    # One leg per targeted toolchain -> silent (returns None). An untargeted
    # toolchain mapped to several legs is fine — the gate only guards targets.
    build_mod.check_targets_unambiguous(
        [_artifact("app", config.BuildTarget("rust"))],
        [_leg("rust"), _leg("go", path="svc-a"), _leg("go", path="svc-b")],
    )


def test_check_targets_unambiguous_refuses_a_toolchain_on_multiple_legs():
    # A targeted toolchain mapped to more than one planned leg has no single
    # producing path: refused loudly, naming the toolchain and the leg count.
    with pytest.raises(config.ConfigError, match=r"ambiguous.*go \(2 paths\)"):
        build_mod.check_targets_unambiguous(
            [_artifact("x", config.BuildTarget("go", package="./cmd/x"))],
            [_leg("go", path="svc-a"), _leg("go", path="svc-b")],
        )


# --------------------------------------------------------------------------
# Cross `--target <triple>`: the rust-only triple-dir redirect (TOL02-WS11)
# --------------------------------------------------------------------------


def test_target_appends_cargo_target_to_a_whole_leg_rust_build():
    # `--target <triple>` cross-compiles the whole rust leg — cargo then writes
    # target/<triple>/release/ (the cross platforms a native runner cannot
    # build natively: darwin-x86_64, musl). Appended LAST, after the base.
    (step,) = build_mod.plan_build(
        [_leg("rust")], [], target="x86_64-unknown-linux-musl"
    )
    assert step.argv == (
        "cargo",
        "build",
        "--release",
        "--target",
        "x86_64-unknown-linux-musl",
    )


def test_target_appends_cargo_target_after_artifact_narrowing():
    # The narrowed build (`-p <package>`) still gains --target — the artifact's
    # unit AND the cross triple both ride the one cargo invocation.
    (step,) = build_mod.plan_build(
        [_leg("rust")],
        [_artifact("lex", config.BuildTarget("rust", package="lex-cli"))],
        target="x86_64-pc-windows-msvc",
    )
    assert step.argv == (
        "cargo",
        "build",
        "--release",
        "-p",
        "lex-cli",
        "--target",
        "x86_64-pc-windows-msvc",
    )


def test_target_is_a_no_op_for_non_rust_toolchains():
    # go cross-compiles by GOOS/GOARCH, python/npm have no per-target build —
    # so --target touches ONLY rust legs; the others pass through untouched.
    go_step, py_step = build_mod.plan_build(
        [_leg("go"), _leg("python", path="pkg")], [], target="x86_64-apple-darwin"
    )
    assert "--target" not in go_step.argv
    assert py_step.argv == ("uv", "build")


def test_no_target_keeps_the_native_build():
    # The default (target=None) is the native build — no --target, cargo writes
    # target/release/ (the native local + native-runner path).
    (step,) = build_mod.plan_build([_leg("rust")], [])
    assert "--target" not in step.argv
