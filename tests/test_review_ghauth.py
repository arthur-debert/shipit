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
    response STATUS, never grepped out of the message;
  * the 3-hop installation-token flow uses the bearer-JWT urllib seams; and
  * EVERY exit of the transport is a parsed body or a `ReviewAuthError` — an
    answer that arrives but is unusable (non-JSON body, non-UTF-8 bytes, an `id`
    that is not a positive integer, a `token` that is not a non-empty string) is
    `API_ERROR`, never a raw decode/`int()` exception that would bypass the
    caller's UNVERIFIED handling, and never coerced into a usable-looking value;
    and
  * no failure of the access-tokens endpoint QUOTES its body. That body is a live
    `ghs_…`, and the message reaches a printed report and an `exc_info=True` log
    record, so an unusable answer there is reported by size and shape. The
    installation GET's body — metadata, no credential — is still quoted, capped
    to one line.

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
    """A response with no non-empty STRING `token` is a clean ReviewAuthError.

    The same shape-not-coercion rule as the installation `id`: a truthy check plus
    a downstream `str()` would render `12345` or a nested object into a
    plausible-looking credential and hand it to `gh` as GH_TOKEN. Guaranteeing the
    type here is what lets `installation_token` return the value with no coercion.

    The failure states the SHAPE it got and never reprs the response: this is the
    credential endpoint's body, and the very answers that fail this guard can
    still carry a usable `ghs_…` under a non-string `token` (`{"token":
    ["ghs_x"]}`) — a repr would print and log it.
    """
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


