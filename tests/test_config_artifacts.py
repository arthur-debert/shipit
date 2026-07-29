import tomllib

import pytest

from shipit import config


def _load(text: str) -> tuple[config.Artifact, ...]:
    return config.load_artifacts(tomllib.loads(text))


def test_absent_table_is_the_empty_tuple():
    assert config.load_artifacts({}) == ()


def test_full_artifact_parses_to_typed_frozen_values():
    (artifact,) = _load(
        "[artifacts.lex-cli]\n"
        'build = [{ toolchain = "rust", package = "lex-cli" }]\n'
        'platforms = ["darwin-arm64", "linux-x86_64"]\n'
        'bundle = { composition = "archive" }\n'
        'main-binary = "lex"\n'
        'product-name = "Lex"\n'
        'endpoints = ["gh-release", "crates"]\n'
        'e2e = { harness = ["bats", "tests/e2e.bats"] }\n'
        "sign = true\n"
    )
    assert artifact == config.Artifact(
        name="lex-cli",
        build=(config.BuildTarget(toolchain="rust", package="lex-cli"),),
        platforms=("darwin-arm64", "linux-x86_64"),
        bundle=config.BundleSpec(composition="archive"),
        main_binary="lex",
        product_name="Lex",
        endpoints=("gh-release", "crates"),
        e2e=config.E2eSpec(harness=("bats", "tests/e2e.bats")),
        sign=True,
    )


def test_bare_toolchain_string_is_a_whole_leg_target():
    (artifact,) = _load('[artifacts.dist]\nbuild = ["python"]\n')
    assert artifact.build == (config.BuildTarget(toolchain="python"),)


def test_optional_fields_default_to_absent_not_null():
    (artifact,) = _load('[artifacts.plugin]\nendpoints = ["gh-release"]\n')
    assert artifact.build == ()
    assert artifact.bundle is None
    assert artifact.e2e is None
    assert artifact.sign is False


@pytest.mark.parametrize(
    "package,expected",
    [
        (None, None),
        ("lex-cli", "lex-cli"),
        ("./cmd/padz", "padz"),
        (".", None),
        ("./", None),
        ("..", None),
        ("/", None),
    ],
)
def test_build_target_package_basename(package, expected):
    target = config.BuildTarget(toolchain="go", package=package)
    assert target.package_basename == expected


def test_go_target_carries_the_version_var():
    (artifact,) = _load(
        "[artifacts.mycli]\n"
        'build = [{ toolchain = "go", package = "./cmd/mycli",'
        ' version-var = "example.com/mycli/internal/version.Version" }]\n'
    )
    (target,) = artifact.build
    assert target.version_var == "example.com/mycli/internal/version.Version"


def test_empty_e2e_table_declares_the_default_harness():
    (artifact,) = _load("[artifacts.app]\ne2e = {}\n")
    assert artifact.e2e == config.E2eSpec(harness=None)


def test_several_artifacts_parse_in_declaration_order():
    artifacts = _load(
        '[artifacts.cli]\nbuild = [{ toolchain = "rust", package = "cli" }]\n'
        '[artifacts.lsp]\nbuild = [{ toolchain = "rust", package = "lsp" }]\n'
    )
    assert [a.name for a in artifacts] == ["cli", "lsp"]


def test_one_artifact_from_several_toolchains():
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = [{ toolchain = "rust" }, { toolchain = "npm", package = "web" }]\n'
    )
    assert [t.toolchain for t in artifact.build] == ["rust", "npm"]


def test_non_table_artifact_is_refused():
    with pytest.raises(config.ConfigError, match=r"\[artifacts\].x must be a table"):
        config.load_artifacts({"artifacts": {"x": "gh-release"}})


def test_unknown_artifact_key_names_itself_and_the_known_set():
    with pytest.raises(config.ConfigError, match="unknown key `endpoint`") as exc:
        _load('[artifacts.x]\nendpoint = ["gh-release"]\n')
    assert (
        "build, platforms, bundle, bundle-config, endpoints, downstreams, e2e, "
        "main-binary, product-name, sign" in str(exc.value)
    )


