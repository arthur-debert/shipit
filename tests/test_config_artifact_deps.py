import dataclasses
import tomllib

import pytest

from shipit import config


def _load(text: str) -> tuple[config.ArtifactDep, ...]:
    return config.load_artifact_deps(tomllib.loads(text))


def test_absent_table_is_the_empty_tuple():
    assert config.load_artifact_deps({}) == ()


def test_minimal_dep_parses_to_typed_frozen_value():
    (dep,) = _load('[artifact-deps.lexd-lsp]\nrepo = "lex-fmt/lex"\n')
    assert dep == config.ArtifactDep(
        package="lexd-lsp", repo="lex-fmt/lex", feature=None
    )


def test_optional_feature_is_carried():
    (dep,) = _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nfeature = "lint"\n')
    assert dep.feature == "lint"


def test_repo_slug_is_canonicalized_lowercased():
    (dep,) = _load('[artifact-deps.lexd]\nrepo = "Lex-Fmt/Lex"\n')
    assert dep.repo == "lex-fmt/lex"


def test_declaration_order_is_preserved():
    deps = _load(
        "[artifact-deps.lexd]\n"
        'repo = "lex-fmt/lex"\n'
        "[artifact-deps.lexd-lsp]\n"
        'repo = "lex-fmt/lex"\n'
    )
    assert [d.package for d in deps] == ["lexd", "lexd-lsp"]


def test_value_is_frozen():
    (dep,) = _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\n')
    with pytest.raises(dataclasses.FrozenInstanceError):
        dep.repo = "other/repo"  # type: ignore[misc]


def test_non_table_section_is_refused():
    with pytest.raises(config.ConfigError, match=r"must be a table"):
        config.load_artifact_deps({"artifact-deps": {"lexd": "0.1.0"}})


def test_missing_repo_is_refused():
    with pytest.raises(config.ConfigError, match=r"\.repo must be"):
        _load("[artifact-deps.lexd]\n")


def test_malformed_repo_slug_is_refused_naming_the_key():
    with pytest.raises(config.ConfigError, match=r"\[artifact-deps\].lexd.repo"):
        _load('[artifact-deps.lexd]\nrepo = "not-a-slug"\n')


def test_legacy_version_key_is_refused_with_a_migration_message():
    with pytest.raises(
        config.ConfigError,
        match=r"version is no longer allowed.*feature\.shipit-artifacts\.dependencies",
    ):
        _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "0.19.3"\n')


def test_unknown_key_is_refused_naming_it():
    with pytest.raises(config.ConfigError, match=r"unknown key `channel`"):
        _load(
            "[artifact-deps.lexd]\n"
            'repo = "lex-fmt/lex"\n'
            'channel = "https://example.com"\n'
        )


def test_dotted_package_and_feature_names_are_admitted():
    (dep,) = _load(
        '[artifact-deps."ruamel.yaml"]\nrepo = "lex-fmt/lex"\nfeature = "tools.v2"\n'
    )
    assert dep.package == "ruamel.yaml"
    assert dep.feature == "tools.v2"


def test_malformed_feature_name_is_refused():
    with pytest.raises(config.ConfigError, match=r"\.feature must be"):
        _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nfeature = "has spaces"\n')


def test_malformed_package_key_is_refused():
    with pytest.raises(config.ConfigError, match=r"package name"):
        config.load_artifact_deps({"artifact-deps": {"bad key": {"repo": "a/b"}}})


def test_uppercase_package_key_is_refused_matching_conda_lowercase_vocabulary():
    with pytest.raises(config.ConfigError, match=r"LOWERCASE"):
        _load('[artifact-deps.LexD]\nrepo = "lex-fmt/lex"\n')


def test_artifact_deps_is_a_known_top_level_table():
    cfg = tomllib.loads('[artifact-deps.lexd]\nrepo = "a/b"\n')
    config._validate_known_tables(cfg)
    with pytest.raises(config.ConfigError, match=r"unknown top-level table"):
        config._validate_known_tables({"artifact-dep": {}})
