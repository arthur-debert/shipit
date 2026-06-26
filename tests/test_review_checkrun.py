"""Tests for `shipit.review.checkrun` — the local-review funnel breadcrumb.

OBS02-WS01: at local-review *kickoff*, shipit opens a GitHub Check Run named
`review: <reviewer>` (`status=in_progress`, `started_at=now`), authored by the
reviewer's App via the installation-token boundary. These tests assert the
breadcrumb shipit WRITES with the App-token boundary (`ghauth.installation_token`)
and the `gh` check-run POST seam FAKED — never live GitHub.

The terminal `transition` (success / failure / timed_out) is WS02, not asserted
here.
"""

from __future__ import annotations

import datetime as _dt
import logging

import pytest

from shipit.review import checkrun


def _fake_token(monkeypatch, sink: dict, value: str = "ghs_tok") -> None:
    """Make `ghauth.installation_token` return a fixed token, recording its args —
    the PEM→JWT→token mint is faked in-memory; nothing touches disk or the wire."""

    def _mint(agent, repo):
        sink["agent"] = agent
        sink["repo"] = repo
        return value

    monkeypatch.setattr(checkrun.ghauth, "installation_token", _mint)


def test_create_opens_in_progress_run_with_name_status_started_at(monkeypatch):
    """The kickoff create POSTs a check run with the right name, in_progress
    status, the PR head sha, and an honest `started_at` timestamp."""
    auth: dict = {}
    _fake_token(monkeypatch, auth)
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["method"] = method
        seen["body"] = body
        seen["token"] = token
        return {"id": 4242}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    run_id = checkrun.create("codex", "owner/repo", "deadbeef")

    assert run_id == 4242
    assert seen["path"] == "/repos/owner/repo/check-runs"
    assert seen["method"] == "POST"
    body = seen["body"]
    assert body["name"] == "review: codex-local"
    assert body["status"] == "in_progress"
    assert body["head_sha"] == "deadbeef"
    # `started_at` is the load-bearing output: an honest, parseable UTC "now".
    started = _dt.datetime.fromisoformat(body["started_at"])
    assert started.tzinfo is not None  # tz-aware (UTC), not naive


def test_create_names_run_per_reviewer(monkeypatch):
    """The run name carries the *reviewer* (`<agent>-local`), so the funnel reads
    one run per reviewer kind."""
    _fake_token(monkeypatch, {})
    seen: dict = {}
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: (
            seen.update(body=body) or {"id": 1}
        ),
    )
    checkrun.create("agy", "owner/repo", "cafef00d")
    assert seen["body"]["name"] == "review: agy-local"


def test_create_authored_via_installation_token(monkeypatch):
    """The run is authored AS the reviewer's App — the create call mints the
    per-agent installation token and injects it on the `gh` POST (the bot-token
    seam), never the user's own `gh` login. The PEM/JWT mint is in-memory only."""
    auth: dict = {}
    _fake_token(monkeypatch, auth, value="ghs_appInstallToken")
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["token"] = token
        return {"id": 7}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.create("codex", "owner/repo", "deadbeef")

    # The token was minted for THIS agent+repo and threaded onto the POST.
    assert auth == {"agent": "codex", "repo": "owner/repo"}
    assert seen["token"] == "ghs_appInstallToken"


def test_create_run_is_non_required(monkeypatch):
    """The funnel run is non-required: it is created via the check-runs endpoint
    and never registered as a required check (no branch-protection write, no
    `required` flag in the body), so it is visible but never blocks merge."""
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["body"] = body
        return {"id": 9}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.create("codex", "owner/repo", "deadbeef")

    # Created as an ordinary check run — not via the branch-protection /
    # required-status-checks surface, and carrying no "required" marker.
    assert seen["path"] == "/repos/owner/repo/check-runs"
    assert "protection" not in seen["path"]
    assert "required" not in seen["body"]


def test_create_never_logs_the_token(monkeypatch, caplog):
    """A record produced over the secret-bearing create path must NOT contain the
    installation-token value — mirror `post.py`'s discipline."""
    secret = "ghs_funnelInstallToken1234567890"
    _fake_token(monkeypatch, {}, value=secret)
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {"id": 3},
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        checkrun.create("codex", "owner/repo", "deadbeef")
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full


def test_create_propagates_auth_failure(monkeypatch):
    """`create` itself is honest — it RAISES on a mint/POST failure (e.g. the
    403 before the `checks:write` re-grant). The best-effort swallowing lives in
    `run_and_post`, not here (so WS02's `transition` shares the same honest base)."""

    def boom(agent, repo):
        raise checkrun.ghauth.ReviewAuthError("403 Resource not accessible")

    monkeypatch.setattr(checkrun.ghauth, "installation_token", boom)
    with pytest.raises(checkrun.ghauth.ReviewAuthError):
        checkrun.create("codex", "owner/repo", "deadbeef")