def test_unknown_endpoint_names_the_closed_registry():
    with pytest.raises(config.ConfigError, match="unknown endpoint `homebrew`") as exc:
        _load('[artifacts.x]\nendpoints = ["homebrew"]\n')
    assert "gh-release, crates, pypi, npm, vscode-marketplace, open-vsx, brew" in str(
        exc.value
    )


def test_downstreams_parse_with_the_notify_endpoint(tmp_path):
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
        'endpoints = ["gh-release", "notify-downstreams"]\n'
        'downstreams = ["lex-fmt/vscode", "lex-fmt/nvim", "lex-fmt/lexed"]\n'
    )
    assert artifact.downstreams == ("lex-fmt/vscode", "lex-fmt/nvim", "lex-fmt/lexed")
    assert "notify-downstreams" in artifact.endpoints


def test_notify_endpoint_without_downstreams_is_refused():
    with pytest.raises(config.ConfigError, match="needs a `downstreams` list"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
            'endpoints = ["gh-release", "notify-downstreams"]\n'
        )


def test_downstreams_without_the_notify_endpoint_is_refused():
    with pytest.raises(config.ConfigError, match="notify-downstreams.*is not"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
            'endpoints = ["gh-release"]\n'
            'downstreams = ["lex-fmt/vscode"]\n'
        )


def test_downstream_not_owner_name_slug_is_refused():
    with pytest.raises(config.ConfigError, match="is not an `owner/name` repo slug"):
        _load(
            "[artifacts.parser]\n"
            'endpoints = ["notify-downstreams"]\n'
            'downstreams = ["justname"]\n'
        )


def test_duplicate_downstream_is_refused():
    with pytest.raises(
        config.ConfigError, match="duplicate downstream `lex-fmt/vscode`"
    ):
        _load(
            "[artifacts.parser]\n"
            'endpoints = ["notify-downstreams"]\n'
            'downstreams = ["lex-fmt/vscode", "lex-fmt/vscode"]\n'
        )


def test_downstreams_normalized_to_canonical_lowercase_slug(tmp_path):
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'endpoints = ["notify-downstreams"]\n'
        'downstreams = ["Lex-Fmt/VSCode", "LEX-FMT/Nvim"]\n'
    )
    assert artifact.downstreams == ("lex-fmt/vscode", "lex-fmt/nvim")


def test_case_only_duplicate_downstream_is_refused():
    with pytest.raises(
        config.ConfigError, match="duplicate downstream `lex-fmt/vscode`"
    ):
        _load(
            "[artifacts.parser]\n"
            'endpoints = ["notify-downstreams"]\n'
            'downstreams = ["lex-fmt/vscode", "Lex-Fmt/VSCode"]\n'
        )


def test_tarball_with_multiple_platforms_is_refused():
    with pytest.raises(config.ConfigError, match="is platform-independent"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
            'platforms = ["linux-x86_64", "darwin-arm64"]\n'
        )


def test_tarball_with_a_single_platform_is_allowed():
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
        'platforms = ["linux-x86_64"]\n'
    )
    assert artifact.platforms == ("linux-x86_64",)


def test_tarball_with_no_platforms_is_allowed():
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
    )
    assert artifact.platforms == ()


def test_wasm_pack_with_multiple_platforms_is_refused():
    with pytest.raises(config.ConfigError, match="is platform-independent"):
        _load(
            "[artifacts.wasm]\n"
            'build = ["rust"]\n'
            'bundle = { composition = "wasm-pack" }\n'
            'platforms = ["linux-x86_64", "darwin-arm64"]\n'
        )


def test_wasm_pack_with_a_single_platform_is_allowed():
    (artifact,) = _load(
        "[artifacts.wasm]\n"
        'build = ["rust"]\n'
        'bundle = { composition = "wasm-pack" }\n'
        'platforms = ["linux-x86_64"]\n'
    )
    assert artifact.platforms == ("linux-x86_64",)


