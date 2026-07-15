"""The closed toolchain registry (TOL01-WS01) — dispatch defaults per tool slot.

Prior art: the lint ``LANGS`` registry tests. The registry is a pure value —
fixture-driven assertions over its entries and lookups, no I/O.
"""

import pytest

from shipit.tools import registry


def test_registry_is_the_closed_prd_set_in_stable_order():
    # PRD story 3 + ADR-0039: rust / go / python / npm, an entry each; plus
    # tree-sitter, the bespoke generated-parser toolchain (TOL02-WS16 #792);
    # plus lua, the Neovim-plugin toolchain (TOL03-WS01 #972).
    assert registry.names() == (
        "rust",
        "go",
        "python",
        "npm",
        "tree-sitter",
        "lua",
    )


def test_lua_is_a_buildless_test_only_toolchain():
    # TOL03-WS01 #972: the lua toolchain's test slot is `busted` (the
    # luarocks-standard nvim-plugin spec runner); its build slot is EMPTY — a
    # Neovim plugin has no compile step (the first buildless toolchain, the
    # build analogue of the go/tree-sitter zero-file bump adapters).
    lua = registry.toolchain("lua")
    assert lua is not None
    assert lua.test == ("busted",)
    assert lua.build == ()


def test_tree_sitter_slots_are_generate_and_corpus_test():
    # TOL02-WS16 #792 / legacy tree-sitter.yml@v3: build = `tree-sitter
    # generate` (regenerate the parser from grammar.js — the whole-leg build a
    # tarball bundles), test = `tree-sitter test` (the corpus assertions a
    # corpus lane runs).
    ts = registry.toolchain("tree-sitter")
    assert ts is not None
    assert ts.build == ("tree-sitter", "generate")
    assert ts.test == ("tree-sitter", "test")


def test_default_test_commands_are_the_blessed_runners():
    # rust -> cargo-nextest, go -> go test ./..., python -> pytest (PRD story
    # 3); npm -> the package's own test script (ADR-0039's registry sketch —
    # the PRD pins no npm default, the entry's docstring records the choice).
    by_name = {tc.name: tc for tc in registry.TOOLCHAINS}
    assert by_name["rust"].test == ("cargo", "nextest", "run")
    assert by_name["go"].test == ("go", "test", "./...")
    assert by_name["python"].test == ("pytest",)
    assert by_name["npm"].test == ("npm", "test")


def test_lookup_by_name_and_unregistered_is_none():
    assert registry.toolchain("rust") is registry.RUST
    assert registry.toolchain("tauri") is None  # never a Kind/dispatch label


def test_default_build_commands_are_the_legacy_single_target_builds():
    # Issue #555's legacy digest: rust -> the build-binaries job's release
    # build, go -> go-cli's static stripped form over EVERY package (the test
    # slot's ./... form, #608: a bare `go build` compiles only the root
    # package and reds any repo whose packages live under subdirs; env/version
    # shaping lives in tools.build, not here), python -> uv build, npm -> the
    # package's own build script (same deference as the test slot).
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
    # The tool-slot vocabulary is CLOSED (test + build); a slot outside it is
    # a caller bug, named loudly.
    with pytest.raises(registry.UnknownToolError, match="known: test, build"):
        registry.RUST.command("bundle")
