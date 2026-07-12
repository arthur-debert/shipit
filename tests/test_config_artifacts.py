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
    # The single source of truth for "does this package name a binary?" — the
    # basename, or None for no package / a bare path-navigation token. Shared by
    # binary_location and the assert-bundle expected-name chain.
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
        'bundle = { composition = "tarball" }\n'
        'endpoints = ["gh-release", "notify-downstreams"]\n'
        'downstreams = ["lex-fmt/vscode", "lex-fmt/nvim", "lex-fmt/lexed"]\n'
    )
    assert artifact.downstreams == ("lex-fmt/vscode", "lex-fmt/nvim", "lex-fmt/lexed")
    assert "notify-downstreams" in artifact.endpoints


def test_notify_endpoint_without_downstreams_is_refused():
    # The endpoint fires repository_dispatch AT the list — an endpoint with no
    # list is a no-op declaration, refused at parse (#792).
    with pytest.raises(config.ConfigError, match="needs a `downstreams` list"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball" }\n'
            'endpoints = ["gh-release", "notify-downstreams"]\n'
        )


def test_downstreams_without_the_notify_endpoint_is_refused():
    # A downstreams list nothing fires is dead config — refused (#792).
    with pytest.raises(config.ConfigError, match="notify-downstreams.*is not"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball" }\n'
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
    # GitHub owner/name are case-insensitive; downstreams go through the
    # canonical slug parser so every dispatch targets one normalized form (#792).
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'endpoints = ["notify-downstreams"]\n'
        'downstreams = ["Lex-Fmt/VSCode", "LEX-FMT/Nvim"]\n'
    )
    assert artifact.downstreams == ("lex-fmt/vscode", "lex-fmt/nvim")


def test_case_only_duplicate_downstream_is_refused():
    # Case-only repeats collapse to one canonical slug — a repeated dispatch is
    # never an intent, so the collision is refused rather than dispatched twice.
    with pytest.raises(
        config.ConfigError, match="duplicate downstream `lex-fmt/vscode`"
    ):
        _load(
            "[artifacts.parser]\n"
            'endpoints = ["notify-downstreams"]\n'
            'downstreams = ["lex-fmt/vscode", "Lex-Fmt/VSCode"]\n'
        )


def test_tarball_with_multiple_platforms_is_refused():
    # A platform-independent composition emits one unqualified archive; >1
    # platform would build colliding assets in the merged dist/ — refused (#792).
    with pytest.raises(config.ConfigError, match="is platform-independent"):
        _load(
            "[artifacts.parser]\n"
            'build = ["tree-sitter"]\n'
            'bundle = { composition = "tarball" }\n'
            'platforms = ["linux-x86_64", "darwin-arm64"]\n'
        )


def test_tarball_with_a_single_platform_is_allowed():
    # Exactly one lane is fine — one leg, one unqualified archive, no collision.
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball" }\n'
        'platforms = ["linux-x86_64"]\n'
    )
    assert artifact.platforms == ("linux-x86_64",)


def test_tarball_with_no_platforms_is_allowed():
    # No declaration defaults to a single lane, so the unqualified archive still
    # builds on exactly one leg.
    (artifact,) = _load(
        "[artifacts.parser]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball" }\n'
    )
    assert artifact.platforms == ()


def test_multi_platform_archive_is_still_allowed():
    # The guard is scoped to platform-independent compositions: archive emits
    # target-qualified names, so multiple platforms never collide.
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
    # A repeated platform would mean a repeated matrix entry, never an intent.
    with pytest.raises(config.ConfigError, match="duplicate platform `linux-x86_64`"):
        _load('[artifacts.x]\nplatforms = ["linux-x86_64", "linux-x86_64"]\n')


def test_non_list_platforms_is_refused():
    with pytest.raises(
        config.ConfigError, match=r"platforms: must be a list of platform names"
    ):
        _load('[artifacts.x]\nplatforms = "linux-x86_64"\n')


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


def test_bundle_requires_its_composition():
    with pytest.raises(config.ConfigError, match="bundle must name its composition"):
        _load("[artifacts.x]\nbundle = {}\n")


def test_bundle_composition_names_the_closed_registry():
    # The composition registry is closed (ADR-0007's shape): the message
    # names the known set, mirroring endpoints/toolchains.
    with pytest.raises(config.ConfigError, match="unknown composition `rpm`") as exc:
        _load('[artifacts.x]\nbundle = { composition = "rpm" }\n')
    assert "archive, deb, wheel, wasm-pack, vsix, mac-app" in str(exc.value)


def test_mac_app_requires_the_declared_bundler_command():
    # mac-app runs the artifact's OWN bundler (the one consumer-specific part
    # of the mac path, workflows.lex §3.1) — the declaration must carry it.
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
        # Normalized to canonical form, like bundle-config.
        source="src-tauri/target/release/bundle",
    )


def test_bundle_command_must_be_an_argv_list():
    # An argv, never a shell string (ADR-0028: no shell=True anywhere).
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
    # archive/deb/wheel assemble their own commands (ADR-0028's one assembly
    # point) — a declared argv or source dir would be a second one.
    with pytest.raises(config.ConfigError, match=f"`{key}` applies only to"):
        _load(
            f'[artifacts.x]\nbundle = {{ composition = "archive", {key} = {value} }}\n'
        )


