"""Authenticate as a review-agent GitHub App installation: app JWT, installation
id, then a 1-hour ``ghs_…`` token the caller injects as ``GH_TOKEN``.

Credentials come from Doppler, and the PEM never lands on disk.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import jwt

from .. import secretsrc
from ..agent import backend as _agent_backend
from ..agent.backend import Backend

_HTTP_TIMEOUT = 30

_API_BASE = "https://api.github.com"

#: GitHub caps an app JWT at 10 minutes and rejects a future ``iat``.
_JWT_IAT_SKEW = 60
_JWT_TTL = 540


#: ``UNCONFIGURED`` says nothing about the target repo; ``NOT_INSTALLED`` is
#: GitHub's own 404 verdict; ``API_ERROR`` leaves the App's state unknown.
UNCONFIGURED = "unconfigured"
NOT_INSTALLED = "not-installed"
API_ERROR = "api-error"


class ReviewAuthError(Exception):
    """``kind`` is required, so no raise site inherits another's diagnosis."""

    def __init__(self, message: str, *, kind: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def _doppler_keys(backend: Backend) -> dict[str, str]:
    if not backend.has_funnel_identity or backend.doppler_app_prefix is None:
        known = ", ".join(
            sorted(b.funnel_agent or "" for b in _agent_backend.funnel_backends())
        )
        raise ReviewAuthError(
            f"No GitHub App credentials are configured for backend {backend.name!r}. "
            f"Known local-review agents: {known}.",
            kind=UNCONFIGURED,
        )
    return {"pem": backend.doppler_pem_key, "app_id": backend.doppler_app_id_key}


def _doppler_get(key: str, *, what: str, agent: str) -> str:
    try:
        value = secretsrc.doppler_get(key)
    except secretsrc.SecretSourceError as exc:
        raise ReviewAuthError(
            f"Could not source the {what} for the {agent!r} review app from "
            f"Doppler (key {key!r}): {exc}",
            kind=UNCONFIGURED,
        ) from exc
    if not value:
        raise ReviewAuthError(
            f"Doppler returned an empty {what} for the {agent!r} review app "
            f"(key {key!r}).",
            kind=UNCONFIGURED,
        )
    return value


def make_app_jwt(backend: Backend) -> str:
    agent = backend.funnel_agent or backend.name
    keys = _doppler_keys(backend)
    app_id = _doppler_get(keys["app_id"], what="app id", agent=agent)
    private_key = _doppler_get(keys["pem"], what="private key", agent=agent)

    now = int(time.time())
    payload = {
        "iat": now - _JWT_IAT_SKEW,
        "exp": now + _JWT_TTL,
        "iss": str(app_id),
    }
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 - surface any signing failure uniformly
        raise ReviewAuthError(
            f"Failed to sign the app JWT for {agent!r}: {exc}. The private key "
            f"Doppler returned is not a usable RSA PEM.",
            kind=UNCONFIGURED,
        ) from exc


def _excerpt(body: str, limit: int = 200) -> str:
    """Caps a body for an error message; it does NOT redact one."""
    flat = " ".join(body.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def _body_detail(payload: bytes, *, credential_body: bool) -> str:
    """A credential-bearing endpoint's body is described by size, never quoted."""
    if credential_body:
        return f"{len(payload)} bytes, not quoted (this body carries a credential)"
    return _excerpt(payload.decode("utf-8", "replace"))


def _shape(value: object) -> str:
    if isinstance(value, str):
        return "a string" if value else "an empty string"
    if value is None:
        return "nothing"
    return f"a {type(value).__name__}"


def _api_request(
    path: str, jwt_token: str, *, method: str, credential_body: bool
) -> object:
    """Every exit is a parsed body or a :class:`ReviewAuthError`, never a raw
    decode error; ``credential_body`` gates quoting the success body."""
    url = f"{_API_BASE}{path}"
    req = urllib.request.Request(url, method=method)  # noqa: S310 - fixed https host
    req.add_header("Authorization", f"Bearer {jwt_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if method == "POST":
        req.data = b"{}"
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed https host
            req, timeout=_HTTP_TIMEOUT
        ) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        body = _excerpt(exc.read().decode("utf-8", "replace"))
        raise ReviewAuthError(
            f"GitHub API {method} {path} failed (HTTP {exc.code}): {body}",
            kind=API_ERROR,
            status=exc.code,
        ) from exc
    except TimeoutError as exc:
        raise ReviewAuthError(
            f"GitHub API {method} {path} timed out after {_HTTP_TIMEOUT}s",
            kind=API_ERROR,
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ReviewAuthError(
                f"GitHub API {method} {path} timed out after {_HTTP_TIMEOUT}s",
                kind=API_ERROR,
            ) from exc
        raise ReviewAuthError(
            f"GitHub API {method} {path} failed: {exc}", kind=API_ERROR
        ) from exc
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Strict: `errors="replace"` would mint a corrupted `ghs_…` that still
        # parses.
        raise ReviewAuthError(
            f"GitHub API {method} {path} returned a body that is not valid UTF-8 "
            f"({exc}): {_body_detail(payload, credential_body=credential_body)}",
            kind=API_ERROR,
        ) from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewAuthError(
            f"GitHub API {method} {path} returned an unparseable body "
            f"({exc}): {_body_detail(payload, credential_body=credential_body)}",
            kind=API_ERROR,
        ) from exc


def _api_get(path: str, jwt_token: str) -> object:
    return _api_request(path, jwt_token, method="GET", credential_body=False)


def _api_post(path: str, jwt_token: str) -> object:
    return _api_request(path, jwt_token, method="POST", credential_body=True)


def installation_id(backend: Backend, repo: str, *, jwt: str | None = None) -> int:
    """A 404 here is the one status read as a verdict (:data:`NOT_INSTALLED`)."""
    agent = backend.funnel_agent or backend.name
    token = jwt if jwt is not None else make_app_jwt(backend)
    try:
        resp = _api_get(f"/repos/{repo}/installation", token)
    except ReviewAuthError as exc:
        if exc.status == 404:
            raise ReviewAuthError(
                f"The {agent!r} review app is not installed on {repo}'s owner. "
                f"Install the GitHub App on the repo's owner and retry.",
                kind=NOT_INSTALLED,
                status=404,
            ) from exc
        raise
    if not isinstance(resp, dict) or "id" not in resp:
        raise ReviewAuthError(
            f"Unexpected installation response for {agent!r} on {repo}: "
            f"{_excerpt(repr(resp))}",
            kind=API_ERROR,
        )
    inst_id = resp["id"]
    # Never coerce: `int()` would address a DIFFERENT installation.
    if isinstance(inst_id, bool) or not isinstance(inst_id, int) or inst_id <= 0:
        raise ReviewAuthError(
            f"Installation response for {agent!r} on {repo} has an unusable 'id' "
            f"(expected a positive integer): {_excerpt(repr(inst_id))}",
            kind=API_ERROR,
        )
    return inst_id


def installation_auth(backend: Backend, repo: str) -> dict:
    jwt_token = make_app_jwt(backend)
    inst_id = installation_id(backend, repo, jwt=jwt_token)
    resp = _api_post(f"/app/installations/{inst_id}/access_tokens", jwt_token)
    agent = backend.funnel_agent or backend.name
    # The failures below never repr the body: a failing answer can still carry
    # a usable `ghs_…`.
    if not isinstance(resp, dict):
        raise ReviewAuthError(
            f"Minting an installation token for {agent!r} on {repo} answered with "
            f"{_shape(resp)}, not a JSON object carrying a 'token'.",
            kind=API_ERROR,
        )
    token = resp.get("token")
    if not isinstance(token, str) or not token:
        raise ReviewAuthError(
            f"Minting an installation token for {agent!r} on {repo} returned no "
            f"usable 'token': expected a non-empty string, got {_shape(token)}.",
            kind=API_ERROR,
        )
    return resp


def installation_token(backend: Backend, repo: str) -> str:
    return installation_auth(backend, repo)["token"]
