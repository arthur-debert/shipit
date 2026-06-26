"""Tests for `shipit.review.ghauth` — the Doppler-sourced, in-memory App auth.

The decided divergence from release-core: the App private key (PEM) AND the app
id come from Doppler via `shipit.secretsrc` (never disk), and PyJWT signs the App
JWT from the in-memory PEM string. These tests assert exactly that:

  * the PEM + app id are sourced via `secretsrc.doppler_get` under the
    agent-derived key names;
  * the JWT is signed in-memory (PyJWT `encode` receives the PEM string), and the
    code never touches the filesystem;
  * a missing PyJWT yields a clean `ReviewAuthError` (the install hint), not an
    ImportError leaking out;
  * the 3-hop installation-token flow uses the bearer-JWT urllib seams.

Every boundary (Doppler, PyJWT, urllib) is mocked — no network, no real LLM, no
disk.
"""

from __future__ import annotations

import builtins

import pytest

from shipit.review import ghauth


@pytest.fixture
def doppler_stub(monkeypatch):
    """Stub `secretsrc.doppler_get` to record requested keys + serve fake values."""
    served = {
        "CODEX_REVIEW_APP_PRIVATE_KEY": "-----BEGIN PEM-----\ncodexkey\n-----END PEM-----",
        "CODEX_REVIEW_APP_ID": "111111",
        "AGY_REVIEW_APP_PRIVATE_KEY": "-----BEGIN PEM-----\nagykey\n-----END PEM-----",
        "AGY_REVIEW_APP_ID": "222222",
    }
    requested: list[str] = []

    def fake_get(key: str) -> str:
        requested.append(key)
        return served[key]

    monkeypatch.setattr(ghauth.secretsrc, "doppler_get", fake_get)
    return requested, served


def _stub_jwt(monkeypatch):
    """Install a fake `jwt` module whose `encode` records its (payload, key)."""
    import types

    fake = types.SimpleNamespace()
    captured: dict = {}

    def encode(payload, key, algorithm):
        captured["payload"] = payload
        captured["key"] = key
        captured["algorithm"] = algorithm
        return "signed.jwt.token"

    fake.encode = encode
    monkeypatch.setitem(__import__("sys").modules, "jwt", fake)
    return captured


def test_make_app_jwt_sources_from_doppler_and_signs_in_memory(
    monkeypatch, doppler_stub
):
    requested, served = doppler_stub
    captured = _stub_jwt(monkeypatch)

    token = ghauth.make_app_jwt("codex")

    assert token == "signed.jwt.token"
    # PEM + app id came from Doppler under the agent-derived key names.
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in requested
    assert "CODEX_REVIEW_APP_ID" in requested
    # Signed FROM THE IN-MEMORY PEM STRING (not a file path), RS256.
    assert captured["key"] == served["CODEX_REVIEW_APP_PRIVATE_KEY"]
    assert captured["algorithm"] == "RS256"
    # `iss` is the app id stringified (PyJWT ≥2.10 requires a string iss).
    assert captured["payload"]["iss"] == "111111"
    assert captured["payload"]["exp"] > captured["payload"]["iat"]


def test_make_app_jwt_agy_uses_agy_keys(monkeypatch, doppler_stub):
    requested, served = doppler_stub
    captured = _stub_jwt(monkeypatch)
    ghauth.make_app_jwt("agy")
    assert "AGY_REVIEW_APP_PRIVATE_KEY" in requested
    assert captured["key"] == served["AGY_REVIEW_APP_PRIVATE_KEY"]
    assert captured["payload"]["iss"] == "222222"


def test_make_app_jwt_never_reads_disk(monkeypatch, doppler_stub):
    """The PEM never lands on disk: `open` is not called during JWT minting."""
    _stub_jwt(monkeypatch)
    real_open = builtins.open

    def guard_open(*args, **kwargs):  # pragma: no cover - only fires on a regression
        raise AssertionError(f"ghauth must not open a file: {args!r}")

    monkeypatch.setattr(builtins, "open", guard_open)
    try:
        ghauth.make_app_jwt("codex")
    finally:
        monkeypatch.setattr(builtins, "open", real_open)


