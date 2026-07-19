"""The ``[stage]`` map loader (conda-direct #1079) — typed frozen copy entries at
the config boundary (ADR-0030: construction is validation).

The app-consumer half of conda-direct: a `[stage.<pkg>]` table declares
source-in-prefix → dest-under-checkout copy pairs that
:mod:`shipit.staging` runs off the resolved env prefix. Fixture-driven like the
`[artifact-deps]`/`[artifacts]` loader tests — happy shapes in (TOML → typed
values), loud malformed-config errors naming the offending key/path.
"""

import tomllib

import pytest

from shipit import config


def _load(text: str) -> tuple[config.StageEntry, ...]:
    return config.load_stage(tomllib.loads(text))


# --------------------------------------------------------------------------
# Happy shapes
# --------------------------------------------------------------------------


def test_absent_table_is_the_empty_tuple():
    # A repo that stages nothing (a tool-only consumer, or shipit itself) has no
    # copy list.
    assert config.load_stage({}) == ()


def test_tool_binary_entry_parses_to_typed_frozen_value():
    (entry,) = _load('[stage.lexd-lsp]\n"bin/lexd-lsp" = "resources/lexd-lsp"\n')
    assert entry == config.StageEntry(
        package="lexd-lsp", source="bin/lexd-lsp", dest="resources/lexd-lsp"
    )


def test_data_artifact_file_and_directory_entries():
    entries = _load(
        "[stage.tree-sitter-lex]\n"
        '"share/tree-sitter-lex/tree-sitter-lex.wasm" = "resources/tree-sitter-lex.wasm"\n'
        '"share/tree-sitter-lex/queries" = "resources/queries"\n'
    )
    assert entries == (
        config.StageEntry(
            package="tree-sitter-lex",
            source="share/tree-sitter-lex/tree-sitter-lex.wasm",
            dest="resources/tree-sitter-lex.wasm",
        ),
        config.StageEntry(
            package="tree-sitter-lex",
            source="share/tree-sitter-lex/queries",
            dest="resources/queries",
        ),
    )


def test_declaration_order_is_preserved_across_packages_then_entries():
    entries = _load(
        "[stage.tree-sitter-lex]\n"
        '"share/tree-sitter-lex/a.wasm" = "resources/a.wasm"\n'
        '"share/tree-sitter-lex/queries" = "resources/queries"\n'
        "\n"
        "[stage.lexd-lsp]\n"
        '"bin/lexd-lsp" = "resources/lexd-lsp"\n'
    )
    assert [(e.package, e.source) for e in entries] == [
        ("tree-sitter-lex", "share/tree-sitter-lex/a.wasm"),
        ("tree-sitter-lex", "share/tree-sitter-lex/queries"),
        ("lexd-lsp", "bin/lexd-lsp"),
    ]


def test_dotted_conda_package_key_is_accepted():
    # The producer's conda vocabulary admits dots (e.g. `ruamel.yaml`); a dotted
    # `[stage."foo.bar"]` key names one package, not a nested table.
    (entry,) = _load('[stage."foo.bar"]\n"bin/foo" = "resources/foo"\n')
    assert entry.package == "foo.bar"


# --------------------------------------------------------------------------
# Malformed shapes — loud at the boundary (ADR-0030)
# --------------------------------------------------------------------------


def test_uppercase_package_key_is_refused():
    with pytest.raises(config.ConfigError, match="valid conda package identifier"):
        _load('[stage.LexdLsp]\n"bin/x" = "resources/x"\n')


def test_empty_stage_table_is_refused():
    with pytest.raises(config.ConfigError, match="non-empty table"):
        _load("[stage.lexd-lsp]\n")


def test_non_string_destination_is_refused():
    with pytest.raises(config.ConfigError, match="destination must be a non-empty"):
        _load('[stage.lexd-lsp]\n"bin/x" = 7\n')


def test_absolute_destination_escapes_and_is_refused():
    with pytest.raises(config.ConfigError, match="repo-relative POSIX path"):
        _load('[stage.lexd-lsp]\n"bin/x" = "/etc/passwd"\n')


def test_parent_escaping_destination_is_refused():
    with pytest.raises(config.ConfigError, match="repo-relative POSIX path"):
        _load('[stage.lexd-lsp]\n"bin/x" = "../outside"\n')


def test_parent_escaping_source_is_refused():
    with pytest.raises(config.ConfigError, match="repo-relative POSIX path"):
        _load('[stage.lexd-lsp]\n"../../etc/passwd" = "resources/x"\n')


def test_absolute_source_is_refused():
    with pytest.raises(config.ConfigError, match="repo-relative POSIX path"):
        _load('[stage.lexd-lsp]\n"/etc/passwd" = "resources/x"\n')


def test_stage_is_a_known_top_level_table():
    # `[stage]` must be in the closed table registry so it is not rejected as an
    # unknown top-level table (the whole reason a new section needs registering).
    assert "stage" in config._KNOWN_TABLES


def test_stage_scalar_instead_of_table_is_refused():
    with pytest.raises(config.ConfigError, match=r"\[stage\] must be a table"):
        config.load_stage({"stage": 7})
