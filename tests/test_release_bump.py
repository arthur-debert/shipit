import pytest

from shipit.release import ReleaseError, bump
from shipit.tools import registry


def test_registry_mirrors_the_toolchain_set():
    assert set(bump.ADAPTERS) == set(registry.names())


def test_tauri_is_never_a_dispatch_label():
    assert "tauri" not in bump.ADAPTERS


def test_rust_command_lines():
    assert bump.adapter_for("rust").commands("1.2.3") == (
        ("cargo", "set-version", "--workspace", "1.2.3"),
        ("cargo", "update", "--workspace"),
    )


def test_rust_stages_workspace_manifests_and_lock():
    assert bump.adapter_for("rust").stage == (
        "Cargo.toml",
        "**/Cargo.toml",
        "Cargo.lock",
    )


def test_npm_command_line():
    assert bump.adapter_for("npm").commands("2.0.0-rc.1") == (
        ("npm", "version", "2.0.0-rc.1", "--no-git-tag-version"),
    )


def test_python_is_a_pure_edit_with_no_commands():
    adapter = bump.adapter_for("python")
    assert adapter.commands("1.2.3") == ()
    assert adapter.edit_path == "pyproject.toml"
    assert adapter.stage == ("pyproject.toml",)


def test_go_is_a_first_class_zero_file_adapter():
    adapter = bump.adapter_for("go")
    assert adapter.commands("1.2.3") == ()
    assert adapter.edit_path is None
    assert adapter.stage == ()
    assert not adapter.projects_files


def test_tree_sitter_is_a_zero_file_adapter():
    adapter = bump.adapter_for("tree-sitter")
    assert adapter.commands("1.2.3") == ()
    assert adapter.edit_path is None
    assert adapter.stage == ()
    assert not adapter.projects_files


def test_lua_is_a_pure_edit_of_the_plugin_entry_file():
    adapter = bump.adapter_for("lua")
    assert adapter.commands("1.2.3") == ()
    assert adapter.edit_path == "init.lua"
    assert adapter.stage == ("init.lua",)
    assert adapter.projects_files


def test_adapter_for_unknown_toolchain_is_loud():
    with pytest.raises(ReleaseError, match="no bump adapter"):
        bump.adapter_for("tauri")


_PYPROJECT = """\
[build-system]
requires = ["hatchling"]

[project]
name = "demo"
authors = [{ name = "A" }]
version = "0.1.0"
description = "a [bracketed] description"

[tool.other]
version = "9.9.9"
"""


def test_bump_pyproject_rewrites_only_the_project_version():
    out = bump.bump_pyproject(_PYPROJECT, "0.2.0")
    assert 'version = "0.2.0"' in out
    assert 'version = "9.9.9"' in out
    assert out == _PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"')


def test_bump_pyproject_crosses_arrays_but_not_tables():
    text = '[project]\nname = "x"\nclassifiers = [\n  "A :: B",\n]\nversion = "1.0.0"\n'
    assert 'version = "2.0.0"' in bump.bump_pyproject(text, "2.0.0")


def test_bump_pyproject_preserves_single_quote_style():
    text = "[project]\nname = 'x'\nversion = '1.0.0'\n"
    assert (
        bump.bump_pyproject(text, "2.0.0")
        == "[project]\nname = 'x'\nversion = '2.0.0'\n"
    )


def test_bump_pyproject_without_project_version_is_loud():
    with pytest.raises(ReleaseError, match="no \\[project\\] version"):
        bump.bump_pyproject('[project]\nname = "x"\ndynamic = ["version"]\n', "1.0.0")


def test_bump_pyproject_ignores_version_of_other_tables_only():
    with pytest.raises(ReleaseError):
        bump.bump_pyproject(
            '[project]\nname = "x"\n\n[tool.y]\nversion = "1.0"\n', "2.0.0"
        )


_INIT_LUA = """\
local M = {}

M.version = "0.1.0"

function M.setup(opts)
  return opts
end

return M
"""


