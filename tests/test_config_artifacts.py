"""The ``[artifacts]`` map loader (TOL01-WS02) — typed frozen values at the
config boundary (ADR-0030: construction is validation).

Fixture-driven, prior art the ``[toolchains]`` loader tests: happy shapes in
(TOML text → typed values out), loud malformed-config errors naming the
offending key. Endpoints / bundle / sign / e2e are parsed HERE and consumed
later (release stages, WS03) — these tests pin the parse contract those
consumers inherit.
"""

import tomllib

import pytest

from shipit import config


def _load(text: str) -> tuple[config.Artifact, ...]:
    return config.load_artifacts(tomllib.loads(text))


# --------------------------------------------------------------------------
# Happy shapes
# --------------------------------------------------------------------------


def test_absent_table_is_the_empty_tuple():
    # No artifact map is a legal repo: `shipit build` runs bare legs.
    assert config.load_artifacts({}) == ()


def test_full_artifact_parses_to_typed_frozen_values():
    (artifact,) = _load(
        "[artifacts.lex-cli]\n"
        'build = [{ toolchain = "rust", package = "lex-cli" }]\n'
        'bundle = { command = ["tauri", "bundle"] }\n'
        'endpoints = ["gh-release", "crates"]\n'
        'e2e = { harness = ["bats", "tests/e2e.bats"] }\n'
        "sign = true\n"
    )
    assert artifact == config.Artifact(
        name="lex-cli",
        build=(config.BuildTarget(toolchain="rust", package="lex-cli"),),
        bundle=config.BundleSpec(command=("tauri", "bundle")),
        endpoints=("gh-release", "crates"),
        e2e=config.E2eSpec(harness=("bats", "tests/e2e.bats")),
        sign=True,
    )


def test_bare_toolchain_string_is_a_whole_leg_target():
    # The shorthand: the leg's default build produces the artifact whole.
    (artifact,) = _load('[artifacts.dist]\nbuild = ["python"]\n')
    assert artifact.build == (config.BuildTarget(toolchain="python"),)


def test_optional_fields_default_to_absent_not_null():
    # nvim's "the tag is the release" shape (PRD further notes): zero build,
    # zero bundle, one endpoint — every optional field at its quiet default.
    (artifact,) = _load('[artifacts.plugin]\nendpoints = ["gh-release"]\n')
    assert artifact.build == ()
    assert artifact.bundle is None
    assert artifact.e2e is None
    assert artifact.sign is False


def test_go_target_carries_the_version_var():
    (artifact,) = _load(
        "[artifacts.mycli]\n"
        'build = [{ toolchain = "go", package = "./cmd/mycli",'
        ' version-var = "example.com/mycli/internal/version.Version" }]\n'
    )
    (target,) = artifact.build
    assert target.version_var == "example.com/mycli/internal/version.Version"


def test_empty_e2e_table_declares_the_default_harness():
    # Declaring `e2e` AT ALL is the opt-in (PRD story 11); an empty table
    # means the registry-default harness (WS03 resolves it).
    (artifact,) = _load("[artifacts.app]\ne2e = {}\n")
    assert artifact.e2e == config.E2eSpec(harness=None)


def test_several_artifacts_parse_in_declaration_order():
    artifacts = _load(
        '[artifacts.cli]\nbuild = [{ toolchain = "rust", package = "cli" }]\n'
        '[artifacts.lsp]\nbuild = [{ toolchain = "rust", package = "lsp" }]\n'
    )
    assert [a.name for a in artifacts] == ["cli", "lsp"]


def test_one_artifact_from_several_toolchains():
    # ADR-0007's many-to-many: rust binary + npm frontend -> one Tauri app.
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = [{ toolchain = "rust" }, { toolchain = "npm", package = "web" }]\n'
    )
    assert [t.toolchain for t in artifact.build] == ["rust", "npm"]


# --------------------------------------------------------------------------
# Loud malformed-config errors, naming the offending key (ADR-0030)
# --------------------------------------------------------------------------


def test_non_table_artifact_is_refused():
    with pytest.raises(config.ConfigError, match=r"\[artifacts\].x must be a table"):
        config.load_artifacts({"artifacts": {"x": "gh-release"}})


def test_unknown_artifact_key_names_itself_and_the_known_set():
    with pytest.raises(config.ConfigError, match="unknown key `endpoint`") as exc:
        _load('[artifacts.x]\nendpoint = ["gh-release"]\n')
    assert "build, bundle, bundle-config, endpoints, e2e, sign" in str(exc.value)


def test_unknown_endpoint_names_the_closed_registry():
    with pytest.raises(config.ConfigError, match="unknown endpoint `homebrew`") as exc:
        _load('[artifacts.x]\nendpoints = ["homebrew"]\n')
    assert "gh-release, crates, pypi, npm, brew" in str(exc.value)


def test_unknown_build_toolchain_names_the_registry():
    # Same closed set as [toolchains]: "tauri" is never a dispatch label.
    with pytest.raises(config.ConfigError, match="unknown toolchain `tauri`"):
        _load('[artifacts.x]\nbuild = [{ toolchain = "tauri" }]\n')