def test_wasm_pack_with_no_platforms_is_allowed():
    (artifact,) = _load(
        '[artifacts.wasm]\nbuild = ["rust"]\nbundle = { composition = "wasm-pack" }\n'
    )
    assert artifact.platforms == ()


def test_multi_platform_archive_is_still_allowed():
    (artifact,) = _load(
        "[artifacts.lex]\n"
        'build = ["rust"]\n'
        'bundle = { composition = "archive" }\n'
        'platforms = ["linux-x86_64", "darwin-arm64"]\n'
    )
    assert artifact.platforms == ("linux-x86_64", "darwin-arm64")


def test_unknown_platform_names_the_closed_registry():
    with pytest.raises(config.ConfigError, match="unknown platform `darwin`") as exc:
        _load('[artifacts.x]\nplatforms = ["darwin"]\n')
    assert ", ".join(config.PLATFORMS) in str(exc.value)


def test_duplicate_platform_is_refused():
    with pytest.raises(config.ConfigError, match="duplicate platform `linux-x86_64`"):
        _load('[artifacts.x]\nplatforms = ["linux-x86_64", "linux-x86_64"]\n')


def test_non_list_platforms_is_refused():
    with pytest.raises(
        config.ConfigError, match=r"platforms: must be a list of platform names"
    ):
        _load('[artifacts.x]\nplatforms = "linux-x86_64"\n')


def test_unknown_build_toolchain_names_the_registry():
    with pytest.raises(config.ConfigError, match="unknown toolchain `tauri`"):
        _load('[artifacts.x]\nbuild = [{ toolchain = "tauri" }]\n')


def test_build_must_be_a_list():
    with pytest.raises(config.ConfigError, match=r"\.build: must be a list"):
        _load('[artifacts.x]\nbuild = "rust"\n')


def test_build_target_table_must_name_its_toolchain():
    with pytest.raises(config.ConfigError, match=r"build\[0\] must name its toolchain"):
        _load('[artifacts.x]\nbuild = [{ package = "cli" }]\n')


def test_unknown_build_target_key_is_refused():
    with pytest.raises(config.ConfigError, match="unknown key `pacakge`"):
        _load('[artifacts.x]\nbuild = [{ toolchain = "rust", pacakge = "cli" }]\n')


def test_empty_shorthand_toolchain_string_names_the_offending_value():
    with pytest.raises(config.ConfigError, match="must be a non-empty toolchain name"):
        _load('[artifacts.x]\nbuild = [""]\n')


def test_whitespace_version_var_is_refused_at_parse():
    with pytest.raises(config.ConfigError, match="must not contain whitespace"):
        _load(
            "[artifacts.x]\n"
            'build = [{ toolchain = "go", version-var = "pkg.Version evil" }]\n'
        )


def test_version_var_is_go_only():
    with pytest.raises(config.ConfigError, match="version-var applies only to the go"):
        _load(
            "[artifacts.x]\n"
            'build = [{ toolchain = "rust", version-var = "pkg.Version" }]\n'
        )


def test_bundle_requires_its_composition():
    with pytest.raises(config.ConfigError, match="bundle must name its composition"):
        _load("[artifacts.x]\nbundle = {}\n")


def test_bundle_composition_names_the_closed_registry():
    with pytest.raises(config.ConfigError, match="unknown composition `rpm`") as exc:
        _load('[artifacts.x]\nbundle = { composition = "rpm" }\n')
    assert (
        "archive, deb, wheel, wasm-pack, vsix, mac-app, tauri, electron, tarball"
        in str(exc.value)
    )


def test_mac_app_requires_the_declared_bundler_command():
    with pytest.raises(config.ConfigError, match="declare its argv"):
        _load('[artifacts.x]\nbundle = { composition = "mac-app" }\n')