def test_bump_lua_version_rewrites_the_module_version_verbatim():
    out = bump.bump_lua_version(_INIT_LUA, "0.2.0")
    assert 'M.version = "0.2.0"' in out
    assert out == _INIT_LUA.replace('M.version = "0.1.0"', 'M.version = "0.2.0"')


def test_bump_lua_version_writes_a_prerelease_semver_verbatim():
    out = bump.bump_lua_version('M.version = "0.0.0"\n', "1.0.0-rc.1")
    assert out == 'M.version = "1.0.0-rc.1"\n'


def test_bump_lua_version_preserves_single_quote_style():
    assert (
        bump.bump_lua_version("M.version = '1.0.0'\n", "2.0.0")
        == "M.version = '2.0.0'\n"
    )


def test_bump_lua_version_bumps_only_the_first_occurrence():
    text = 'M.version = "0.1.0"\nM.version = "9.9.9"\n'
    assert bump.bump_lua_version(text, "0.2.0") == (
        'M.version = "0.2.0"\nM.version = "9.9.9"\n'
    )


def test_bump_lua_version_skips_a_leading_comment_line():
    text = (
        '-- M.version = "0.0.1" (old, kept as a note)\n'
        "local M = {}\n"
        'M.version = "0.1.0"\n'
        "return M\n"
    )
    out = bump.bump_lua_version(text, "0.2.0")
    assert '-- M.version = "0.0.1" (old, kept as a note)' in out
    assert 'M.version = "0.2.0"' in out
    assert out == text.replace('M.version = "0.1.0"', 'M.version = "0.2.0"')


def test_bump_lua_version_skips_a_version_inside_a_string():
    text = 'local doc = "M.version = 9.9.9"\nM.version = "0.1.0"\n'
    out = bump.bump_lua_version(text, "0.2.0")
    assert 'local doc = "M.version = 9.9.9"' in out
    assert out == 'local doc = "M.version = 9.9.9"\nM.version = "0.2.0"\n'


def test_bump_lua_version_inserts_the_value_literally_not_as_a_backreference():
    assert bump.bump_lua_version('M.version = "0.0.0"\n', r"1.0.0-\g<head>") == (
        'M.version = "1.0.0-\\g<head>"\n'
    )


def test_bump_lua_version_bumps_an_indented_assignment():
    assert bump.bump_lua_version('\tM.version = "0.1.0"\n', "0.2.0") == (
        '\tM.version = "0.2.0"\n'
    )


def test_bump_lua_version_ignores_a_longer_identifier():
    text = 'someM.version = "0.1.0"\n'
    with pytest.raises(ReleaseError, match=r"M\.version"):
        bump.bump_lua_version(text, "0.2.0")


def test_bump_lua_version_without_a_version_line_is_loud():
    with pytest.raises(ReleaseError, match=r"M\.version"):
        bump.bump_lua_version("local M = {}\nreturn M\n", "1.0.0")


def test_edit_for_dispatches_lua_to_the_lua_rewrite():
    adapter = bump.adapter_for("lua")
    assert bump.edit_for(adapter, 'M.version = "0.1.0"\n', "0.2.0") == (
        'M.version = "0.2.0"\n'
    )


@pytest.mark.parametrize(
    ("semver", "pep440"),
    [
        ("1.0.0", "1.0.0"),
        ("0.1.0", "0.1.0"),
        ("10.20.30", "10.20.30"),
        ("1.0.0-release-rc", "1.0.0rc0"),
        ("2.3.4-release-rc.2", "2.3.4rc2"),
        ("1.2.3-rc.1", "1.2.3rc1"),
        ("1.2.3-alpha.2", "1.2.3a2"),
        ("1.2.3-beta.3", "1.2.3b3"),
        ("1.2.3-c.1", "1.2.3rc1"),
        ("1.2.3-preview.5", "1.2.3rc5"),
        ("1.2.3-rc", "1.2.3rc0"),
        ("1.2.3-rc1", "1.2.3rc1"),
        ("1.2.3-alpha", "1.2.3a0"),
        ("1.2.3-Beta.4", "1.2.3b4"),
    ],
)
def test_to_pep440_maps_semver_to_pep440(semver, pep440):
    assert bump.to_pep440(semver) == pep440