class _FakeResponse:
    """A minimal `urlopen` context manager serving a canned body."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _serve(monkeypatch, body: bytes) -> None:
    """Make the bearer-JWT transport return a 2xx carrying `body`."""
    monkeypatch.setattr(
        ghauth.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(body)
    )


def test_unparseable_success_body_is_an_api_error(monkeypatch):
    """A 2xx that is not JSON is API_ERROR — never a raw `JSONDecodeError`.

    `verify-apps` catches `ReviewAuthError` and nothing else to reach its
    UNVERIFIED outcome, so a decode error escaping this layer surfaced as a
    traceback instead of the documented "nothing was determined" report. The most
    likely real shape is an intercepting proxy or an incident page answering 200
    with HTML — and on THIS seam (the installation GET, whose body is metadata)
    quoting that page is the whole diagnosis, so the body is shown.
    """
    _serve(monkeypatch, b"<html><body>502 Bad Gateway</body></html>")
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/repos/owner/repo/installation", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR
    # No verdict is available from an unusable answer, so no status is claimed.
    assert excinfo.value.status is None
    assert "unparseable" in str(excinfo.value)
    # The body is quoted so an operator can see WHAT answered.
    assert "502 Bad Gateway" in str(excinfo.value)


def test_unparseable_body_excerpt_is_one_capped_line(monkeypatch):
    """The quoted body is flattened and truncated — an error message, not a dump."""
    _serve(monkeypatch, b"<html>\n" + b"x" * 5000 + b"\n</html>")
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/x", "signed.jwt.token")
    message = str(excinfo.value)
    assert "\n" not in message
    assert len(message) < 400


def test_non_utf8_success_body_is_an_api_error(monkeypatch):
    """Undecodable BYTES are the same situation as undecodable JSON.

    The error-body read decodes with `errors="replace"` (its text is only ever a
    message); the success read must NOT, or a garbled 2xx becomes a parsed answer.
    """
    _serve(monkeypatch, b"\xff\xfe not utf-8 at all")
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_get("/x", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR


#: A real access-tokens answer whose TOKEN STRING carries one invalid byte. The
#: bytes around it are valid JSON, so replacement decoding leaves a perfectly
#: parseable body — this is the shape that a lossy read waves through.
_CORRUPT_TOKEN_BODY = b'{"token":"ghs_\xff","permissions":{"checks":"write"}}'


def test_invalid_utf8_inside_valid_json_is_an_api_error(monkeypatch):
    """The garbled byte is INSIDE a JSON string, so the body still parses.

    Decoding with `errors="replace"` does not fail on this — it substitutes U+FFFD
    and returns valid JSON, which `json.loads` accepts. The transport must decode
    STRICTLY so this reaches the caller as API_ERROR; a body that merely puts its
    invalid bytes outside the JSON (the `\\xff\\xfe …` case above) cannot catch
    the difference, because it fails at the JSON layer either way.
    """
    _serve(monkeypatch, _CORRUPT_TOKEN_BODY)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_post("/app/installations/42/access_tokens", "signed.jwt.token")
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "not valid UTF-8" in str(excinfo.value)


#: An access-tokens answer whose token is INTACT and USABLE — the invalid byte is
#: somewhere else entirely. The strict decode still rejects the body, and the
#: rejection message must not carry the live credential that came with it.
_USABLE_TOKEN_WITH_A_BAD_BYTE_ELSEWHERE = (
    b'{"token":"ghs_usable_credential","extra":"\xff"}'
)

#: The same endpoint answering with TRUNCATED JSON — unparseable, token intact.
#: The other half of the class: whatever makes the body unusable, the credential
#: it carries must not reach the message.
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
    """An unusable access-tokens body is reported by SIZE, never quoted.

    `POST …/access_tokens` answers with a live `ghs_…`, and what makes the body
    unusable can sit anywhere — one invalid byte in a neighbouring field, or a
    truncation after the token. Excerpting it put a USABLE credential into the
    `ReviewAuthError` that `verify_app` logs with `exc_info=True` and that
    `_auth_failure` interpolates into the printed reason: a live token in the
    console and in the JSONL log, from a path that is only reporting a fault.
    The message keeps what diagnoses the fault — which call, what went wrong,
    how many bytes answered — and drops the content.
    """
    _serve(monkeypatch, body)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth._api_post("/app/installations/42/access_tokens", "signed.jwt.token")
    message = str(excinfo.value)
    assert excinfo.value.kind == ghauth.API_ERROR
    assert expected in message
    assert "ghs_" not in message
    assert "usable_credential" not in message
    # Still diagnosable: the failing call and the body's size are named.
    assert "/app/installations/42/access_tokens" in message
    assert f"{len(body)} bytes" in message


def test_a_corrupted_token_is_never_minted(monkeypatch, doppler_stub):
    """The end the strict decode protects: no corrupted `ghs_…` reaches a caller.

    With a lossy read, this access-tokens response parsed into a token whose bytes
    GitHub never sent (`ghs_�`) and a `checks: write` permission map — so
    `installation_auth` returned it, `gh` would have authenticated with it, and
    `verify-apps` would have printed LIVE off a corrupted credential.
    """
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    _serve(monkeypatch, _CORRUPT_TOKEN_BODY)
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_auth(agent_backend.CODEX, "owner/repo")
    assert excinfo.value.kind == ghauth.API_ERROR
    # …and the refusal does not print the token it refused, corrupt or not.
    assert "ghs_" not in str(excinfo.value)


def test_empty_success_body_stays_none(monkeypatch):
    """An empty 2xx body is still `None`, not an error — the normalization above
    must not swallow the legitimately-bodiless response."""
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
    """An `id` that is not a positive integer establishes nothing — API_ERROR.

    Same class as the unparseable body: GitHub answered, but the answer is
    unusable, so it must arrive at the seam `verify-apps` catches rather than as a
    raw `ValueError`. `int()` was the wrong gate in BOTH directions — it crashed on
    the text case and silently ACCEPTED `True` (→ 1) and `42.9` (→ 42), either of
    which would have addressed the access-token request to a different
    installation. The shape is validated now, never coerced.
    """
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": bad_id})
    with pytest.raises(ghauth.ReviewAuthError) as excinfo:
        ghauth.installation_id(
            agent_backend.CODEX, "owner/repo", jwt="signed.jwt.token"
        )
    assert excinfo.value.kind == ghauth.API_ERROR
    assert "not installed" not in str(excinfo.value)


def test_installation_id_accepts_a_plain_json_integer(monkeypatch, doppler_stub):
    """The shape GitHub actually sends still passes, unchanged and untouched."""
    _stub_jwt(monkeypatch)
    monkeypatch.setattr(ghauth, "_api_get", lambda path, token: {"id": 42})
    assert (
        ghauth.installation_id(
            agent_backend.CODEX, "owner/repo", jwt="signed.jwt.token"
        )
        == 42
    )