def test_mac_app_requires_the_source_dir():
    with pytest.raises(config.ConfigError, match="needs `source`"):
        _load(
            "[artifacts.x]\n"
            'bundle = { composition = "mac-app", command = ["tauri", "build"] }\n'
        )


def test_mac_app_parses_command_and_source():
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = ["rust"]\n'
        'bundle = { composition = "mac-app", command = ["npm", "run", "bundle"],'
        ' source = "./src-tauri/target/release/bundle" }\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="mac-app",
        command=("npm", "run", "bundle"),
        source="src-tauri/target/release/bundle",
    )


def test_tauri_parses_command_and_source_like_a_declared_bundler():
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = ["rust", "npm"]\n'
        'bundle = { composition = "tauri", command = ["npm", "run", "tauri", '
        '"build"], source = "src-tauri/target/release/bundle" }\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="tauri",
        command=("npm", "run", "tauri", "build"),
        source="src-tauri/target/release/bundle",
    )


def test_tauri_needs_command_and_source():
    with pytest.raises(
        config.ConfigError, match=r"composition `tauri` runs the artifact's own"
    ):
        _load('[artifacts.x]\nbundle = { composition = "tauri" }\n')


@pytest.mark.parametrize("bad", ['"."', '"./"'])
def test_declared_command_source_refuses_the_repo_root(bad):
    with pytest.raises(
        config.ConfigError, match=r"dedicated bundle output subdirectory"
    ):
        _load(
            "[artifacts.x]\n"
            'bundle = { composition = "tauri", command = ["npm", "run", "tauri", '
            f'"build"], source = {bad} }}\n'
        )


def test_sign_with_a_tauri_bundle_parses():
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = ["rust", "npm"]\n'
        'platforms = ["darwin-arm64", "linux-x86_64"]\n'
        'bundle = { composition = "tauri", command = ["npm", "run", "tauri", '
        '"build"], source = "src-tauri/target/release/bundle" }\n'
        "sign = true\n"
    )
    assert artifact.sign is True
    assert artifact.bundle.composition == "tauri"


def test_electron_parses_command_and_source_like_a_declared_bundler():
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = ["npm"]\n'
        'bundle = { composition = "electron", command = ["npm", "run", "dist"],'
        ' source = "./release" }\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="electron",
        command=("npm", "run", "dist"),
        source="release",
    )


def test_electron_requires_the_declared_bundler_command():
    with pytest.raises(config.ConfigError, match="declare its argv"):
        _load('[artifacts.x]\nbuild = ["npm"]\nbundle = { composition = "electron" }\n')


def test_sign_with_electron_is_accepted_it_routes_through_the_sign_stage():
    (artifact,) = _load(
        "[artifacts.app]\n"
        'build = ["npm"]\n'
        'platforms = ["darwin-arm64"]\n'
        'bundle = { composition = "electron", command = ["npm", "run", "dist"],'
        ' source = "release" }\n'
        "sign = true\n"
    )
    assert artifact.sign is True
    assert artifact.bundle.composition == "electron"


def test_bundle_command_must_be_an_argv_list():
    with pytest.raises(
        config.ConfigError, match=r"bundle.command must be a non-empty argv"
    ):
        _load(
            "[artifacts.x]\n"
            'bundle = { composition = "mac-app", command = "tauri bundle",'
            ' source = "out" }\n'
        )


@pytest.mark.parametrize("key,value", [("command", '["tar"]'), ("source", '"out"')])
def test_registry_assembled_compositions_reject_declared_command(key, value):
    with pytest.raises(config.ConfigError, match=f"`{key}` applies only to"):
        _load(
            f'[artifacts.x]\nbundle = {{ composition = "archive", {key} = {value} }}\n'
        )


def test_wasm_pack_parses_scope_and_wasm_target(tmp_path):
    (artifact,) = _load(
        "[artifacts.wasm]\n"
        'build = ["rust"]\n'
        'bundle = { composition = "wasm-pack", scope = "lex-fmt", '
        'wasm-target = "web" }\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="wasm-pack", scope="lex-fmt", wasm_target="web"
    )


