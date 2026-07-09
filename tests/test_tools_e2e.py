"""The pure e2e planner (TOL01-WS03): `<NAME>_BIN` derivation pinned to the
legacy `tr` contract, the harness registry's bats default, declaration-is-
opt-in job planning with the ADR-0039 selector/passthrough rules on the
artifact axis, and the declaration-derived binary location.
"""

import pytest

from shipit import config
from shipit.tools import e2e as e2e_mod


def _artifact(name, *, build=(), e2e=None):
    return config.Artifact(name=name, build=tuple(build), e2e=e2e)


def _entry(path, toolchain):
    return config.ToolchainEntry(path=path, toolchain=toolchain, commands={})


# --------------------------------------------------------------------------
# <NAME>_BIN derivation — the legacy `tr '[:lower:]-' '[:upper:]_'` contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "var"),
    [
        # The fleet's two live consumers, byte-for-byte (issue #556).
        ("padz", "PADZ_BIN"),
        ("dodot", "DODOT_BIN"),
        # `-` -> `_`, exactly as tr translated it.
        ("lex-cli", "LEX_CLI_BIN"),
        # tr touches ONLY ascii lowercase and `-`: digits, existing capitals,
        # and any other character pass through unchanged.
        ("tool2", "TOOL2_BIN"),
        ("MyTool", "MYTOOL_BIN"),
    ],
)
def test_bin_env_var_matches_the_legacy_tr_derivation(name, var):
    assert e2e_mod.bin_env_var(name) == var


# --------------------------------------------------------------------------
# plan_e2e — declaring `e2e` is the opt-in (PRD story 11)
# --------------------------------------------------------------------------


def test_bare_invocation_with_no_e2e_declaration_plans_no_jobs():
    # A BARE invocation over an artifact map without any `e2e` table has NO
    # e2e lane: an empty plan (the verb's clean "nothing to run"), never an
    # error. This clean empty exit is EXCLUSIVE to the bare invocation.
    artifacts = (_artifact("cli", build=(config.BuildTarget("rust"),)),)
    assert e2e_mod.plan_e2e(artifacts) == ()


def test_explicit_selector_on_a_repo_with_no_e2e_is_a_usage_error():
    # An EXPLICIT selector is a usage claim, never the clean no-op: asking
    # for `padz` when NO artifact declares e2e (padz forgot its `e2e` table)
    # must fail as usage, not exit 0 green — otherwise CI silently no-ops.
    artifacts = (_artifact("padz", build=(config.BuildTarget("rust"),)),)
    with pytest.raises(e2e_mod.E2ePlanError, match=r"'padz'.*no artifact.*e2e table"):
        e2e_mod.plan_e2e(artifacts, selector="padz")


def test_bare_e2e_table_opts_in_with_the_registry_default_harness():
    artifacts = (_artifact("padz", e2e=config.E2eSpec(harness=None)),)
    (job,) = e2e_mod.plan_e2e(artifacts)
    assert job.harness == ("bin/check-e2e",)
    assert job.harness == e2e_mod.DEFAULT_HARNESS.argv
    assert job.env_var == "PADZ_BIN"
    assert job.label == "padz"


def test_declared_harness_replaces_the_default_for_that_artifact_only():
    artifacts = (
        _artifact("a", e2e=config.E2eSpec(harness=("bats", "tests/e2e.bats"))),
        _artifact("b", e2e=config.E2eSpec(harness=None)),
    )
    jobs = e2e_mod.plan_e2e(artifacts)
    assert [j.harness for j in jobs] == [
        ("bats", "tests/e2e.bats"),
        ("bin/check-e2e",),
    ]


def test_jobs_follow_artifact_declaration_order_skipping_non_declaring():
    artifacts = (
        _artifact("one", e2e=config.E2eSpec()),
        _artifact("no-e2e"),
        _artifact("two", e2e=config.E2eSpec()),
    )
    assert [j.label for j in e2e_mod.plan_e2e(artifacts)] == ["one", "two"]


def test_selector_picks_one_artifact():
    artifacts = (
        _artifact("a", e2e=config.E2eSpec()),
        _artifact("b", e2e=config.E2eSpec()),
    )
    (job,) = e2e_mod.plan_e2e(artifacts, selector="b")
    assert job.label == "b"


def test_unknown_selector_is_a_plan_error_naming_the_declared_artifacts():
    artifacts = (_artifact("padz", e2e=config.E2eSpec()),)
    with pytest.raises(e2e_mod.E2ePlanError, match=r"'dodot'.*padz"):
        e2e_mod.plan_e2e(artifacts, selector="dodot")


def test_passthrough_appends_verbatim_to_the_single_selected_harness():
    artifacts = (_artifact("padz", e2e=config.E2eSpec()),)
    (job,) = e2e_mod.plan_e2e(artifacts, passthrough=("--tap",))
    assert job.harness == ("bin/check-e2e", "--tap")


