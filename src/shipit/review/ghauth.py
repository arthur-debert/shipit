"""ghauth — authenticate AS a review-agent GitHub App installation, so a review
posts as ``adr-<agent>-review[bot]`` rather than as the user's own ``gh`` login.

The flow (GitHub App *installation* auth) has three credential hops, then the
post:

1. **App JWT** — an RS256 JWT signed with the app's private key, claims
   ``iat = now-60``, ``exp = now+540`` (≤10 min), ``iss = app_id``. Used as
   ``Authorization: Bearer <jwt>`` for the next two calls.
2. **Installation id** — ``GET /repos/{owner}/{repo}/installation`` with the JWT
   → the app's installation ``id`` on that repo's owner.
3. **Installation token** — ``POST /app/installations/{id}/access_tokens`` with
   the JWT → a 1-hour ``ghs_…`` token.

That ``ghs_…`` token is then a NORMAL token: the caller hands it to
``gh.rest(..., token=...)`` (→ ``GH_TOKEN``) for the actual review POST, which
GitHub attributes to the bot.

**The shipit divergence (vs release-core): Doppler, never disk.** Both the App
private key (PEM) and the app id are sourced from Doppler via shipit's existing
:mod:`shipit.secretsrc` (``doppler secrets get … --project github --config prd``),
keyed per agent (``CODEX_REVIEW_APP_PRIVATE_KEY`` / ``CODEX_REVIEW_APP_ID`` and
the ``AGY_…`` pair). PyJWT signs the App JWT from the in-memory PEM STRING — the
PEM never lands on disk. Release-core's ``~/.config/release-review/apps/*.pem``
+ ``ghapp`` disk lookups are dropped entirely.

**Transport split.** Steps 2–3 need ``Authorization: Bearer <jwt>``, but `gh api`
injects the user's token and is awkward to coerce into bearer-JWT auth — so those
two calls go through stdlib :mod:`urllib.request` here (the ``_api_get`` /
``_api_post`` mock seams). Only the final review POST reuses the `gh` boundary
(with the installation token injected as ``GH_TOKEN``). PyJWT (with its crypto
backend) does the RS256 signing and is imported LAZILY — it is an OPTIONAL
dependency (the ``review`` extra), not a hard runtime requirement.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .. import secretsrc

#: Per-request timeout (seconds) for the bearer-JWT urllib calls.
_HTTP_TIMEOUT = 30

#: GitHub REST API base for the bearer-JWT calls (steps 2–3). The final review
#: POST goes through `gh`, which targets api.github.com itself.
_API_BASE = "https://api.github.com"

#: JWT lifetime: GitHub caps an app JWT at 10 minutes and rejects a future
#: ``iat`` if the runner clock is skewed forward, so back-date ``iat`` 60s and
#: set ``exp`` to +9 minutes (well under the cap).
_JWT_IAT_SKEW = 60
_JWT_TTL = 540

#: Per-agent Doppler key names for the App PEM + app id. The agent's review bot
#: is ``adr-<agent>-review[bot]``; the App credentials are provisioned into
#: Doppler ``github/prd`` under these keys (pre-work, already done).
_DOPPLER_KEYS: dict[str, dict[str, str]] = {
    "codex": {
        "pem": "CODEX_REVIEW_APP_PRIVATE_KEY",
        "app_id": "CODEX_REVIEW_APP_ID",
    },
    "agy": {
        "pem": "AGY_REVIEW_APP_PRIVATE_KEY",
        "app_id": "AGY_REVIEW_APP_ID",
    },
}


class ReviewAuthError(Exception):
    """App-installation auth failed (missing PyJWT, app not installed, API error,
    or a Doppler-sourcing failure).

    Carries an actionable message — the caller prints it and exits nonzero.
    """


def _doppler_keys(agent: str) -> dict[str, str]:
    """The Doppler key names for ``agent``, or raise an actionable error."""
    keys = _DOPPLER_KEYS.get(agent)
    if keys is None:
        known = ", ".join(sorted(_DOPPLER_KEYS))
        raise ReviewAuthError(
            f"No GitHub App credentials are configured for agent {agent!r}. "
            f"Known local-review agents: {known}."
        )
    return keys


def _doppler_get(key: str, *, what: str, agent: str) -> str:
    """Source one Doppler secret via :mod:`shipit.secretsrc`, mapping any failure
    (doppler missing, key absent) to a clean :class:`ReviewAuthError`."""
    try:
        value = secretsrc.doppler_get(key)
    except secretsrc.SecretSourceError as exc:
        raise ReviewAuthError(
            f"Could not source the {what} for the {agent!r} review app from "
            f"Doppler (key {key!r}): {exc}"
        ) from exc
    if not value:
        raise ReviewAuthError(
            f"Doppler returned an empty {what} for the {agent!r} review app "
            f"(key {key!r})."
        )
    return value


def make_app_jwt(agent: str) -> str:
    """Sign an RS256 app JWT for ``agent`` (``iss = app_id``), valid ~9 minutes.

    Sources the agent's app id + private key (PEM) from Doppler via
    :mod:`shipit.secretsrc` and signs the JWT FROM THE IN-MEMORY PEM STRING with
    PyJWT — the PEM never touches disk. PyJWT (and its crypto backend) is imported
    lazily; if it is missing, raises :class:`ReviewAuthError` with the install hint.
    """
    try:
        import jwt  # noqa: PLC0415 — lazy: optional `review` extra
    except ImportError as exc:  # pragma: no cover - exercised only when extra absent
        raise ReviewAuthError(
            "Posting a review as a GitHub App needs PyJWT (with its crypto "
            'backend). Install it with: pip install "pyjwt[crypto]" '
            "(or install shipit with the `review` extra: pip install 'shipit[review]')."
        ) from exc

    keys = _doppler_keys(agent)
    app_id = _doppler_get(keys["app_id"], what="app id", agent=agent)
    private_key = _doppler_get(keys["pem"], what="private key", agent=agent)

    now = int(time.time())
    payload = {
        "iat": now - _JWT_IAT_SKEW,
        "exp": now + _JWT_TTL,
        # GitHub accepts the app id as a string, and PyJWT ≥2.10 REQUIRES `iss`
        # to be a string — so stringify it (Doppler stores it as digits).
        "iss": str(app_id),
    }
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 - surface any signing failure uniformly
        raise ReviewAuthError(
            f"Failed to sign the app JWT for {agent!r}: {exc}. If this mentions a "
            'missing crypto backend, install: pip install "pyjwt[crypto]"'
        ) from exc


def _api_request(path: str, jwt_token: str, *, method: str) -> object:
    """Bearer-JWT call to the GitHub REST API via stdlib urllib → parsed JSON.

    The shared core of :func:`_api_get` / :func:`_api_post` (the mock seams).
    Sends ``Authorization: Bearer <jwt>`` plus the versioned Accept headers, and
    raises :class:`ReviewAuthError` on any non-2xx, including the response body.
    """
    url = f"{_API_BASE}{path}"
    req = urllib.request.Request(url, method=method)  # noqa: S310 - fixed https host
    req.add_header("Authorization", f"Bearer {jwt_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if method == "POST":
        # POST /access_tokens takes an (optional) JSON body; send an empty object
        # so the request carries a content-type and a zero-length body cleanly.
        req.data = b"{}"
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed https host
            req, timeout=_HTTP_TIMEOUT
        ) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ReviewAuthError(
            f"GitHub API {method} {path} failed (HTTP {exc.code}): {body.strip()}"
        ) from exc
    except TimeoutError as exc:
        # socket.timeout is an alias of TimeoutError since Py3.10, so this single
        # clause covers both the direct-timeout and the socket-timeout shapes.
        raise ReviewAuthError(
            f"GitHub API {method} {path} timed out after {_HTTP_TIMEOUT}s"
        ) from exc
    except urllib.error.URLError as exc:
        # A urllib timeout surfaces as URLError wrapping a socket.timeout — treat
        # it the same as the direct TimeoutError case above.
        if isinstance(exc.reason, TimeoutError):
            raise ReviewAuthError(
                f"GitHub API {method} {path} timed out after {_HTTP_TIMEOUT}s"
            ) from exc
        raise ReviewAuthError(f"GitHub API {method} {path} failed: {exc}") from exc
    return json.loads(raw) if raw.strip() else None


def _api_get(path: str, jwt_token: str) -> object:
    """``GET <path>`` with a bearer JWT → parsed JSON. The mock seam for step 2."""
    return _api_request(path, jwt_token, method="GET")


def _api_post(path: str, jwt_token: str) -> object:
    """``POST <path>`` with a bearer JWT → parsed JSON. The mock seam for step 3."""
    return _api_request(path, jwt_token, method="POST")


def installation_id(agent: str, repo: str, *, jwt: str | None = None) -> int:
    """The app installation id for ``agent`` on ``repo``'s owner.

    ``GET /repos/{owner}/{repo}/installation`` with a bearer JWT (minted here if
    ``jwt`` isn't supplied) → ``id``. Raises :class:`ReviewAuthError` with an
    actionable message if the app isn't installed on the repo's owner (404).
    """
    token = jwt if jwt is not None else make_app_jwt(agent)
    try:
        resp = _api_get(f"/repos/{repo}/installation", token)
    except ReviewAuthError as exc:
        if "HTTP 404" in str(exc):
            raise ReviewAuthError(
                f"The {agent!r} review app is not installed on {repo}'s owner. "
                f"Install the GitHub App on the repo's owner and retry."
            ) from exc
        raise
    if not isinstance(resp, dict) or "id" not in resp:
        raise ReviewAuthError(
            f"Unexpected installation response for {agent!r} on {repo}: {resp!r}"
        )
    return int(resp["id"])


def installation_token(agent: str, repo: str) -> str:
    """Mint a 1-hour installation access token for ``agent`` on ``repo``.

    Orchestrates the three hops: JWT → installation id → ``POST
    /app/installations/{id}/access_tokens`` → the ``ghs_…`` token. Nothing is
    cached to disk; the token is returned for the caller to inject as
    ``gh.rest(..., token=...)``. Raises :class:`ReviewAuthError` on any failure.
    """
    jwt_token = make_app_jwt(agent)
    inst_id = installation_id(agent, repo, jwt=jwt_token)
    resp = _api_post(f"/app/installations/{inst_id}/access_tokens", jwt_token)
    if not isinstance(resp, dict) or not resp.get("token"):
        raise ReviewAuthError(
            f"Minting an installation token for {agent!r} on {repo} returned no "
            f"'token': {resp!r}"
        )
    return str(resp["token"])
