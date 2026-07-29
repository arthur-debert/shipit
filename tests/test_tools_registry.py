import pytest

from shipit.tools import registry


def test_registry_is_the_closed_prd_set_in_stable_order():
    assert registry.names() == (
        "rust",
        "go",
        "python",
        "npm",
        "tree-sitter",
        "lua",
    )


def test_lua_is_a_buildless_test_only_toolchain():
    lua = registry.toolchain("lua")
    assert lua is not None
    assert lua.test == ("busted",)
    assert lua.build == ()


def test_tree_sitter_slots_are_generate_and_corpus_test():
    ts = registry.toolchain("tree-sitter")
    assert ts is not None
    assert ts.build == ("tree-sitter", "generate")
    assert ts.test == ("tree-sitter", "test")


def test_default_test_commands_are_the_blessed_runners():
    by_name = {tc.name: tc for tc in registry.TOOLCHAINS}
    assert by_name["rust"].test == ("cargo", "nextest", "run")
    assert by_name["go"].test == ("go", "test", "./...")
    assert by_name["python"].test == ("pytest",)
    assert by_name["npm"].test == ("npm", "test")


def test_lookup_by_name_and_unregistered_is_none():
    assert registry.toolchain("rust") is registry.RUST
    assert registry.toolchain("tauri") is None


def test_default_build_commands_are_the_legacy_single_target_builds():
    by_name = {tc.name: tc for tc in registry.TOOLCHAINS}
    assert by_name["rust"].build == ("cargo", "build", "--release")
    assert by_name["go"].build == (
        "go",
        "build",
        "-trimpath",
        "-ldflags",
        "-s -w",
        "./...",
    )
    assert by_name["python"].build == ("uv", "build")
    assert by_name["npm"].build == ("npm", "run", "build")


def test_command_accessor_serves_each_tool_slot():
    assert registry.RUST.command(registry.TOOL_TEST) == ("cargo", "nextest", "run")
    assert registry.RUST.command(registry.TOOL_BUILD) == ("cargo", "build", "--release")


def test_command_accessor_rejects_an_unknown_tool_slot():
    with pytest.raises(registry.UnknownToolError, match="known: test, build"):
        registry.RUST.command("bundle")
