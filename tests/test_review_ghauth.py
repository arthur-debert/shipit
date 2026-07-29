from __future__ import annotations

import builtins
import pathlib
import urllib.error

import pytest

from shipit.agent import backend as agent_backend
from shipit.review import ghauth


@pytest.fixture
def doppler_stub(monkeypatch):
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
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in requested
    assert "CODEX_REVIEW_APP_ID" in requested
    assert captured["key"] == served["CODEX_REVIEW_APP_PRIVATE_KEY"]
    assert captured["algorithm"] == "RS256"
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
    import shipit.review.ghauth as mod

    assert mod.jwt is not None
    src = pathlib.Path(mod.__file__).read_text()
    assert "\nimport jwt\n" in src
    assert "-e review" not in src


def test_signing_failure_is_unconfigured(monkeypatch, doppler_stub):
    fake = _stub_jwt(monkeypatch)
    assert fake is not None

    def boom(payload, key, algorithm):
        raise ValueError("Could not parse the provided public key.")

    monkeypatch.setattr(ghauth.jwt, "encode", boom)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.make_app_jwt(agent_backend.CODEX)
    assert excinfo.value.kind == ghauth.UNCONFIGURED


def test_doppler_failure_is_unconfigured(monkeypatch):

    def fail(key: str) -> str:
        raise ghauth.secretsrc.SecretSourceError("doppler: command not found")

    monkeypatch.setattr(ghauth.secretsrc, "doppler_get", fail)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.make_app_jwt(agent_backend.CODEX)
    assert excinfo.value.kind == ghauth.UNCONFIGURED


def test_no_funnel_backend_is_a_clean_error(monkeypatch, doppler_stub):
    _stub_jwt(monkeypatch)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.make_app_jwt(agent_backend.CLAUDE)
    assert "No GitHub App credentials" in str(excinfo.value)
    assert excinfo.value.kind == ghauth.UNCONFIGURED


def test_doppler_failure_is_normalized(monkeypatch):
    _stub_jwt(monkeypatch)

    def boom(key):
        raise ghauth.secretsrc.SecretSourceError("doppler not found on PATH")

    monkeypatch.setattr(ghauth.secretsrc, "doppler_get", boom)
    with pytest.raises(ghauth.ReviewAuthError, match="from\n? *Doppler|from Doppler"):
        ghauth.make_app_jwt(agent_backend.CODEX)


def test_installation_token_runs_the_three_hops(monkeypatch, doppler_stub):
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
    assert (
        ghauth.installation_token(agent_backend.CODEX, "owner/repo")
        == "ghs_installation_tok"
    )


@pytest.mark.parametrize(
    "resp",
    [
        pytest.param({"permissions": {}}, id="absent"),
        pytest.param({"token": ""}, id="empty"),
        pytest.param({"token": None}, id="null"),
        pytest.param({"token": 12345}, id="number"),
        pytest.param({"token": {"value": "ghs_x"}}, id="object"),
        pytest.param(["ghs_x"], id="not-an-object"),
    ],
)
def test_installation_auth_raises_without_a_usable_token(
    monkeypatch, doppler_stub, resp
):
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    monkeypatch.setattr(ghauth, "_api_post", lambda path, token: resp)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_auth(agent_backend.CODEX, "owner/repo")
    message = str(excinfo.value)
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "'token'" in message
    assert "ghs_" not in message


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
    assert excinfo.value.kind == ghauth.NOT_INSTALLED


def test_installation_id_reads_the_status_not_the_message(monkeypatch, doppler_stub):
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


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _serve(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(
        ghauth.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body)
    )


def test_unparseable_success_body_is_an_api_error(monkeypatch):
    _serve(monkeypatch, b"<html><body>502 Bad Gateway</body></html>")
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/repos/owner/repo/installation", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR
    assert excinfo.value.status is None
    assert "unparseable" in str(excinfo.value)
    assert "502 Bad Gateway" in str(excinfo.value)


def test_unparseable_body_excerpt_is_one_capped_line(monkeypatch):
    _serve(monkeypatch, b"<html>\n" + b"x" * 5000 + b"\n</html>")
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/x", "signed.jwt.token")
    message = str(excinfo.value)
    assert "\n" not in message
    assert len(message) < 400


def test_non_utf8_success_body_is_an_api_error(monkeypatch):
    _serve(monkeypatch, b"\xff\xfe not utf-8 at all")
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/x", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR


_CORRUPT_TOKEN_BODY = b'{"token":"ghs_\xff","permissions":{"checks":"write"}}'


def test_invalid_utf8_inside_valid_json_is_an_api_error(monkeypatch):
    _serve(monkeypatch, _CORRUPT_TOKEN_BODY)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_post("/app/installations/42/access_tokens", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "not valid UTF-8" in str(excinfo.value)


_USABLE_TOKEN_WITH_A_BAD_BYTE_ELSEWHERE = (
    b'{"token":"ghs_usable_credential","extra":"\xff"}'
)

_TRUNCATED_TOKEN_BODY = b'{"token":"ghs_usable_credential","permi'


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            _USABLE_TOKEN_WITH_A_BAD_BYTE_ELSEWHERE, "not valid UTF-8", id="undecodable"
        ),
        pytest.param(_TRUNCATED_TOKEN_BODY, "unparseable", id="truncated"),
    ],
)
def test_a_credential_bearing_body_is_never_quoted(monkeypatch, body, expected):
    _serve(monkeypatch, body)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_post("/app/installations/42/access_tokens", "signed.jwt.token")
    message = str(excinfo.value)
    assert excinfo.value.kind == ghauth.API_ERROR
    assert expected in message
    assert "ghs_" not in message
    assert "usable_credential" not in message
    assert "/app/installations/42/access_tokens" in message
    assert f"{len(body)} bytes" in message


def test_a_corrupted_token_is_never_minted(monkeypatch, doppler_stub):
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    _serve(monkeypatch, _CORRUPT_TOKEN_BODY)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_auth(agent_backend.CODEX, "owner/repo")
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "ghs_" not in str(excinfo.value)


def test_empty_success_body_stays_none(monkeypatch):
    _serve(monkeypatch, b"   \n")
    assert ghauth._api_get("/x", "signed.jwt.token") is None


@pytest.mark.parametrize(
    "bad_id",
    [
        pytest.param("not-a-number", id="text"),
        pytest.param("42", id="digit-string"),
        pytest.param(True, id="bool"),
        pytest.param(42.9, id="float"),
        pytest.param(0, id="zero"),
        pytest.param(-7, id="negative"),
        pytest.param(None, id="null"),
        pytest.param({"id": 42}, id="object"),
    ],
)
def test_installation_id_rejects_any_non_integer_id(monkeypatch, doppler_stub, bad_id):
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": bad_id})
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_id(
            agent_backend.CODEX, "owner/repo", jwt="signed.jwt.token"
        )
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "not installed" not in str(excinfo.value)


def test_installation_id_accepts_a_plain_json_integer(monkeypatch, doppler_stub):
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    assert (
        ghauth.installation_id(
            agent_backend.CODEX, "owner/repo", jwt="signed.jwt.token"
        )
        == 42
    )
