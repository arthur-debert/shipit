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
backend) does the RS256 signing and is a BASE dependency, imported at module top
like any other (#969): App auth is the PR engine's local-reviewer path, not an
edge tool, so it must work from ANY shipit install — including the pinned build a
consumer repo runs, which carries no extras and has no shipit-local pixi env to
re-run inside.

**Three failure kinds, never conflated** (#969). Auth can fail because THIS
environment cannot produce credentials (:data:`UNCONFIGURED`), because the App is
genuinely absent from the target's owner (:data:`NOT_INSTALLED`), or because the
probe itself failed and nothing is known (:data:`API_ERROR`).
:class:`ReviewAuthError` carries which, so a caller like ``verify-apps`` reports
the operator's real situation instead of collapsing all three into "not live".
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


#: The three kinds of App-auth failure — the DIAGNOSIS, not the prose (#969).
#: ``UNCONFIGURED``: this environment cannot produce App credentials at all (they
#: could not be sourced from Doppler, the backend has no App, or the PEM would not
#: sign). Says NOTHING about the target repo. ``NOT_INSTALLED``: credentials work
#: and GitHub answered — the App is genuinely absent from the repo's owner (404).
#: ``API_ERROR``: the probe itself failed (HTTP error, timeout, unparseable
#: answer), so the App's state is UNKNOWN.
UNCONFIGURED = "unconfigured"
NOT_INSTALLED = "not-installed"
API_ERROR = "api-error"


class ReviewAuthError(Exception):
    """App-installation auth failed, carrying WHICH kind of failure it was.

    ``kind`` is one of :data:`UNCONFIGURED`, :data:`NOT_INSTALLED`, or
    :data:`API_ERROR`. It is a REQUIRED keyword: the three read alike in a message
    but mean different things to the operator ("configure Doppler here" vs
    "install the App on that owner" vs "GitHub is unreachable"), and a default
    would let a new raise site silently inherit someone else's diagnosis — exactly
    the collapse #969 reports.

    The message is actionable — the caller prints it and exits nonzero.
    """

    def __init__(self, message: str, *, kind: str, status: int | None = None) -> None:
        super().__init__(message)
        #: One of :data:`UNCONFIGURED` / :data:`NOT_INSTALLED` / :data:`API_ERROR`.
        self.kind = kind
        #: The HTTP status GitHub returned, when the failure came from a response;
        #: ``None`` otherwise. Carried so callers branch on the CODE rather than
        #: grepping ``"HTTP 404"`` out of the rendered message.
        self.status = status


def _doppler_keys(backend: Backend) -> dict[str, str]:
    """The Doppler key names for ``backend`` — READ off the ONE agent-backend
    identity registry (ADR-0025: the Doppler key names, like every backend alias,
    are defined once and shared by the launch + funnel axes), never a table
    duplicated here. Raises an actionable error for a backend with no funnel App
    (``claude``)."""
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
    """Source one Doppler secret via :mod:`shipit.secretsrc`, mapping any failure
    (doppler missing, key absent) to an :data:`UNCONFIGURED` :class:`ReviewAuthError`.

    Both shapes are a LOCAL setup gap — no `doppler` on PATH, no login, no such key
    — and say nothing about whether the App is installed anywhere, so both carry
    ``kind=UNCONFIGURED``.
    """
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
    """Sign an RS256 app JWT for ``backend``'s review App (``iss = app_id``),
    valid ~9 minutes.

    Sources the backend's app id + private key (PEM) from Doppler (under the key
    names the identity registry defines) via :mod:`shipit.secretsrc` and signs the
    JWT FROM THE IN-MEMORY PEM STRING with PyJWT — the PEM never touches disk.
    Every failure here is :data:`UNCONFIGURED`: nothing has been asked of GitHub
    yet, so none of it can speak to whether the App is installed.
    """
    agent = backend.funnel_agent or backend.name
    keys = _doppler_keys(backend)
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
            f"Failed to sign the app JWT for {agent!r}: {exc}. The private key "
            f"Doppler returned is not a usable RSA PEM.",
            kind=UNCONFIGURED,
        ) from exc


def _excerpt(body: str, limit: int = 200) -> str:
    """A single-line, length-capped snippet of a response body, for an error message.

    Response bodies are arbitrary remote text — an HTML error page or a proxy
    dump can run to kilobytes of newlines. The message this feeds is printed to a
    console AND logged, so it is collapsed to one line and truncated.
    """
    flat = " ".join(body.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def _api_request(path: str, jwt_token: str, *, method: str) -> object:
    """Bearer-JWT call to the GitHub REST API via stdlib urllib → parsed JSON.

    The shared core of :func:`_api_get` / :func:`_api_post` (the mock seams).
    Sends ``Authorization: Bearer <jwt>`` plus the versioned Accept headers, and
    raises an :data:`API_ERROR` :class:`ReviewAuthError` on any non-2xx (carrying
    the HTTP status and the response body), timeout, transport failure, OR a 2xx
    whose body will not decode as UTF-8 JSON. At this layer nothing is known about
    the App itself — a caller that CAN read a status as an App verdict
    (``installation_id`` reading 404) re-raises with its own kind.

    EVERY exit is either a parsed body or a :class:`ReviewAuthError`: a caller
    like ``verify-apps`` catches exactly that type to reach its UNVERIFIED
    outcome, so a raw ``JSONDecodeError``/``UnicodeDecodeError`` leaking from a
    garbled-but-successful answer would surface as a traceback instead of the
    documented "nothing could be determined" report (#969).
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
            # Decode defensively, same as the error-body read below: a garbled
            # byte in a 2xx body is an unparseable ANSWER (API_ERROR), not a
            # crash — the replacement chars land in the excerpt below.
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ReviewAuthError(
            f"GitHub API {method} {path} failed (HTTP {exc.code}): {body.strip()}",
            kind=API_ERROR,
            status=exc.code,
        ) from exc
    except TimeoutError as exc:
        # socket.timeout is an alias of TimeoutError since Py3.10, so this single
        # clause covers both the direct-timeout and the socket-timeout shapes.
        raise ReviewAuthError(
            f"GitHub API {method} {path} timed out after {_HTTP_TIMEOUT}s",
            kind=API_ERROR,
        ) from exc
    except urllib.error.URLError as exc:
        # A urllib timeout surfaces as URLError wrapping a socket.timeout — treat
        # it the same as the direct TimeoutError case above.
        if isinstance(exc.reason, TimeoutError):
            raise ReviewAuthError(
                f"GitHub API {method} {path} timed out after {_HTTP_TIMEOUT}s",
                kind=API_ERROR,
            ) from exc
        raise ReviewAuthError(
            f"GitHub API {method} {path} failed: {exc}", kind=API_ERROR
        ) from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # A 2xx that is not JSON is the SAME situation as a transport failure —
        # GitHub answered, but the answer is unusable, so the App's state stays
        # UNKNOWN. Normalising it here is what keeps the "every exit is a parsed
        # body or a ReviewAuthError" contract true for `verify-apps` (#969).
        raise ReviewAuthError(
            f"GitHub API {method} {path} returned an unparseable body "
            f"({exc}): {_excerpt(raw)}",
            kind=API_ERROR,
        ) from exc


def _api_get(path: str, jwt_token: str) -> object:
    """``GET <path>`` with a bearer JWT → parsed JSON. The mock seam for step 2."""
    return _api_request(path, jwt_token, method="GET")


def _api_post(path: str, jwt_token: str) -> object:
    """``POST <path>`` with a bearer JWT → parsed JSON. The mock seam for step 3."""
    return _api_request(path, jwt_token, method="POST")


def installation_id(backend: Backend, repo: str, *, jwt: str | None = None) -> int:
    """The app installation id for ``backend``'s review App on ``repo``'s owner.

    ``GET /repos/{owner}/{repo}/installation`` with a bearer JWT (minted here if
    ``jwt`` isn't supplied) → ``id``.

    This is the ONE layer that can turn an HTTP status into a verdict about the
    App: a 404 here means the App is genuinely not installed on the repo's owner,
    so it re-raises as :data:`NOT_INSTALLED` (read off the response's STATUS, not
    by grepping the message text). Every other failure keeps the kind it arrived
    with — :data:`UNCONFIGURED` from minting the JWT, :data:`API_ERROR` from the
    call itself. A 2xx whose body is missing ``id`` or carries a non-numeric one
    is :data:`API_ERROR` too: an answer arrived, but it establishes nothing.
    """
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
            f"Unexpected installation response for {agent!r} on {repo}: {resp!r}",
            kind=API_ERROR,
        )
    try:
        return int(resp["id"])
    except (TypeError, ValueError) as exc:
        # Same class as an unparseable body: GitHub answered with the key but not
        # with a number. It is still an unusable ANSWER, so it must arrive at the
        # caller as API_ERROR rather than a raw ValueError (#969).
        raise ReviewAuthError(
            f"Installation response for {agent!r} on {repo} has a non-numeric "
            f"'id': {resp['id']!r}",
            kind=API_ERROR,
        ) from exc


def installation_auth(backend: Backend, repo: str) -> dict:
    """Mint a 1-hour installation token for ``backend``'s App on ``repo`` — full
    response.

    The same three hops as :func:`installation_token` (JWT → installation id →
    ``POST /app/installations/{id}/access_tokens``), but returns the WHOLE
    access-tokens response rather than just the token string. The load-bearing
    extra field is ``permissions`` — the scope map GitHub actually granted this
    token (e.g. ``{"checks": "write", "pull_requests": "write", …}``), which the
    OBS02 funnel verification harness asserts carries ``checks: write`` (the
    re-grant landed) before it drives a check-run create. Nothing is cached to
    disk. Raises :class:`ReviewAuthError` on any failure.
    """
    jwt_token = make_app_jwt(backend)
    inst_id = installation_id(backend, repo, jwt=jwt_token)
    resp = _api_post(f"/app/installations/{inst_id}/access_tokens", jwt_token)
    if not isinstance(resp, dict) or not resp.get("token"):
        agent = backend.funnel_agent or backend.name
        raise ReviewAuthError(
            f"Minting an installation token for {agent!r} on {repo} returned no "
            f"'token': {resp!r}",
            kind=API_ERROR,
        )
    return resp


def installation_token(backend: Backend, repo: str) -> str:
    """Mint a 1-hour installation access token for ``backend``'s App on ``repo``.

    Orchestrates the three hops: JWT → installation id → ``POST
    /app/installations/{id}/access_tokens`` → the ``ghs_…`` token (the ``token``
    field of :func:`installation_auth`'s response). Nothing is cached to disk; the
    token is returned for the caller to inject as ``gh.rest(..., token=...)``.
    Raises :class:`ReviewAuthError` on any failure.
    """
    return str(installation_auth(backend, repo)["token"])
