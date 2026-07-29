import pytest

from shipit import config
from shipit.tools import e2e as e2e_mod


def _artifact(name, *, build=(), e2e=None):
    return config.Artifact(name=name, build=tuple(build), e2e=e2e)


def _entry(path, toolchain):
    return config.ToolchainEntry(path=path, toolchain=toolchain, commands={})


@pytest.mark.parametrize(
    ("name", "var"),
    [
        ("padz", "PADZ_BIN"),
        ("dodot", "DODOT_BIN"),
        ("lex-cli", "LEX_CLI_BIN"),
        ("tool2", "TOOL2_BIN"),
        ("MyTool", "MYTOOL_BIN"),
    ],
)
def test_bin_env_var_matches_the_legacy_tr_derivation(name, var):
    assert e2e_mod.bin_env_var(name) == var


def test_bare_invocation_with_no_e2e_declaration_plans_no_jobs():
    artifacts = (_artifact("cli", build=(config.BuildTarget("rust"),)),)
    assert e2e_mod.plan_e2e(artifacts) == ()


def test_explicit_selector_on_a_repo_with_no_e2e_is_a_usage_error():
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


def test_the_registry_is_indexed_by_unique_name():
    assert set(e2e_mod.HARNESS_BY_NAME) == {"bats", "electron", "tauri"}
    assert all(
        e2e_mod.HARNESS_BY_NAME[name].name == name for name in e2e_mod.HARNESS_BY_NAME
    )
    assert e2e_mod.HARNESS_BY_NAME["bats"] is e2e_mod.DEFAULT_HARNESS


def test_duplicate_harness_name_is_refused_loudly_not_an_assert():
    dupe = (
        e2e_mod.BATS,
        e2e_mod.Harness("bats", argv=("other",)),
    )
    with pytest.raises(config.ConfigError, match=r"duplicate e2e harness name 'bats'"):
        e2e_mod._index_by_name(dupe)


def test_bats_default_carries_no_injected_env():
    assert e2e_mod.BATS.env == ()
    artifacts = (_artifact("padz", e2e=config.E2eSpec()),)
    (job,) = e2e_mod.plan_e2e(artifacts)
    assert job.harness == ("bin/check-e2e",)
    assert job.env == ()


def test_named_electron_harness_resolves_to_playwright_argv_and_e2e_env():
    artifacts = (_artifact("gal", e2e=config.E2eSpec(harness_name="electron")),)
    (job,) = e2e_mod.plan_e2e(artifacts)
    assert job.harness == ("npm", "exec", "--", "playwright", "test")
    assert job.env == (
        ("E2E", "1"),
        ("E2E_HIDE_WINDOW", "1"),
        ("E2E_DISABLE_PERSISTENCE", "1"),
    )
    assert job.env_var == "GAL_BIN"


def test_named_tauri_harness_resolves_to_webdriver_launch_and_the_same_env():
    artifacts = (_artifact("app", e2e=config.E2eSpec(harness_name="tauri")),)
    (job,) = e2e_mod.plan_e2e(artifacts)
    assert job.harness == ("npm", "exec", "--", "wdio", "run", "wdio.conf.ts")
    assert job.env == e2e_mod.ELECTRON.env
    assert job.env_var == "APP_BIN"


def test_named_bats_harness_selects_the_default_by_name():
    artifacts = (_artifact("cli", e2e=config.E2eSpec(harness_name="bats")),)
    (job,) = e2e_mod.plan_e2e(artifacts)
    assert job.harness == ("bin/check-e2e",)
    assert job.env == ()


def test_unknown_named_harness_is_a_config_error_naming_the_registered_ones():
    artifacts = (_artifact("x", e2e=config.E2eSpec(harness_name="qt")),)
    with pytest.raises(config.ConfigError, match=r"unknown e2e harness 'qt'.*electron"):
        e2e_mod.plan_e2e(artifacts)


def test_raw_argv_override_runs_with_no_injected_e2e_env():
    artifacts = (
        _artifact("a", e2e=config.E2eSpec(harness=("npx", "playwright", "test"))),
    )
    (job,) = e2e_mod.plan_e2e(artifacts)
    assert job.harness == ("npx", "playwright", "test")
    assert job.env == ()


def test_named_harness_env_survives_passthrough_append():
    artifacts = (_artifact("gal", e2e=config.E2eSpec(harness_name="electron")),)
    (job,) = e2e_mod.plan_e2e(artifacts, passthrough=("--grep", "smoke"))
    assert job.harness == ("npm", "exec", "--", "playwright", "test", "--grep", "smoke")
    assert job.env == e2e_mod.ELECTRON.env


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
    (job,) = e2e_mod.plan_e2e(artifacts, selector="a", passthrough=("--tap",))
    assert job.harness[-1] == "--tap"


def test_passthrough_over_a_repo_with_no_e2e_is_a_usage_error_not_a_no_op():
    artifacts = (_artifact("cli", build=(config.BuildTarget("rust"),)),)
    with pytest.raises(e2e_mod.E2ePlanError, match=r"exactly one.*declares no e2e"):
        e2e_mod.plan_e2e(artifacts, passthrough=("--tap",))


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


def test_rust_cross_target_reads_the_triple_release_dir():
    artifact = _artifact("app", build=(config.BuildTarget("rust", package="app-cli"),))
    loc = e2e_mod.binary_location(
        artifact, (_entry(".", "rust"),), target_triple="x86_64-unknown-linux-musl"
    )
    assert loc == e2e_mod.BinaryLocation(
        leg_path=".", relpath="target/x86_64-unknown-linux-musl/release/app-cli"
    )


def test_go_cross_target_is_ignored_native_path_stays():
    artifact = _artifact("dodot", build=(config.BuildTarget("go"),))
    loc = e2e_mod.binary_location(
        artifact, (_entry(".", "go"),), target_triple="x86_64-pc-windows-msvc"
    )
    assert loc.relpath == "dodot"


def test_go_binary_is_the_built_package_basename_in_the_leg_path():
    artifact = _artifact(
        "padz", build=(config.BuildTarget("go", package="./cmd/padz"),)
    )
    loc = e2e_mod.binary_location(artifact, (_entry(".", "go"),))
    assert loc == e2e_mod.BinaryLocation(leg_path=".", relpath="padz")


def test_go_binary_without_a_package_is_named_by_the_artifact():
    artifact = _artifact("dodot", build=(config.BuildTarget("go"),))
    assert e2e_mod.binary_location(artifact, (_entry(".", "go"),)).relpath == "dodot"


@pytest.mark.parametrize("package", [".", "./", "..", "../", "/"])
def test_ambiguous_go_package_is_refused_with_a_real_diagnosis(package):
    artifact = _artifact("padz", build=(config.BuildTarget("go", package=package),))
    with pytest.raises(config.ConfigError, match=r"has no binary name.*\./cmd/padz"):
        e2e_mod.binary_location(artifact, (_entry(".", "go"),))


@pytest.mark.parametrize("package", [".", "./", "..", "/"])
def test_ambiguous_rust_package_is_refused_before_a_nonsense_binary_path(package):
    artifact = _artifact("app", build=(config.BuildTarget("rust", package=package),))
    with pytest.raises(config.ConfigError, match=r"has no binary name.*crate name"):
        e2e_mod.binary_location(artifact, (_entry(".", "rust"),))


def test_the_first_binary_producing_target_wins_over_non_binary_ones():
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
    artifact = _artifact("cli", build=(config.BuildTarget("rust"),))
    with pytest.raises(config.ConfigError, match=r"\[toolchains\] rust leg"):
        e2e_mod.binary_location(artifact, ())
