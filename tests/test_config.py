"""Unit tests for .shipit.toml parsing — the [secrets] map."""

import tomllib

import pytest

from shipit import config


def _secrets(toml: str) -> list[config.SecretSource]:
    return config.load_secrets(tomllib.loads(toml))


def test_load_secrets_each_source_kind():
    sources = _secrets(
        """
        [secrets]
        A = { doppler = "KEY_A" }
        B = { env = "VAR_B" }
        C = { prompt = true }
        D = { doppler = "KEY_D", optional = true }
        """
    )
    by_name = {s.name: s for s in sources}
    assert by_name["A"] == config.SecretSource("A", "doppler", "KEY_A", False)
    assert by_name["B"] == config.SecretSource("B", "env", "VAR_B", False)
    assert by_name["C"] == config.SecretSource("C", "prompt", None, False)
    assert by_name["D"].optional is True
    # Declaration order preserved.
    assert [s.name for s in sources] == ["A", "B", "C", "D"]


def test_missing_secrets_table_is_empty():
    assert config.load_secrets({}) == []


def test_two_sources_rejected():
    with pytest.raises(config.ConfigError, match="exactly one source"):
        _secrets('[secrets]\nA = { doppler = "K", env = "V" }\n')


def test_no_source_rejected():
    with pytest.raises(config.ConfigError, match="exactly one source"):
        _secrets("[secrets]\nA = { optional = true }\n")


def test_prompt_must_be_true():
    with pytest.raises(config.ConfigError, match="prompt must be"):
        _secrets("[secrets]\nA = { prompt = false }\n")


def test_non_table_entry_rejected():
    with pytest.raises(config.ConfigError, match="inline table"):
        _secrets('[secrets]\nA = "just-a-string"\n')


def test_empty_key_rejected():
    with pytest.raises(config.ConfigError, match="non-empty string"):
        _secrets('[secrets]\nA = { doppler = "" }\n')


def test_load_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="no .shipit.toml"):
        config.load(tmp_path / "nope.toml")


def test_load_roundtrip(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[secrets]\nA = { env = "X" }\n')
    cfg = config.load(p)
    assert config.load_secrets(cfg)[0].name == "A"


def test_unknown_top_level_table_rejected(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[secretz]\nA = { env = "X" }\n')
    with pytest.raises(config.ConfigError, match="unknown top-level table `secretz`"):
        config.load(p)


def test_known_tables_load(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[secrets]\nA = { env = "X" }\n'
        "[reviewers]\ncopilot = {}\n"
        '[shipit]\nversion = "abc"\n'
        '[managed]\n"path" = "sha256:deadbeef"\n'
    )
    cfg = config.load(p)
    assert config.load_secrets(cfg)[0].name == "A"


def test_project_freeform_subtree_not_validated(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        "[project.portfolio]\n"
        'exclude = ["a/b"]\n'
        "[[project.portfolio.repo]]\n"
        'name = "x/y"\n'
        'kind = "anything"\n'
        "[project.whatever.deeply.nested]\n"
        'made_up_key = "fine"\n'
    )
    cfg = config.load(p)
    assert cfg["project"]["portfolio"]["repo"][0]["name"] == "x/y"


def test_custom_escape_hatch_alias_allowed(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[custom.anything]\nmade_up = "fine"\n')
    cfg = config.load(p)
    assert cfg["custom"]["anything"]["made_up"] == "fine"


# --------------------------------------------------------------------------
# The fleet manifest (#449 item 3, ADR-0033): [project.portfolio] carries the
# adoption targets of the ADP fleet sweep (#426) — including shipit-canary,
# the standing test bed — and none of the sweep's non-targets.
# --------------------------------------------------------------------------


def _portfolio_repos() -> set[str]:
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / ".shipit.toml").read_text())
    portfolio = cfg["project"]["portfolio"]
    return {entry["repo"] for stack in portfolio.values() for entry in stack}


def test_portfolio_contains_the_adoption_targets():
    repos = _portfolio_repos()
    # The ADP00 canary and the tool itself are fleet rows (#426).
    assert "arthur-debert/shipit-canary" in repos
    assert "arthur-debert/shipit" in repos
    # The WS07 sweep's other previously-missing targets.
    for slug in (
        "arthur-debert/shellai",
        "arthur-debert/nanodoc",
        "arthur-debert/supage",
        "arthur-debert/falala",
        "arthur-debert/dotcat",
        "arthur-debert/electron-splashguard",
        "arthur-debert/visual-explore",
        "arthur-debert/sprinkles",
        "lex-fmt/mkdocs-lex",
        "phos-editor/phos.photo",
    ):
        assert slug in repos, slug


def test_portfolio_excludes_the_sweeps_non_targets():
    repos = _portfolio_repos()
    # n/a rows in the sweep: docs, editor config, grammar packaging.
    for slug in ("lex-fmt/comms", "lex-fmt/nvim", "lex-fmt/zed-lex"):
        assert slug not in repos, slug


def test_portfolio_entries_carry_repo_and_path():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / ".shipit.toml").read_text())
    for stack, entries in cfg["project"]["portfolio"].items():
        assert isinstance(entries, list) and entries, stack
        for entry in entries:
            assert "repo" in entry and "/" in entry["repo"], (stack, entry)
            assert "path" in entry and entry["path"], (stack, entry)