def test_wasm_pack_scope_and_target_default_to_absent():
    (artifact,) = _load(
        '[artifacts.wasm]\nbuild = ["rust"]\nbundle = { composition = "wasm-pack" }\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="wasm-pack", scope=None, wasm_target=None
    )


@pytest.mark.parametrize("key", ["scope", "wasm-target"])
def test_wasm_pack_options_must_be_non_empty_strings(key):
    with pytest.raises(config.ConfigError, match=f"{key} must be a non-empty string"):
        _load(
            "[artifacts.wasm]\n"
            'build = ["rust"]\n'
            f'bundle = {{ composition = "wasm-pack", {key} = "" }}\n'
        )


@pytest.mark.parametrize("key", ["scope", "wasm-target"])
def test_wasm_pack_options_are_rejected_on_other_compositions(key):
    with pytest.raises(config.ConfigError, match=f"unknown key `{key}`"):
        _load(
            "[artifacts.x]\n"
            'build = ["rust"]\n'
            f'bundle = {{ composition = "archive", {key} = "web" }}\n'
        )


def _payload_toml(payload: str, composition: str = "tarball") -> str:
    return (
        "[artifacts.parser]\n"
        'build = ["tree-sitter"]\n'
        "[artifacts.parser.bundle]\n"
        f'composition = "{composition}"\n'
        'leg = "tree-sitter"\n'
        f"payload = {payload}\n"
    )


def test_payload_parses_to_ordered_typed_entries():
    (artifact,) = _load(
        _payload_toml(
            "[\n"
            '  { path = "src", required = true },\n'
            '  { path = "tree-sitter-lex.wasm", required = true },\n'
            '  { path = "queries" },\n'
            '  { path = "shared/embedded-grammars.json" },\n'
            "]"
        )
    )
    assert artifact.bundle == config.BundleSpec(
        composition="tarball",
        leg="tree-sitter",
        payload=(
            config.PayloadEntry(path="src", required=True),
            config.PayloadEntry(path="tree-sitter-lex.wasm", required=True),
            config.PayloadEntry(path="queries", required=False),
            config.PayloadEntry(path="shared/embedded-grammars.json", required=False),
        ),
    )


def test_zed_takes_the_same_declaration():
    (artifact,) = _load(
        "[artifacts.zed-lex]\n"
        'build = ["rust"]\n'
        "[artifacts.zed-lex.bundle]\n"
        'composition = "zed"\n'
        'leg = "rust"\n'
        'payload = [{ path = "extension.toml", required = true }, { path = "shared" }]\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="zed",
        leg="rust",
        payload=(
            config.PayloadEntry(path="extension.toml", required=True),
            config.PayloadEntry(path="shared", required=False),
        ),
    )


@pytest.mark.parametrize(
    ("declared", "named"),
    [
        ("", "`leg` and `payload`"),
        ('leg = "tree-sitter"', "`payload`"),
        ('payload = [{ path = "src", required = true }]', "`leg`"),
    ],
)
def test_declared_payload_keys_are_required_with_a_migration_pointer(declared, named):
    with pytest.raises(config.ConfigError) as exc:
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            "[artifacts.parser.bundle]\n"
            'composition = "tarball"\n' + (f"{declared}\n" if declared else "")
        )
    message = str(exc.value)
    assert f"is missing {named}" in message
    assert "no longer carries a built-in file list" in message
    assert 'payload = [{ path = "src", required = true }' in message


def test_payload_leg_must_be_a_non_empty_string():
    with pytest.raises(config.ConfigError, match=r"bundle\.leg must be a non-empty"):
        _load(
            _payload_toml('[{ path = "src", required = true }]').replace(
                'leg = "tree-sitter"', 'leg = ""'
            )
        )


def test_payload_leg_names_a_known_toolchain():
    with pytest.raises(config.ConfigError, match="unknown toolchain `treesitter`"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball", leg = "treesitter", payload = '
            '[{ path = "src", required = true }] }\n'
        )


