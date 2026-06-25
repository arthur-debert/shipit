"""Unit tests for secret-source resolution — the optional-skip rule."""

import pytest

from shipit import secretsrc
from shipit.config import SecretSource


def test_resolve_doppler_injected():
    src = SecretSource("A", "doppler", "KEY", False)
    val = secretsrc.resolve(src, doppler_get=lambda k: f"val:{k}")
    assert val == "val:KEY"


def test_resolve_env():
    src = SecretSource("A", "env", "VAR", False)
    assert secretsrc.resolve(src, env={"VAR": "secret"}) == "secret"


def test_resolve_prompt():
    src = SecretSource("A", "prompt", None, False)
    assert secretsrc.resolve(src, prompt=lambda name: "typed") == "typed"


def test_required_env_missing_raises():
    src = SecretSource("A", "env", "VAR", False)
    with pytest.raises(secretsrc.SecretSourceError, match="VAR not set"):
        secretsrc.resolve(src, env={})


def test_optional_env_missing_skips():
    src = SecretSource("A", "env", "VAR", True)
    assert secretsrc.resolve(src, env={}) is None


def test_optional_doppler_failure_skips():
    def boom(_key):
        raise secretsrc.SecretSourceError("doppler down")

    src = SecretSource("A", "doppler", "KEY", True)
    assert secretsrc.resolve(src, doppler_get=boom) is None


def test_required_doppler_failure_raises():
    def boom(_key):
        raise secretsrc.SecretSourceError("doppler down")

    src = SecretSource("A", "doppler", "KEY", False)
    with pytest.raises(secretsrc.SecretSourceError):
        secretsrc.resolve(src, doppler_get=boom)


def test_prompt_without_prompt_fn_raises():
    src = SecretSource("A", "prompt", None, False)
    with pytest.raises(secretsrc.SecretSourceError, match="interactive prompt"):
        secretsrc.resolve(src, prompt=None)
