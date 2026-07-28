"""Tests for `shipit.review.ghauth` — the Doppler-sourced, in-memory App auth.

The decided divergence from release-core: the App private key (PEM) AND the app
id come from Doppler via `shipit.secretsrc` (never disk), and PyJWT signs the App
JWT from the in-memory PEM string. These tests assert exactly that:

  * the PEM + app id are sourced via `secretsrc.doppler_get` under the
    agent-derived key names;
  * the JWT is signed in-memory (PyJWT `encode` receives the PEM string), and the
    code never touches the filesystem;
  * every failure states WHICH KIND it is (#969) — a local credential gap
    (`UNCONFIGURED`), a genuine absent installation (`NOT_INSTALLED`), or a failed
    probe (`API_ERROR`) — and the 404 that means NOT_INSTALLED is read off the
    response STATUS, never grepped out of the message; and
  * the 3-hop installation-token flow uses the bearer-JWT urllib seams.

Every boundary (Doppler, PyJWT, urllib) is mocked — no network, no real LLM, no
disk.
"""

from __future__ import annotations

import builtins
import pathlib
import urllib.error

import pytest

from shipit.agent import backend as agent_backend
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
    """Swap ghauth's bound `jwt` for a fake whose `encode` records (payload, key).

    Patches the module ATTRIBUTE, not `sys.modules`: since #969 PyJWT is a base
    dependency imported at ghauth's module top, so the name is already bound by
    the time a test runs.
    """
    import types

    fake = types.SimpleNamespace()
    captured: dict = {}

    def encode(payload, key, algorithm):
        captured["payload"] = payload
        captured["key"] = key
        captured["algorithm"] = algorithm
        return "signed.jwt.token"

    fake.encode = encode
    monkeypatch.setattr(ghauth, "jwt", fake)
    return captured


def test_make_app_jwt_sources_from_doppler_and_signs_in_memory(
    monkeypatch, doppler_stub
):
    requested, served = doppler_stub
    captured = _stub_jwt(monkeypatch)

    token = ghauth.make_app_jwt(agent_backend.CODEX)

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
    ghauth.make_app_jwt(agent_backend.ANTIGRAVITY)
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
        ghauth.make_app_jwt(agent_backend.CODEX)
    finally:
        monkeypatch.setattr(builtins, "open", real_open)


def test_pyjwt_is_a_base_dependency(monkeypatch, doppler_stub):
    """PyJWT is imported at module top, not lazily behind an install hint (#969).

    The whole point of the base-dependency move: a shipit that imports cannot be a
    shipit that lacks the App-JWT signer, so there is no "missing extra" branch to
    take and no shipit-local pixi env to redirect a consumer into.
    """
    import shipit.review.ghauth as mod

    assert mod.jwt is not None
    src = pathlib.Path(mod.__file__).read_text()
    assert "\nimport jwt\n" in src
    # No remediation may name a pixi env that exists only in shipit's own repo.
    assert "-e review" not in src


def test_signing_failure_is_unconfigured(monkeypatch, doppler_stub):
    """An unusable PEM is a LOCAL credential gap, not a verdict about any repo."""
    fake = _stub_jwt(monkeypatch)
    assert fake is not None

    def boom(payload, key, algorithm):
        raise ValueError("Could not parse the provided public key.")

    monkeypatch.setattr(ghauth.jwt, "encode", boom)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.make_app_jwt(agent_backend.CODEX)
    assert excinfo.value.kind == ghauth.UNCONFIGURED


def test_doppler_failure_is_unconfigured(monkeypatch):
    """No credentials to source HERE — nothing was asked of GitHub, so nothing is
    known about any App: `UNCONFIGURED`, never `NOT_INSTALLED` (#969)."""

    def fail(key: str) -> str:
        raise ghauth.secretsrc.SecretSourceError("doppler: command not found")

    monkeypatch.setattr(ghauth.secretsrc, "doppler_get", fail)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.make_app_jwt(agent_backend.CODEX)
    assert excinfo.value.kind == ghauth.UNCONFIGURED


