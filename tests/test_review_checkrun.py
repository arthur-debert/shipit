"""Tests for `shipit.review.checkrun` — the local-review funnel breadcrumb.

OBS02-WS01: at local-review *kickoff*, shipit opens a GitHub Check Run named
`review: <reviewer>` (`status=in_progress`, `started_at=now`), authored by the
reviewer's App via the installation-token boundary. These tests assert the
breadcrumb shipit WRITES with the App-token boundary (`ghauth.installation_token`)
and the `gh` check-run POST seam FAKED — never live GitHub.

OBS02-WS02 adds the terminal `transition` (a single PATCH closing the SAME run to
its mapped conclusion + output + completed_at); its tests follow the create tests.
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


# --------------------------------------------------------------------------
# transition — OBS02-WS02 terminal conclusion
# --------------------------------------------------------------------------


def test_transition_patches_run_to_terminal_conclusion(monkeypatch):
    """The terminal transition PATCHes the SAME run id (no second create) to
    completed + the mapped conclusion, with an output message and an honest
    tz-aware `completed_at` timestamp."""
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["method"] = method
        seen["body"] = body
        return {}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.transition(
        "codex",
        "owner/repo",
        4242,
        conclusion="success",
        title="Local review posted",
        summary="done",
    )

    assert seen["path"] == "/repos/owner/repo/check-runs/4242"
    assert seen["method"] == "PATCH"
    body = seen["body"]
    assert body["status"] == "completed"
    assert body["conclusion"] == "success"
    assert body["output"] == {"title": "Local review posted", "summary": "done"}
    # `completed_at` mirrors `started_at`: an honest, parseable, tz-aware UTC "now".
    completed = _dt.datetime.fromisoformat(body["completed_at"])
    assert completed.tzinfo is not None


def test_transition_authored_via_installation_token(monkeypatch):
    """The transition is authored AS the reviewer's App — it mints the per-agent
    installation token and injects it on the PATCH, never the user's `gh` login."""
    auth: dict = {}
    _fake_token(monkeypatch, auth, value="ghs_appInstallToken")
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["token"] = token
        return {}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.transition(
        "agy", "owner/repo", 7, conclusion="failure", title="t", summary="s"
    )

    assert auth == {"agent": "agy", "repo": "owner/repo"}
    assert seen["token"] == "ghs_appInstallToken"


def test_transition_run_is_non_required(monkeypatch):
    """The transition rides the same non-required check-runs surface — never the
    branch-protection endpoint, never a `required` marker — so the run stays
    visible-but-non-blocking through its terminal state too."""
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["body"] = body
        return {}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.transition(
        "codex", "owner/repo", 9, conclusion="timed_out", title="t", summary="s"
    )

    assert "protection" not in seen["path"]
    assert "required" not in seen["body"]


def test_transition_never_logs_the_token(monkeypatch, caplog):
    """A record produced over the secret-bearing transition path must NOT contain
    the installation-token value — mirror `create`/`post.py` discipline."""
    secret = "ghs_transitionInstallToken1234567890"
    _fake_token(monkeypatch, {}, value=secret)
    monkeypatch.setattr(
        checkrun.gh, "rest", lambda path, *, method=None, body=None, token=None: {}
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        checkrun.transition(
            "codex", "owner/repo", 3, conclusion="success", title="t", summary="s"
        )
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full


def test_transition_propagates_failure(monkeypatch):
    """`transition` is honest like `create` — it RAISES on a mint/PATCH failure
    (e.g. the 403 before the `checks:write` re-grant). The best-effort swallowing
    lives in `run_and_post`, not here."""
    _fake_token(monkeypatch, {})

    def boom(path, *, method=None, body=None, token=None):
        raise checkrun.gh.GhError("403 Resource not accessible")

    monkeypatch.setattr(checkrun.gh, "rest", boom)
    with pytest.raises(checkrun.gh.GhError):
        checkrun.transition(
            "codex", "owner/repo", 1, conclusion="success", title="t", summary="s"
        )


# --------------------------------------------------------------------------
# find_nonterminal — OBS03-WS03 idempotency read
# --------------------------------------------------------------------------


def test_find_nonterminal_returns_id_for_in_progress_run(monkeypatch):
    """The reconcile read GETs the check runs on the head commit and returns the id
    of an IN-FLIGHT (`in_progress`) funnel run — the run a re-request reconciles
    against instead of opening a duplicate."""
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["method"] = method
        return {
            "total_count": 1,
            "check_runs": [{"id": 4242, "status": "in_progress", "conclusion": None}],
        }

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    run_id = checkrun.find_nonterminal("codex", "owner/repo", "deadbeef")

    assert run_id == 4242
    # A read (GET) of the head commit's check runs, filtered by the reviewer name.
    assert seen["method"] in (None, "GET")
    assert "/repos/owner/repo/commits/deadbeef/check-runs" in seen["path"]
    assert "check_name" in seen["path"]


@pytest.mark.parametrize("status", ["waiting", "requested", "pending", "queued"])
def test_find_nonterminal_returns_id_for_other_unfinished_statuses(monkeypatch, status):
    """`completed` is the SOLE terminal status — every other status the Checks API
    can surface (`waiting` / `requested` / `pending` / `queued`) is still IN FLIGHT.
    A run in any of them must reconcile as in-flight so reconcile catches it instead
    of opening + spawning a DUPLICATE review."""
    _fake_token(monkeypatch, {})
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {
            "total_count": 1,
            "check_runs": [{"id": 4242, "status": status, "conclusion": None}],
        },
    )
    assert checkrun.find_nonterminal("codex", "owner/repo", "deadbeef") == 4242


def test_find_nonterminal_returns_none_for_terminal_run(monkeypatch):
    """A run that has already CLOSED (status=completed) is terminal, not in flight —
    `find_nonterminal` returns None so the caller opens a fresh run."""
    _fake_token(monkeypatch, {})
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {
            "total_count": 1,
            "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}],
        },
    )
    assert checkrun.find_nonterminal("codex", "owner/repo", "deadbeef") is None


def test_find_nonterminal_returns_none_when_absent(monkeypatch):
    """No funnel run on the head commit → None (nothing to reconcile against)."""
    _fake_token(monkeypatch, {})
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {
            "total_count": 0,
            "check_runs": [],
        },
    )
    assert checkrun.find_nonterminal("codex", "owner/repo", "deadbeef") is None


def test_find_nonterminal_authored_via_installation_token(monkeypatch):
    """The reconcile read is authored AS the reviewer's App — it mints the per-agent
    installation token and threads it on the GET, mirroring create/transition."""
    auth: dict = {}
    _fake_token(monkeypatch, auth, value="ghs_appInstallToken")
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["token"] = token
        return {"check_runs": []}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.find_nonterminal("codex", "owner/repo", "deadbeef")

    assert auth == {"agent": "codex", "repo": "owner/repo"}
    assert seen["token"] == "ghs_appInstallToken"


def test_find_nonterminal_filters_by_reviewer_name(monkeypatch):
    """The query filters server-side by the per-reviewer run name (`<agent>-local`),
    url-encoded so the space + colon make a well-formed query string."""
    from urllib.parse import quote

    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        return {"check_runs": []}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.find_nonterminal("agy", "owner/repo", "cafef00d")

    assert quote("review: agy-local") in seen["path"]