def test_payload_with_no_required_entry_is_refused():
    with pytest.raises(config.ConfigError, match="no entry declares `required"):
        _load(_payload_toml('[{ path = "queries" }, { path = "grammar.js" }]'))


def test_payload_must_be_a_non_empty_list():
    with pytest.raises(config.ConfigError, match="non-empty list of entry tables"):
        _load(_payload_toml("[]"))


def test_payload_entry_must_be_a_table_not_a_bare_string():
    with pytest.raises(config.ConfigError, match=r"payload\[1\]: must be a table"):
        _load(_payload_toml('[{ path = "src", required = true }, "queries"]'))


def test_payload_entry_unknown_key_names_itself_and_the_known_set():
    with pytest.raises(config.ConfigError, match="unknown key `optional`") as exc:
        _load(_payload_toml('[{ path = "src", optional = true }]'))
    assert "payload[0]: unknown key `optional`; known keys: path, required" in str(
        exc.value
    )


def test_payload_entry_path_must_be_a_non_empty_string():
    with pytest.raises(config.ConfigError, match=r"payload\[0\]\.path must be"):
        _load(_payload_toml('[{ path = "", required = true }]'))


def test_payload_entry_required_must_be_a_boolean():
    with pytest.raises(config.ConfigError, match=r"payload\[0\]\.required must be"):
        _load(_payload_toml('[{ path = "src", required = "yes" }]'))


@pytest.mark.parametrize("path", [".", "./", "./."])
def test_payload_entry_path_may_not_name_the_leg_dir_itself(path):
    with pytest.raises(config.ConfigError, match="names the leg directory itself"):
        _load(_payload_toml(f"[{{ path = '{path}', required = true }}]"))


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "..", "C:/x", "a\\b"])
def test_payload_entry_path_may_not_escape_the_checkout(path):
    with pytest.raises(config.ConfigError, match="repo-relative POSIX path"):
        _load(_payload_toml(f"[{{ path = '{path}', required = true }}]"))


def test_duplicate_payload_path_is_refused():
    with pytest.raises(config.ConfigError, match="already declared earlier"):
        _load(
            _payload_toml(
                '[{ path = "src", required = true }, { path = "./src" }]',
            )
        )


@pytest.mark.parametrize(
    "second", ["src/parser.c", "src/tree_sitter/parser.h"], ids=["child", "descendant"]
)
def test_payload_paths_that_overlap_an_earlier_entry_are_refused(second):
    with pytest.raises(config.ConfigError) as excinfo:
        _load(
            _payload_toml(
                f'[{{ path = "src", required = true }}, {{ path = "{second}" }}]',
            )
        )
    message = str(excinfo.value)
    assert "overlaps `src`" in message


def test_payload_overlap_is_compared_by_component_not_string_prefix():
    artifacts = _load(
        _payload_toml(
            '[{ path = "src", required = true }, { path = "srcfoo" }]',
        )
    )
    assert [entry.path for entry in artifacts[0].bundle.payload] == ["src", "srcfoo"]


def test_payload_overlap_is_refused_in_either_declaration_order():
    with pytest.raises(config.ConfigError, match="overlaps `src/parser.c`"):
        _load(
            _payload_toml(
                '[{ path = "src/parser.c", required = true }, { path = "src" }]',
            )
        )


@pytest.mark.parametrize("key", ["leg", "payload"])
def test_leg_and_payload_are_rejected_on_other_compositions(key):
    value = '"rust"' if key == "leg" else '[{ path = "src", required = true }]'
    with pytest.raises(config.ConfigError, match=f"`{key}` applies only to") as exc:
        _load(
            "[artifacts.x]\n"
            'build = ["rust"]\n'
            f'bundle = {{ composition = "archive", {key} = {value} }}\n'
        )
    assert "producer-declared (tarball, zed)" in str(exc.value)