def test_build_must_be_a_list():
    # The offending key rides the `where` prefix (ADR-0030), like the nested
    # `.build[i]` / `.bundle.command` messages do.
    with pytest.raises(config.ConfigError, match=r"\.build: must be a list"):
        _load('[artifacts.x]\nbuild = "rust"\n')


def test_build_target_table_must_name_its_toolchain():
    with pytest.raises(config.ConfigError, match=r"build\[0\] must name its toolchain"):
        _load('[artifacts.x]\nbuild = [{ package = "cli" }]\n')


def test_unknown_build_target_key_is_refused():
    with pytest.raises(config.ConfigError, match="unknown key `pacakge`"):
        _load('[artifacts.x]\nbuild = [{ toolchain = "rust", pacakge = "cli" }]\n')


def test_empty_shorthand_toolchain_string_names_the_offending_value():
    # `build = [""]` is caught as an empty target, not misreported as an
    # "unknown toolchain ``" — the shorthand form validates non-empty like the
    # table form does.
    with pytest.raises(config.ConfigError, match="must be a non-empty toolchain name"):
        _load('[artifacts.x]\nbuild = [""]\n')


def test_whitespace_version_var_is_refused_at_parse():
    # version-var rides go's -ldflags -X value, which the go tool re-splits on
    # whitespace — so whitespace in it is refused at parse (ADR-0041), the same
    # class as a whitespace `--version`.
    with pytest.raises(config.ConfigError, match="must not contain whitespace"):
        _load(
            "[artifacts.x]\n"
            'build = [{ toolchain = "go", version-var = "pkg.Version evil" }]\n'
        )


def test_version_var_is_go_only():
    # ADR-0041: only go injects the version at build; everyone else's version
    # is a manifest projection bumped at prepare.
    with pytest.raises(config.ConfigError, match="version-var applies only to the go"):
        _load(
            "[artifacts.x]\n"
            'build = [{ toolchain = "rust", version-var = "pkg.Version" }]\n'
        )


def test_bundle_requires_its_command_argv():
    with pytest.raises(config.ConfigError, match="bundle must declare its command"):
        _load("[artifacts.x]\nbundle = {}\n")


def test_bundle_command_must_be_an_argv_list():
    # An argv, never a shell string (ADR-0028: no shell=True anywhere).
    with pytest.raises(
        config.ConfigError, match=r"bundle.command must be a non-empty argv"
    ):
        _load('[artifacts.x]\nbundle = { command = "tauri bundle" }\n')


def test_e2e_harness_must_be_an_argv_list():
    with pytest.raises(
        config.ConfigError, match=r"e2e.harness must be a non-empty argv"
    ):
        _load('[artifacts.x]\ne2e = { harness = "bats tests" }\n')


def test_sign_must_be_a_boolean():
    with pytest.raises(config.ConfigError, match=r"\.sign: must be a boolean"):
        _load('[artifacts.x]\nsign = "yes"\n')


def test_bundle_config_parses_to_a_path():
    # The artifact-declared bundle-config hook (TOL02-WS01, PRD story 25):
    # the repo-relative version file release prepare bumps in lockstep.
    (artifact,) = _load(
        '[artifacts.app]\nbundle-config = "src-tauri/tauri.conf.json"\n'
    )
    assert artifact.bundle_config == "src-tauri/tauri.conf.json"


def test_bundle_config_defaults_to_absent():
    (artifact,) = _load('[artifacts.app]\nendpoints = ["gh-release"]\n')
    assert artifact.bundle_config is None


@pytest.mark.parametrize("value", ['""', "true", "[1]"])
def test_bundle_config_must_be_a_non_empty_path(value):
    with pytest.raises(config.ConfigError, match=r"bundle-config: must be a non-empty"):
        _load(f"[artifacts.app]\nbundle-config = {value}\n")


def test_bundle_config_is_normalized_to_canonical_form():
    # `./src-tauri/...` must be stored as `src-tauri/...` so the release stage
    # stages and matches the same path `git status` reports (no false no-op).
    (artifact,) = _load(
        '[artifacts.app]\nbundle-config = "./src-tauri/tauri.conf.json"\n'
    )
    assert artifact.bundle_config == "src-tauri/tauri.conf.json"


@pytest.mark.parametrize(
    "value",
    ['"/etc/passwd"', '"../outside/tauri.conf.json"', '"a/../../b.json"'],
)
def test_bundle_config_rejects_paths_escaping_the_checkout(value):
    # A repo config is joined to the checkout root and REWRITTEN by release
    # prepare; an absolute or `..` path would steer that write outside the tree,
    # so it is refused at the parse boundary (the one place values flow through).
    with pytest.raises(config.ConfigError, match=r"inside the checkout"):
        _load(f"[artifacts.app]\nbundle-config = {value}\n")


def test_artifacts_is_a_known_top_level_table(tmp_path):
    # The closed known-tables registry accepts [artifacts]; the boundary load
    # would otherwise reject the whole file before the loader ever ran.
    p = tmp_path / config.CONFIG_NAME
    p.write_text('[artifacts.x]\nendpoints = ["npm"]\n', encoding="utf-8")
    (artifact,) = config.load_artifacts(config.load(p))
    assert artifact.endpoints == ("npm",)
