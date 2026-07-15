"""The ``[artifact-deps]`` map loader (ARF01-WS02 #952) — typed frozen values at
the config boundary (ADR-0030: construction is validation).

The CONSUMER half of the Artifact channel: a downstream repo declares a
cross-repo artifact-pinned dependency, parsed to a typed
:class:`~shipit.config.ArtifactDep`. Fixture-driven, prior art the
``[toolchains]``/``[artifacts]`` loader tests — happy shapes in (TOML → typed
values), loud malformed-config errors naming the offending key/path.
"""

import dataclasses
import tomllib

import pytest

from shipit import config


def _load(text: str) -> tuple[config.ArtifactDep, ...]:
    return config.load_artifact_deps(tomllib.loads(text))


# --------------------------------------------------------------------------
# Happy shapes
# --------------------------------------------------------------------------


def test_absent_table_is_the_empty_tuple():
    # A repo declaring no cross-repo artifact pin projects no managed block.
    assert config.load_artifact_deps({}) == ()


def test_minimal_dep_parses_to_typed_frozen_value():
    (dep,) = _load(
        '[artifact-deps.lexd-lsp]\nrepo = "lex-fmt/lex"\nversion = "0.19.3"\n'
    )
    assert dep == config.ArtifactDep(
        package="lexd-lsp", repo="lex-fmt/lex", version="0.19.3", feature=None
    )


def test_optional_feature_is_carried():
    (dep,) = _load(
        "[artifact-deps.lexd]\n"
        'repo = "lex-fmt/lex"\n'
        'version = "0.19.*"\n'
        'feature = "lint"\n'
    )
    assert dep.feature == "lint"


def test_repo_slug_is_canonicalized_lowercased():
    # repo_from_slug lowercases owner/name so a cased declaration matches the
    # resolved identity (the channel URL is derived from this slug).
    (dep,) = _load('[artifact-deps.lexd]\nrepo = "Lex-Fmt/Lex"\nversion = "1.0.0"\n')
    assert dep.repo == "lex-fmt/lex"


def test_declaration_order_is_preserved():
    deps = _load(
        "[artifact-deps.lexd]\n"
        'repo = "lex-fmt/lex"\n'
        'version = "1.0.0"\n'
        "[artifact-deps.lexd-lsp]\n"
        'repo = "lex-fmt/lex"\n'
        'version = "1.0.0"\n'
    )
    assert [d.package for d in deps] == ["lexd", "lexd-lsp"]


def test_value_is_frozen():
    (dep,) = _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = "1.0.0"\n')
    with pytest.raises(dataclasses.FrozenInstanceError):
        dep.version = "2.0.0"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Loud malformed-config errors (construction is validation)
# --------------------------------------------------------------------------


def test_non_table_section_is_refused():
    with pytest.raises(config.ConfigError, match=r"must be a table"):
        config.load_artifact_deps({"artifact-deps": {"lexd": "0.1.0"}})


def test_missing_repo_is_refused():
    with pytest.raises(config.ConfigError, match=r"\.repo must be"):
        _load('[artifact-deps.lexd]\nversion = "1.0.0"\n')


def test_malformed_repo_slug_is_refused_naming_the_key():
    with pytest.raises(config.ConfigError, match=r"\[artifact-deps\].lexd.repo"):
        _load('[artifact-deps.lexd]\nrepo = "not-a-slug"\nversion = "1.0.0"\n')


def test_missing_version_is_refused():
    with pytest.raises(config.ConfigError, match=r"\.version must be"):
        _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\n')


def test_empty_version_is_refused():
    with pytest.raises(config.ConfigError, match=r"\.version must be"):
        _load('[artifact-deps.lexd]\nrepo = "lex-fmt/lex"\nversion = ""\n')


def test_unknown_key_is_refused_naming_it():
    with pytest.raises(config.ConfigError, match=r"unknown key `channel`"):
        _load(
            "[artifact-deps.lexd]\n"
            'repo = "lex-fmt/lex"\n'
            'version = "1.0.0"\n'
            'channel = "https://example.com"\n'
        )


def test_dotted_package_and_feature_names_are_admitted():
    # Dots are valid in a conda package name (the producer's vocabulary admits
    # them), so a `[artifact-deps."foo.bar"]` declaration and a dotted `feature`
    # must PARSE — the projection quotes them as TOML keys at emission rather
    # than the parser rejecting them (ARF01-WS02 review).
    (dep,) = _load(
        '[artifact-deps."ruamel.yaml"]\n'
        'repo = "lex-fmt/lex"\n'
        'version = "0.19.3"\n'
        'feature = "tools.v2"\n'
    )
    assert dep.package == "ruamel.yaml"
    assert dep.feature == "tools.v2"


def test_malformed_feature_name_is_refused():
    with pytest.raises(config.ConfigError, match=r"\.feature must be"):
        _load(
            "[artifact-deps.lexd]\n"
            'repo = "lex-fmt/lex"\n'
            'version = "1.0.0"\n'
            'feature = "has spaces"\n'
        )


def test_malformed_package_key_is_refused():
    with pytest.raises(config.ConfigError, match=r"package name"):
        config.load_artifact_deps(
            {"artifact-deps": {"bad key": {"repo": "a/b", "version": "1"}}}
        )


def test_artifact_deps_is_a_known_top_level_table():
    # The closed registry accepts it (a typo like [artifact-dep] still dies).
    cfg = tomllib.loads('[artifact-deps.lexd]\nrepo = "a/b"\nversion = "1"\n')
    config._validate_known_tables(cfg)  # does not raise
    with pytest.raises(config.ConfigError, match=r"unknown top-level table"):
        config._validate_known_tables({"artifact-dep": {}})