def test_vsix_parses_the_stage_map():
    (artifact,) = _load(
        "[artifacts.ext]\n"
        'build = ["npm"]\n'
        "[artifacts.ext.bundle]\n"
        'composition = "vsix"\n'
        "[artifacts.ext.bundle.stage]\n"
        '"lexd-lsp" = "resources/lexd-lsp"\n'
        '"tree-sitter-lex" = "resources/tree-sitter-lex.wasm"\n'
    )
    assert artifact.bundle == config.BundleSpec(
        composition="vsix",
        stage=(
            ("lexd-lsp", "resources/lexd-lsp"),
            ("tree-sitter-lex", "resources/tree-sitter-lex.wasm"),
        ),
    )


def test_vsix_stage_defaults_to_empty():
    (artifact,) = _load(
        '[artifacts.ext]\nbuild = ["npm"]\nbundle = { composition = "vsix" }\n'
    )
    assert artifact.bundle == config.BundleSpec(composition="vsix", stage=())


def test_vsix_stage_is_rejected_on_other_compositions():
    with pytest.raises(config.ConfigError, match="unknown key `stage`"):
        _load(
            "[artifacts.x]\n"
            'build = ["rust"]\n'
            'bundle = { composition = "archive", stage = { "lexd-lsp" = "r" } }\n'
        )


def test_vsix_stage_must_be_a_non_empty_table():
    with pytest.raises(config.ConfigError, match="stage: must be a non-empty table"):
        _load(
            "[artifacts.ext]\n"
            'build = ["npm"]\n'
            'bundle = { composition = "vsix", stage = {} }\n'
        )


def test_vsix_stage_key_must_be_a_conda_package_name():
    with pytest.raises(
        config.ConfigError, match="not a valid \\[artifact-deps\\] package name"
    ):
        _load(
            "[artifacts.ext]\n"
            'build = ["npm"]\n'
            '[artifacts.ext.bundle]\ncomposition = "vsix"\n'
            '[artifacts.ext.bundle.stage]\n"Lexd-LSP" = "resources/lexd-lsp"\n'
        )


def test_vsix_stage_destination_must_be_a_non_empty_string():
    with pytest.raises(config.ConfigError, match="destination must be a non-empty"):
        _load(
            "[artifacts.ext]\n"
            'build = ["npm"]\n'
            '[artifacts.ext.bundle]\ncomposition = "vsix"\n'
            '[artifacts.ext.bundle.stage]\n"lexd-lsp" = ""\n'
        )


@pytest.mark.parametrize(
    "dest",
    [
        "/abs/lexd",
        "../escape/lexd",
        "C:\\\\temp\\\\lexd-lsp",
        "\\\\temp\\\\lexd-lsp",
        "..\\\\escape\\\\x",
        "C:lexd-lsp",
    ],
)
def test_vsix_stage_destination_may_not_escape_the_checkout(dest):
    with pytest.raises(config.ConfigError, match="must be a repo-relative"):
        _load(
            "[artifacts.ext]\n"
            'build = ["npm"]\n'
            '[artifacts.ext.bundle]\ncomposition = "vsix"\n'
            f'[artifacts.ext.bundle.stage]\n"lexd-lsp" = "{dest}"\n'
        )


@pytest.mark.parametrize("key", ["main-binary", "product-name"])
@pytest.mark.parametrize("value", ['""', "true", "[1]"])
def test_main_binary_names_must_be_non_empty_strings(key, value):
    with pytest.raises(config.ConfigError, match=f"{key}: must be a non-empty name"):
        _load(f"[artifacts.x]\n{key} = {value}\n")


def test_e2e_harness_string_names_a_registered_harness():
    (artifact,) = _load('[artifacts.x]\ne2e = { harness = "electron" }\n')
    assert artifact.e2e == config.E2eSpec(harness_name="electron")


def test_e2e_empty_harness_string_is_refused():
    with pytest.raises(config.ConfigError, match=r"named harness must be a non-empty"):
        _load('[artifacts.x]\ne2e = { harness = "" }\n')


