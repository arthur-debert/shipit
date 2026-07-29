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


def test_leg_without_artifacts_runs_its_base_command_once():
    (step,) = build_mod.plan_build([_leg("python")], [])
    assert step.argv == ("uv", "build")
    assert step.artifact is None
    assert step.env == ()
    assert step.label == "python (.)"


def test_buildless_lua_leg_is_skipped():
    assert build_mod.plan_build([_leg("lua")], []) == ()


def test_buildless_lua_leg_is_skipped_but_sibling_legs_still_build():
    steps = build_mod.plan_build([_leg("lua"), _leg("python")], [])
    assert [step.argv for step in steps] == [("uv", "build")]


def test_artifact_targeting_a_buildless_toolchain_is_a_loud_error():
    artifact = _artifact("plugin", config.BuildTarget(toolchain="lua", package=None))
    with pytest.raises(config.ConfigError, match="buildless toolchain 'lua'"):
        build_mod.plan_build([_leg("lua")], [artifact])


def test_go_leg_without_artifacts_builds_every_package():
    (step,) = build_mod.plan_build([_leg("go")], [])
    assert step.argv == ("go", "build", "-trimpath", "-ldflags", "-s -w", "./...")


def test_go_whole_leg_build_keeps_all_packages_last_after_passthrough():
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


def test_rust_target_package_appends_dash_p():
    (step,) = build_mod.plan_build(
        [_leg("rust")],
        [_artifact("lex-cli", config.BuildTarget(toolchain="rust", package="lex-cli"))],
    )
    assert step.argv == ("cargo", "build", "--release", "-p", "lex-cli")
    assert step.artifact == "lex-cli"


def test_two_artifacts_from_one_rust_workspace_are_two_steps():
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


def test_go_legs_carry_the_static_env_and_others_do_not():
    steps = build_mod.plan_build([_leg("go"), _leg("rust", path="cli")], [])
    assert steps[0].env == (("CGO_ENABLED", "0"),)
    assert steps[1].env == ()


def test_supplied_version_rides_the_existing_ldflags_value():
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
    leg = _leg("go", argv=("go", "build"))
    (step,) = build_mod.plan_build(
        [leg],
        [_artifact("x", config.BuildTarget(toolchain="go", version_var="p.V"))],
        version="2.0.0",
    )
    assert step.argv == ("go", "build", "-ldflags", "-X p.V=2.0.0")


def test_injection_extends_a_joined_form_ldflags_value():
    leg = _leg("go", argv=("go", "build", "-ldflags=-s -w"))
    (step,) = build_mod.plan_build(
        [leg],
        [_artifact("x", config.BuildTarget(toolchain="go", version_var="p.V"))],
        version="3.1.0",
    )
    assert step.argv == ("go", "build", "-ldflags=-s -w -X p.V=3.1.0")


def test_injection_extends_the_last_ldflags_when_several_are_present():
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


def test_passthrough_args_stay_ahead_of_the_go_package_path():
    leg = _leg("go", argv=(*registry.GO.command("build"), "-v"))
    (step,) = build_mod.plan_build(
        [leg], [_artifact("x", config.BuildTarget(toolchain="go", package="./cmd/x"))]
    )
    assert step.argv[-2:] == ("-v", "./cmd/x")
    assert "./..." not in step.argv


def test_check_targets_mapped_passes_when_every_target_has_a_leg():
    build_mod.check_targets_mapped(
        [_artifact("app", config.BuildTarget("rust"), config.BuildTarget("npm"))],
        [_entry("rust"), _entry("npm", path="web")],
    )


def test_check_targets_mapped_refuses_an_orphaned_target_naming_it():
    with pytest.raises(config.ConfigError, match=r"no \[toolchains\] leg.*app -> npm"):
        build_mod.check_targets_mapped(
            [_artifact("app", config.BuildTarget("rust"), config.BuildTarget("npm"))],
            [_entry("rust")],
        )


def test_check_targets_unambiguous_passes_when_each_toolchain_is_one_leg():
    build_mod.check_targets_unambiguous(
        [_artifact("app", config.BuildTarget("rust"))],
        [_leg("rust"), _leg("go", path="svc-a"), _leg("go", path="svc-b")],
    )


def test_check_targets_unambiguous_refuses_a_toolchain_on_multiple_legs():
    with pytest.raises(config.ConfigError, match=r"ambiguous.*go \(2 paths\)"):
        build_mod.check_targets_unambiguous(
            [_artifact("x", config.BuildTarget("go", package="./cmd/x"))],
            [_leg("go", path="svc-a"), _leg("go", path="svc-b")],
        )


def test_target_appends_cargo_target_to_a_whole_leg_rust_build():
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
    go_step, py_step = build_mod.plan_build(
        [_leg("go"), _leg("python", path="pkg")], [], target="x86_64-apple-darwin"
    )
    assert "--target" not in go_step.argv
    assert py_step.argv == ("uv", "build")


def test_no_target_keeps_the_native_build():
    (step,) = build_mod.plan_build([_leg("rust")], [])
    assert "--target" not in step.argv