@pytest.mark.parametrize(
    "bad",
    [
        "1.2.3-snapshot.1",
        "1.2.3-dev.1",
        "1.2.3-1",
        "1.2.3-rc.1.2",
        "1.2.3-rc.foo",
    ],
)
def test_to_pep440_refuses_unmappable_suffix_loudly(bad):
    with pytest.raises(ReleaseError, match="no PEP 440 mapping"):
        bump.to_pep440(bad)


@pytest.mark.parametrize(
    "annotated",
    [
        "1.0.0+build.1",
        "1.2.3-rc.1+build.1",
    ],
)
def test_to_pep440_refuses_build_metadata_loudly(annotated):
    with pytest.raises(ReleaseError, match="build metadata is not allowed"):
        bump.to_pep440(annotated)


def test_bump_pyproject_normalizes_a_prerelease_to_pep440():
    text = '[project]\nname = "x"\nversion = "0.0.0"\n'
    assert bump.bump_pyproject(text, "1.0.0-release-rc") == (
        '[project]\nname = "x"\nversion = "1.0.0rc0"\n'
    )


def test_bump_pyproject_refuses_an_unmappable_prerelease():
    text = '[project]\nname = "x"\nversion = "0.0.0"\n'
    with pytest.raises(ReleaseError, match="no PEP 440 mapping"):
        bump.bump_pyproject(text, "1.0.0-snapshot.1")


_TAURI_CONF = """{
  "productName": "demo",
  "version": "0.1.0",
  "app": {
    "windows": [{ "title": "demo", "version": "ignored" }]
  }
}
"""


def test_bump_bundle_config_rewrites_top_level_version_preserving_format():
    out = bump.bump_bundle_config(_TAURI_CONF, "0.2.0")
    assert out == _TAURI_CONF.replace('"version": "0.1.0"', '"version": "0.2.0"')


def test_bump_bundle_config_rejects_non_json():
    with pytest.raises(ReleaseError, match="not valid JSON"):
        bump.bump_bundle_config("nope {", "1.0.0")


def test_bump_bundle_config_requires_top_level_version():
    with pytest.raises(ReleaseError, match='no top-level string "version"'):
        bump.bump_bundle_config('{"productName": "x"}', "1.0.0")


def test_bump_bundle_config_refuses_a_nested_first_version():
    text = '{"app": {"version": "0.0.9"}, "version": "0.1.0"}'
    with pytest.raises(ReleaseError, match="not the top-level"):
        bump.bump_bundle_config(text, "0.2.0")


def test_missing_cargo_set_version_gets_the_reconcile_remedy():
    message = bump.explain_command_failure(
        ("cargo", "set-version", "--workspace", "1.2.3"),
        "error: no such command: `set-version`",
    )
    assert message is not None
    assert "cargo-edit" in message
    assert "`shipit install --pr`" in message
    assert "`shipit install --local`" in message
    assert "pixi.toml#shipit-rust-release-deps" in message
    assert "pixi.lock" in message
    assert "cargo install" not in message


def test_a_different_cargo_set_version_failure_stays_untranslated():
    assert (
        bump.explain_command_failure(
            ("cargo", "set-version", "--workspace", "1.2.3"),
            "error: failed to parse manifest at Cargo.toml",
        )
        is None
    )


def test_other_commands_never_match_even_with_the_marker():
    assert (
        bump.explain_command_failure(("npm", "version", "1.2.3"), "no such command")
        is None
    )
    assert (
        bump.explain_command_failure(
            ("cargo", "update", "--workspace"), "error: no such command: `whatever`"
        )
        is None
    )
