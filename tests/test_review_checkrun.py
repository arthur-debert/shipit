from __future__ import annotations

import datetime as _dt
import logging

import pytest

from shipit.agent import backend as agent_backend
from shipit.execrun import ExecError
from shipit.review import checkrun


def _fake_token(monkeypatch, sink: dict, value: str = "ghs_tok") -> None:

    def _mint(backend, repo):
        sink["backend"] = backend
        sink["repo"] = repo
        return value

    monkeypatch.setattr(checkrun.ghauth, "installation_token", _mint)


def test_create_opens_in_progress_run_with_name_status_started_at(monkeypatch):
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

    run_id = checkrun.create(agent_backend.CODEX, "owner/repo", "deadbeef")

    assert run_id == 4242
    assert seen["path"] == "/repos/owner/repo/check-runs"
    assert seen["method"] == "POST"
    body = seen["body"]
    assert body["name"] == "review: codex-local"
    assert body["status"] == "in_progress"
    assert body["head_sha"] == "deadbeef"
    started = _dt.datetime.fromisoformat(body["started_at"])
    assert started.tzinfo is not None


def test_create_names_run_per_reviewer(monkeypatch):
    _fake_token(monkeypatch, {})
    seen: dict = {}
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: (
            seen.update(body=body) or {"id": 1}
        ),
    )
    checkrun.create(agent_backend.ANTIGRAVITY, "owner/repo", "cafef00d")
    assert seen["body"]["name"] == "review: agy-local"


def test_create_authored_via_installation_token(monkeypatch):
    auth: dict = {}
    _fake_token(monkeypatch, auth, value="ghs_appInstallToken")
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["token"] = token
        return {"id": 7}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.create(agent_backend.CODEX, "owner/repo", "deadbeef")

    assert auth == {"backend": agent_backend.CODEX, "repo": "owner/repo"}
    assert seen["token"] == "ghs_appInstallToken"


def test_create_run_is_non_required(monkeypatch):
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["body"] = body
        return {"id": 9}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.create(agent_backend.CODEX, "owner/repo", "deadbeef")

    assert seen["path"] == "/repos/owner/repo/check-runs"
    assert "protection" not in seen["path"]
    assert "required" not in seen["body"]


def test_create_never_logs_the_token(monkeypatch, caplog):
    secret = "ghs_funnelInstallToken1234567890"
    _fake_token(monkeypatch, {}, value=secret)
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {"id": 3},
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        checkrun.create(agent_backend.CODEX, "owner/repo", "deadbeef")
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full


def test_create_propagates_auth_failure(monkeypatch):

    def boom(backend, repo):
        raise checkrun.ghauth.ReviewAuthError(
            "403 Resource not accessible", kind=checkrun.ghauth.API_ERROR
        )

    monkeypatch.setattr(checkrun.ghauth, "installation_token", boom)
    with pytest.raises(checkrun.ghauth.ReviewAuthError):
        checkrun.create(agent_backend.CODEX, "owner/repo", "deadbeef")


def test_transition_patches_run_to_terminal_conclusion(monkeypatch):
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["method"] = method
        seen["body"] = body
        return {}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.transition(
        agent_backend.CODEX,
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
    completed = _dt.datetime.fromisoformat(body["completed_at"])
    assert completed.tzinfo is not None


def test_transition_authored_via_installation_token(monkeypatch):
    auth: dict = {}
    _fake_token(monkeypatch, auth, value="ghs_appInstallToken")
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["token"] = token
        return {}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.transition(
        agent_backend.ANTIGRAVITY,
        "owner/repo",
        7,
        conclusion="failure",
        title="t",
        summary="s",
    )

    assert auth == {"backend": agent_backend.ANTIGRAVITY, "repo": "owner/repo"}
    assert seen["token"] == "ghs_appInstallToken"


def test_transition_run_is_non_required(monkeypatch):
    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["body"] = body
        return {}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.transition(
        agent_backend.CODEX,
        "owner/repo",
        9,
        conclusion="timed_out",
        title="t",
        summary="s",
    )

    assert "protection" not in seen["path"]
    assert "required" not in seen["body"]


def test_transition_never_logs_the_token(monkeypatch, caplog):
    secret = "ghs_transitionInstallToken1234567890"
    _fake_token(monkeypatch, {}, value=secret)
    monkeypatch.setattr(
        checkrun.gh, "rest", lambda path, *, method=None, body=None, token=None: {}
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        checkrun.transition(
            agent_backend.CODEX,
            "owner/repo",
            3,
            conclusion="success",
            title="t",
            summary="s",
        )
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full


def test_transition_propagates_failure(monkeypatch):
    _fake_token(monkeypatch, {})

    def boom(path, *, method=None, body=None, token=None):
        raise ExecError(["gh"], rc=1, stderr="403 Resource not accessible")

    monkeypatch.setattr(checkrun.gh, "rest", boom)
    with pytest.raises(ExecError):
        checkrun.transition(
            agent_backend.CODEX,
            "owner/repo",
            1,
            conclusion="success",
            title="t",
            summary="s",
        )


def test_find_nonterminal_returns_id_for_in_progress_run(monkeypatch):
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

    run_id = checkrun.find_nonterminal(agent_backend.CODEX, "owner/repo", "deadbeef")

    assert run_id == 4242
    assert seen["method"] in (None, "GET")
    assert "/repos/owner/repo/commits/deadbeef/check-runs" in seen["path"]
    assert "check_name" in seen["path"]


@pytest.mark.parametrize("status", ["waiting", "requested", "pending", "queued"])
def test_find_nonterminal_returns_id_for_other_unfinished_statuses(monkeypatch, status):
    _fake_token(monkeypatch, {})
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {
            "total_count": 1,
            "check_runs": [{"id": 4242, "status": status, "conclusion": None}],
        },
    )
    assert (
        checkrun.find_nonterminal(agent_backend.CODEX, "owner/repo", "deadbeef") == 4242
    )


def test_find_nonterminal_returns_none_for_terminal_run(monkeypatch):
    _fake_token(monkeypatch, {})
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {
            "total_count": 1,
            "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}],
        },
    )
    assert (
        checkrun.find_nonterminal(agent_backend.CODEX, "owner/repo", "deadbeef") is None
    )


def test_find_nonterminal_returns_none_when_absent(monkeypatch):
    _fake_token(monkeypatch, {})
    monkeypatch.setattr(
        checkrun.gh,
        "rest",
        lambda path, *, method=None, body=None, token=None: {
            "total_count": 0,
            "check_runs": [],
        },
    )
    assert (
        checkrun.find_nonterminal(agent_backend.CODEX, "owner/repo", "deadbeef") is None
    )


def test_find_nonterminal_authored_via_installation_token(monkeypatch):
    auth: dict = {}
    _fake_token(monkeypatch, auth, value="ghs_appInstallToken")
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["token"] = token
        return {"check_runs": []}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.find_nonterminal(agent_backend.CODEX, "owner/repo", "deadbeef")

    assert auth == {"backend": agent_backend.CODEX, "repo": "owner/repo"}
    assert seen["token"] == "ghs_appInstallToken"


def test_find_nonterminal_filters_by_reviewer_name(monkeypatch):
    from urllib.parse import quote

    _fake_token(monkeypatch, {})
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        return {"check_runs": []}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)

    checkrun.find_nonterminal(agent_backend.ANTIGRAVITY, "owner/repo", "cafef00d")

    assert quote("review: agy-local") in seen["path"]