def test_make_app_jwt_clean_error_when_pyjwt_absent(monkeypatch, doppler_stub):
    """A missing PyJWT surfaces a clean ReviewAuthError with the install hint."""
    real_import = builtins.__import__

    def no_jwt(name, *args, **kwargs):
        if name == "jwt":
            raise ImportError("No module named 'jwt'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_jwt)
    with pytest.raises(ghauth.ReviewAuthError, match="pyjwt"):
        ghauth.make_app_jwt("codex")


def test_unknown_agent_is_a_clean_error(monkeypatch, doppler_stub):
    _stub_jwt(monkeypatch)
    with pytest.raises(ghauth.ReviewAuthError, match="No GitHub App credentials"):
        ghauth.make_app_jwt("bogus")


def test_doppler_failure_is_normalized(monkeypatch):
    """A Doppler-sourcing failure becomes a clean ReviewAuthError, not a leak."""
    _stub_jwt(monkeypatch)

    def boom(key):
        raise ghauth.secretsrc.SecretSourceError("doppler not found on PATH")

    monkeypatch.setattr(ghauth.secretsrc, "doppler_get", boom)
    with pytest.raises(ghauth.ReviewAuthError, match="from\n? *Doppler|from Doppler"):
        ghauth.make_app_jwt("codex")


def test_installation_token_runs_the_three_hops(monkeypatch, doppler_stub):
    """JWT → installation id → access token, via the urllib bearer-JWT seams."""
    _stub_jwt(monkeypatch)
    gets: list[str] = []
    posts: list[str] = []

    def fake_get(path, token):
        gets.append(path)
        assert token == "signed.jwt.token"
        return {"id": 42}

    def fake_post(path, token):
        posts.append(path)
        return {"token": "ghs_installation_tok"}

    monkeypatch.setattr(ghauth, "_api_get", fake_get)
    monkeypatch.setattr(ghauth, "_api_post", fake_post)

    tok = ghauth.installation_token("codex", "owner/repo")
    assert tok == "ghs_installation_tok"
    assert gets == ["/repos/owner/repo/installation"]
    assert posts == ["/app/installations/42/access_tokens"]


def test_installation_auth_returns_token_and_granted_permissions(
    monkeypatch, doppler_stub
):
    """`installation_auth` returns the WHOLE access-tokens response — crucially the
    `permissions` scope map the OBS02 funnel verify harness reads `checks: write`
    from — while `installation_token` delegates to it for just the token string."""
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    monkeypatch.setattr(
        ghauth,
        "_api_post",
        lambda path, token: {
            "token": "ghs_installation_tok",
            "permissions": {"checks": "write", "pull_requests": "write"},
        },
    )

    auth = ghauth.installation_auth("codex", "owner/repo")
    assert auth["token"] == "ghs_installation_tok"
    assert auth["permissions"]["checks"] == "write"
    # The string-only helper rides the same mint.
    assert ghauth.installation_token("codex", "owner/repo") == "ghs_installation_tok"


def test_installation_auth_raises_when_no_token(monkeypatch, doppler_stub):
    """A response without a `token` is a clean ReviewAuthError, not a silent {}."""
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    monkeypatch.setattr(ghauth, "_api_post", lambda path, token: {"permissions": {}})
    with pytest.raises(ghauth.ReviewAuthError, match="no\n? *'token'|no 'token'"):
        ghauth.installation_auth("codex", "owner/repo")


def test_installation_id_404_is_actionable(monkeypatch, doppler_stub):
    _stub_jwt(monkeypatch)

    def not_installed(path, token):
        raise ghauth.ReviewAuthError("GitHub API GET /x failed (HTTP 404): nope")

    monkeypatch.setattr(ghauth, "_api_get", not_installed)
    with pytest.raises(ghauth.ReviewAuthError, match="not installed"):
        ghauth.installation_id("codex", "owner/repo", jwt="signed.jwt.token")