def test_wasm_pack_parses_scope_and_wasm_target(tmp_path):
    # wasm-pack's optional consumer-specific parts (TOL02-WS12 #788): the npm
    # @scope and wasm-pack's --target, declared on the bundle table.
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
    # Both are optional — an undeclared scope/target is None (the composition
    # applies wasm-pack's own default target, `bundler`).
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
    # scope/wasm-target are wasm-pack's ONLY (option_keys) — an unknown key on
    # a composition that does not name them, so a typo dies at parse.
    with pytest.raises(config.ConfigError, match=f"unknown key `{key}`"):
        _load(
            "[artifacts.x]\n"
            'build = ["rust"]\n'
            f'bundle = {{ composition = "archive", {key} = "web" }}\n'
        )


@pytest.mark.parametrize("key", ["main-binary", "product-name"])
@pytest.mark.parametrize("value", ['""', "true", "[1]"])
def test_main_binary_names_must_be_non_empty_strings(key, value):
    with pytest.raises(config.ConfigError, match=f"{key}: must be a non-empty name"):
        _load(f"[artifacts.x]\n{key} = {value}\n")


def test_e2e_harness_must_be_an_argv_list():
    with pytest.raises(
        config.ConfigError, match=r"e2e.harness must be a non-empty argv"
    ):
        _load('[artifacts.x]\ne2e = { harness = "bats tests" }\n')


def test_sign_must_be_a_boolean():
    with pytest.raises(config.ConfigError, match=r"\.sign: must be a boolean"):
        _load('[artifacts.x]\nsign = "yes"\n')


def test_sign_with_a_build_darwin_platform_and_signable_bundle_parses():
    # `sign = true` is coherent once a build-bearing artifact has a darwin lane
    # (signing signs a build output, on macOS) AND a bundle the signer can
    # reopen (TOL02-WS08 #779): the linux entry rides along un-signed, the
    # darwin one signs.
    (artifact,) = _load(
        "[artifacts.x]\n"
        'build = ["rust"]\n'
        'platforms = ["darwin-arm64", "linux-x86_64"]\n'
        'bundle = { composition = "archive" }\n'
        "sign = true\n"
    )
    assert artifact.sign is True


def test_sign_without_a_bundle_is_refused():
    # The signer reopens what the bundle stage composed (workflows.lex §3.1);
    # a sign declaration with no bundle would emit a sign matrix entry whose
    # leg has no bundle tree to download — a deep-CI failure the parse
    # boundary catches instead (TOL02-WS08 #779).
    with pytest.raises(
        config.ConfigError,
        match=r"sign = true requires a bundle composition the signer can "
        r"reopen \(archive, mac-app\); got no bundle",
    ):
        _load(
            "[artifacts.x]\n"
            'build = ["rust"]\n'
            'platforms = ["darwin-arm64"]\n'
            "sign = true\n"
        )


def test_sign_with_an_unsignable_composition_is_refused():
    # The signer has legs for the mac-app payload and the archive tarball
    # only; `sign = true` over a wheel (or deb) composition routes nowhere —
    # refused at parse, naming the signable set.
    with pytest.raises(
        config.ConfigError,
        match=r"sign = true requires a bundle composition the signer can "
        r"reopen \(archive, mac-app\); got composition `wheel`",
    ):
        _load(
            "[artifacts.x]\n"
            'build = ["python"]\n'
            'platforms = ["darwin-arm64"]\n'
            'bundle = { composition = "wheel" }\n'
            "sign = true\n"
        )


def test_bundle_without_a_build_target_is_refused():
    # The bundle twin of the sign rule: a bundle composes build outputs, so on a
    # no-build artifact the stage never materializes yet the declaration reads
    # as intent. Refused at parse rather than silently dropped.
    with pytest.raises(
        config.ConfigError, match=r"bundle requires at least one build target"
    ):
        _load('[artifacts.x]\nbundle = { composition = "archive" }\n')


def test_bundle_shape_error_precedes_the_build_requirement():
    # A malformed bundle still gets its specific composition-shape error first —
    # the build-requirement check is ordered after the composition parse.
    with pytest.raises(config.ConfigError, match="bundle must name its composition"):
        _load("[artifacts.x]\nbundle = {}\n")


def test_sign_without_a_build_target_is_refused():
    # An artifact with no build produces nothing to sign, so preflight emits no
    # matrix entry (and no sign stage) for it while gh-setup would still demand
    # the Apple secrets — the two consumers disagreeing. Refused at parse.
    with pytest.raises(
        config.ConfigError, match=r"sign = true requires at least one build target"
    ):
        _load('[artifacts.x]\nplatforms = ["darwin-arm64"]\nsign = true\n')


def test_sign_without_a_darwin_platform_is_refused():
    # A linux-only signing declaration would silently ship UNSIGNED (no darwin
    # lane → no sign stage) while gh-setup still demands the Apple secrets — the
    # two consumers disagreeing. Refused at parse (story 28).
    with pytest.raises(
        config.ConfigError, match=r"sign = true requires at least one darwin platform"
    ):
        _load(
            '[artifacts.x]\nbuild = ["rust"]\nplatforms = ["linux-x86_64"]\nsign = true\n'
        )


def test_sign_with_default_platforms_is_refused():
    # An undeclared `platforms` defaults to the linux lane — non-darwin — so a
    # build-bearing `sign = true` is refused the same way a linux-only one is.
    with pytest.raises(
        config.ConfigError, match=r"sign = true requires at least one darwin platform"
    ):
        _load('[artifacts.x]\nbuild = ["rust"]\nsign = true\n')


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
