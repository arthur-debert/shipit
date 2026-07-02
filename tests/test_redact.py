"""Tests for :mod:`shipit.redact` — the central redactor.

Registered exact values and pattern rules (GitHub token shapes, PEM blocks) are
masked; non-secret text passes through untouched; degenerate registrations
(empty / too short) can never shred the record.
"""

from __future__ import annotations

import pytest

from shipit import redact


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    """Each test starts with a clean value registry (module state otherwise
    persists for the process lifetime, by design)."""
    monkeypatch.setattr(redact, "_registered", set())


def test_registered_value_is_masked():
    redact.register("hunter2-secret")
    out = redact.redact("connecting with token hunter2-secret over https")
    assert "hunter2-secret" not in out
    assert redact.MASK in out


def test_longest_registered_value_masks_whole(monkeypatch):
    # A value containing another registered value is masked WHOLE — longest
    # first — never left half-recognizable.
    redact.register("abc123")
    redact.register("abc123-and-more")
    out = redact.redact("x abc123-and-more y")
    assert out == f"x {redact.MASK} y"


def test_github_token_patterns_masked_without_registration():
    for token in ("ghp_abc123DEF", "ghs_XyZ987", "github_pat_11AAAA_bbbb"):
        out = redact.redact(f"Authorization: {token}")
        assert token not in out, token
        assert redact.MASK in out


def test_pem_block_masked():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA7\nmorekeymaterial\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact.redact(f"loaded key:\n{pem}\ndone")
    assert "keymaterial" not in out
    assert redact.MASK in out
    assert "done" in out


def test_non_secret_text_untouched():
    text = "git -C /repo fetch origin main (rc=0, 123ms)"
    assert redact.redact(text) == text


def test_empty_and_short_values_are_ignored():
    redact.register(None)
    redact.register("")
    redact.register("abc")  # below the minimum length — masking it would shred text
    assert redact.redact("abc def") == "abc def"