def test_e2e_harness_that_is_neither_string_nor_list_must_be_an_argv():
    with pytest.raises(
        config.ConfigError, match=r"e2e.harness must be a non-empty argv"
    ):
        _load("[artifacts.x]\ne2e = { harness = 3 }\n")


def test_sign_must_be_a_boolean():
    with pytest.raises(config.ConfigError, match=r"\.sign: must be a boolean"):
        _load('[artifacts.x]\nsign = "yes"\n')


def test_sign_with_a_build_darwin_platform_and_signable_bundle_parses():
    (artifact,) = _load(
        "[artifacts.x]\n"
        'build = ["rust"]\n'
        'platforms = ["darwin-arm64", "linux-x86_64"]\n'
        'bundle = { composition = "archive" }\n'
        "sign = true\n"
    )
    assert artifact.sign is True


def test_sign_without_a_bundle_is_refused():
    with pytest.raises(
        config.ConfigError,
        match=r"sign = true requires a bundle composition the signer can "
        r"reopen \(archive, mac-app, tauri, electron\); got no bundle",
    ):
        _load(
            "[artifacts.x]\n"
            'build = ["rust"]\n'
            'platforms = ["darwin-arm64"]\n'
            "sign = true\n"
        )


def test_sign_with_an_unsignable_composition_is_refused():
    with pytest.raises(
        config.ConfigError,
        match=r"sign = true requires a bundle composition the signer can "
        r"reopen \(archive, mac-app, tauri, electron\); got composition `wheel`",
    ):
        _load(
            "[artifacts.x]\n"
            'build = ["python"]\n'
            'platforms = ["darwin-arm64"]\n'
            'bundle = { composition = "wheel" }\n'
            "sign = true\n"
        )


def test_bundle_without_a_build_target_is_refused():
    with pytest.raises(
        config.ConfigError, match=r"bundle requires at least one build target"
    ):
        _load('[artifacts.x]\nbundle = { composition = "archive" }\n')


def test_bundle_shape_error_precedes_the_build_requirement():
    with pytest.raises(config.ConfigError, match="bundle must name its composition"):
        _load("[artifacts.x]\nbundle = {}\n")


def test_sign_without_a_build_target_is_refused():
    with pytest.raises(
        config.ConfigError, match=r"sign = true requires at least one build target"
    ):
        _load('[artifacts.x]\nplatforms = ["darwin-arm64"]\nsign = true\n')


def test_sign_without_a_darwin_platform_is_refused():
    with pytest.raises(
        config.ConfigError, match=r"sign = true requires at least one darwin platform"
    ):
        _load(
            '[artifacts.x]\nbuild = ["rust"]\nplatforms = ["linux-x86_64"]\nsign = true\n'
        )


def test_sign_with_default_platforms_is_refused():
    with pytest.raises(
        config.ConfigError, match=r"sign = true requires at least one darwin platform"
    ):
        _load('[artifacts.x]\nbuild = ["rust"]\nsign = true\n')


def test_bundle_config_parses_to_a_path():
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
    (artifact,) = _load(
        '[artifacts.app]\nbundle-config = "./src-tauri/tauri.conf.json"\n'
    )
    assert artifact.bundle_config == "src-tauri/tauri.conf.json"


@pytest.mark.parametrize(
    "value",
    ['"/etc/passwd"', '"../outside/tauri.conf.json"', '"a/../../b.json"'],
)
def test_bundle_config_rejects_paths_escaping_the_checkout(value):
    with pytest.raises(config.ConfigError, match=r"inside the checkout"):
        _load(f"[artifacts.app]\nbundle-config = {value}\n")


def test_artifacts_is_a_known_top_level_table(tmp_path):
    p = tmp_path / config.CONFIG_NAME
    p.write_text('[artifacts.x]\nendpoints = ["npm"]\n', encoding="utf-8")
    (artifact,) = config.load_artifacts(config.load(p))
    assert artifact.endpoints == ("npm",)
