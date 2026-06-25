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