def test_passthrough_over_several_jobs_is_a_hard_error_never_a_broadcast():
    artifacts = (
        _artifact("a", e2e=config.E2eSpec()),
        _artifact("b", e2e=config.E2eSpec()),
    )
    with pytest.raises(e2e_mod.E2ePlanError, match="exactly one"):
        e2e_mod.plan_e2e(artifacts, passthrough=("--tap",))
    # A selector narrows it back to legal.
    (job,) = e2e_mod.plan_e2e(artifacts, selector="a", passthrough=("--tap",))
    assert job.harness[-1] == "--tap"


def test_passthrough_over_a_repo_with_no_e2e_is_a_usage_error_not_a_no_op():
    # Passthrough is a usage claim that exactly one artifact receives it. Over a
    # repo where NO artifact declares e2e (zero jobs), `shipit e2e -- --tap`
    # must fail as usage, NOT take the bare clean-no-op path — otherwise a
    # misconfigured CI lane hides as a green exit 0.
    artifacts = (_artifact("cli", build=(config.BuildTarget("rust"),)),)
    with pytest.raises(e2e_mod.E2ePlanError, match=r"exactly one.*declares no e2e"):
        e2e_mod.plan_e2e(artifacts, passthrough=("--tap",))


# --------------------------------------------------------------------------
# binary_location — declaration-derived, filesystem-free
# --------------------------------------------------------------------------


def test_rust_binary_lands_in_target_release_named_by_the_package():
    artifact = _artifact(
        "app",
        build=(config.BuildTarget("rust", package="app-cli"),),
        e2e=config.E2eSpec(),
    )
    loc = e2e_mod.binary_location(artifact, (_entry(".", "rust"),))
    assert loc == e2e_mod.BinaryLocation(leg_path=".", relpath="target/release/app-cli")


def test_rust_binary_without_a_package_is_named_by_the_artifact():
    artifact = _artifact("mytool", build=(config.BuildTarget("rust"),))
    loc = e2e_mod.binary_location(artifact, (_entry("core", "rust"),))
    assert loc.leg_path == "core"
    assert loc.relpath == "target/release/mytool"


def test_go_binary_is_the_built_package_basename_in_the_leg_path():
    # `go build ./cmd/padz` writes `padz` into its cwd — the leg's path.
    artifact = _artifact(
        "padz", build=(config.BuildTarget("go", package="./cmd/padz"),)
    )
    loc = e2e_mod.binary_location(artifact, (_entry(".", "go"),))
    assert loc == e2e_mod.BinaryLocation(leg_path=".", relpath="padz")


def test_go_binary_without_a_package_is_named_by_the_artifact():
    artifact = _artifact("dodot", build=(config.BuildTarget("go"),))
    assert e2e_mod.binary_location(artifact, (_entry(".", "go"),)).relpath == "dodot"


@pytest.mark.parametrize("package", [".", "./", "/"])
def test_ambiguous_go_package_is_refused_with_a_real_diagnosis(package):
    # `.` / `./` / `/` have no basename to name the binary: fail fast with a
    # clear ConfigError, never a downstream "built green but no binary at
    # <dir>" (drop `package` to build the module root as the artifact name).
    artifact = _artifact("padz", build=(config.BuildTarget("go", package=package),))
    with pytest.raises(config.ConfigError, match=r"has no binary name.*\./cmd/padz"):
        e2e_mod.binary_location(artifact, (_entry(".", "go"),))


def test_the_first_binary_producing_target_wins_over_non_binary_ones():
    # A Tauri-shaped artifact: the npm target builds the frontend; the
    # injectable binary is the rust side's.
    artifact = _artifact(
        "app",
        build=(config.BuildTarget("npm"), config.BuildTarget("rust", package="app")),
    )
    entries = (_entry("web", "npm"), _entry(".", "rust"))
    loc = e2e_mod.binary_location(artifact, entries)
    assert loc == e2e_mod.BinaryLocation(leg_path=".", relpath="target/release/app")


def test_no_binary_producing_target_is_refused_loudly():
    artifact = _artifact("site", build=(config.BuildTarget("npm"),))
    with pytest.raises(config.ConfigError, match="no binary-producing"):
        e2e_mod.binary_location(artifact, (_entry(".", "npm"),))


def test_binary_target_without_a_map_leg_is_refused_loudly():
    # The target's toolchain has no [toolchains] leg to build on — a config
    # inconsistency (covers the empty/absent map too), never a quiet skip.
    artifact = _artifact("cli", build=(config.BuildTarget("rust"),))
    with pytest.raises(config.ConfigError, match=r"\[toolchains\] rust leg"):
        e2e_mod.binary_location(artifact, ())