def test_no_funnel_backend_is_a_clean_error(monkeypatch, doppler_stub):
    # A backend with no funnel App (claude) fails loud with an actionable message —
    # the registry-derived key names are never fabricated for it.
    _stub_jwt(monkeypatch)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.make_app_jwt(agent_backend.CLAUDE)
    assert "No GitHub App credentials" in str(excinfo.value)
    assert excinfo.value.kind == ghauth.UNCONFIGURED


def test_doppler_failure_is_normalized(monkeypatch):
    """A Doppler-sourcing failure becomes a clean ReviewAuthError, not a leak."""
    _stub_jwt(monkeypatch)

    def boom(key):
        raise ghauth.secretsrc.SecretSourceError("doppler not found on PATH")

    monkeypatch.setattr(ghauth.secretsrc, "doppler_get", boom)
    with pytest.raises(ghauth.ReviewAuthError, match="from\n? *Doppler|from Doppler"):
        ghauth.make_app_jwt(agent_backend.CODEX)


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

    tok = ghauth.installation_token(agent_backend.CODEX, "owner/repo")
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

    auth = ghauth.installation_auth(agent_backend.CODEX, "owner/repo")
    assert auth["token"] == "ghs_installation_tok"
    assert auth["permissions"]["checks"] == "write"
    # The string-only helper rides the same mint.
    assert (
        ghauth.installation_token(agent_backend.CODEX, "owner/repo")
        == "ghs_installation_tok"
    )


def test_installation_auth_raises_when_no_token(monkeypatch, doppler_stub):
    """A response without a `token` is a clean ReviewAuthError, not a silent {}."""
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    monkeypatch.setattr(ghauth, "_api_post", lambda path, token: {"permissions": {}})
    with pytest.raises(ghauth.ReviewAuthError, match="no\n? *'token'|no 'token'"):
        ghauth.installation_auth(agent_backend.CODEX, "owner/repo")


def test_installation_id_404_is_actionable(monkeypatch, doppler_stub):
    _stub_jwt(monkeypatch)

    def not_installed(path, token):
        raise ghauth.ReviewAuthError(
            "GitHub API GET /x failed (HTTP 404): nope",
            kind=ghauth.API_ERROR,
            status=404,
        )

    monkeypatch.setattr(ghauth, "_api_get", not_installed)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_id(
            agent_backend.CODEX, "owner/repo", jwt="signed.jwt.token"
        )
    assert "not installed" in str(excinfo.value)
    # The 404 is promoted to a VERDICT about the App — that is the one status this
    # layer is entitled to read as one.
    assert excinfo.value.kind == ghauth.NOT_INSTALLED


def test_installation_id_reads_the_status_not_the_message(monkeypatch, doppler_stub):
    """A 500 whose BODY happens to contain "HTTP 404" is not a not-installed verdict.

    The old check grepped `"HTTP 404" in str(exc)`, so any error text quoting a 404
    (a proxy page, an upstream error body) read as "the App is not installed" — the
    same substring-for-structure defect the engine keeps getting bitten by. The
    branch now reads `exc.status`.
    """
    _stub_jwt(monkeypatch)

    def server_error(path, token):
        raise ghauth.ReviewAuthError(
            "GitHub API GET /x failed (HTTP 500): upstream said HTTP 404",
            kind=ghauth.API_ERROR,
            status=500,
        )

    monkeypatch.setattr(ghauth, "_api_get", server_error)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_id(
            agent_backend.CODEX, "owner/repo", jwt="signed.jwt.token"
        )
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "not installed" not in str(excinfo.value)


def test_http_error_carries_kind_and_status(monkeypatch, doppler_stub):
    """The transport layer reports API_ERROR with the HTTP status attached — it is
    not entitled to any verdict about the App."""

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://api.github.com/x", 403, "Forbidden", {}, None)

        def read(self):
            return b'{"message":"Resource not accessible by integration"}'

    def boom(req, timeout):
        raise FakeHTTPError()

    monkeypatch.setattr(ghauth.urllib.request, "urlopen", boom)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/repos/owner/repo/installation", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR
    assert excinfo.value.status == 403
