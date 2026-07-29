from __future__ import annotations

import pytest

from shipit.agent import backend as agent_backend
from shipit.execrun import ExecError
from shipit.review import funnel_verify


class _FakeGitHub:
    def __init__(self, *, head_sha: str = "deadbeef", create_403: bool = False):
        self.head_sha = head_sha
        self.create_403 = create_403
        self.runs: dict[int, dict] = {}
        self._next_id = 4242
        self.calls: list[dict] = []

    def rest(self, path, *, method=None, body=None, paginate=False, token=None):
        self.calls.append({"method": method or "GET", "path": path, "body": body})
        if path.endswith("/pulls/7"):
            return {"head": {"sha": self.head_sha}}
        if method == "POST" and path.endswith("/check-runs"):
            if self.create_403:
                raise ExecError(
                    ["gh"], rc=1, stderr="403 Resource not accessible by integration"
                )
            run_id = self._next_id
            self._next_id += 1
            run = {
                "id": run_id,
                "status": body["status"],
                "started_at": body["started_at"],
                "head_sha": body["head_sha"],
                "name": body["name"],
            }
            self.runs[run_id] = run
            return dict(run)
        if method == "PATCH" and "/check-runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            self.runs[run_id].update(body)
            return dict(self.runs[run_id])
        if method is None and "/check-runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            return dict(self.runs[run_id])
        raise AssertionError(f"unexpected gh.rest call: {method} {path}")


@pytest.fixture
def healthy(monkeypatch):
    fake = _FakeGitHub()
    monkeypatch.setattr(funnel_verify.gh, "rest", fake.rest)
    monkeypatch.setattr(
        funnel_verify.ghauth,
        "installation_auth",
        lambda agent, repo: {
            "token": "ghs_tok",
            "permissions": {"checks": "write", "pull_requests": "write"},
        },
    )
    monkeypatch.setattr(
        funnel_verify.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    return fake


def test_verify_passes_on_a_healthy_boundary(healthy):
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is True
    assert all(c.passed for c in report.checks)
    names = " | ".join(c.name for c in report.checks)
    assert "checks: write" in names
    assert "201" in names
    assert "in_progress" in names
    assert "completed" in names


def test_verify_drives_one_create_then_one_patch_on_the_same_run(healthy):
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    posts = [c for c in healthy.calls if c["method"] == "POST"]
    patches = [c for c in healthy.calls if c["method"] == "PATCH"]
    assert len(posts) == 1
    assert posts[0]["path"] == "/repos/owner/repo/check-runs"
    assert len(patches) == 1
    assert patches[0]["path"] == f"/repos/owner/repo/check-runs/{report.run_id}"
    assert patches[0]["body"]["status"] == "completed"
    assert patches[0]["body"]["conclusion"] == "success"


def test_verify_asserts_started_at_and_completed_at(healthy):
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)
    by_name = {c.name: c for c in report.checks}
    assert by_name["kickoff run has a started_at"].passed
    assert by_name["run has a completed_at"].passed


def test_verify_drives_the_requested_conclusion(healthy):
    report = funnel_verify.verify(
        agent_backend.ANTIGRAVITY, "owner/repo", 7, conclusion="timed_out"
    )
    assert report.passed is True
    patches = [c for c in healthy.calls if c["method"] == "PATCH"]
    assert patches[0]["body"]["conclusion"] == "timed_out"


def test_verify_fails_when_token_lacks_checks_write(monkeypatch):
    fake = _FakeGitHub()
    monkeypatch.setattr(funnel_verify.gh, "rest", fake.rest)
    monkeypatch.setattr(
        funnel_verify.ghauth,
        "installation_auth",
        lambda agent, repo: {"token": "ghs_tok", "permissions": {"checks": "read"}},
    )
    monkeypatch.setattr(
        funnel_verify.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    scope = next(c for c in report.checks if "checks: write" in c.name)
    assert scope.passed is False
    assert "read" in scope.detail


def test_verify_records_403_on_create_and_stops(monkeypatch):
    fake = _FakeGitHub(create_403=True)
    monkeypatch.setattr(funnel_verify.gh, "rest", fake.rest)
    monkeypatch.setattr(
        funnel_verify.ghauth,
        "installation_auth",
        lambda agent, repo: {
            "token": "ghs_tok",
            "permissions": {"checks": "write"},
        },
    )
    monkeypatch.setattr(
        funnel_verify.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    create_check = next(c for c in report.checks if "201" in c.name)
    assert create_check.passed is False
    assert "403" in create_check.detail
    assert not [c for c in fake.calls if c["method"] == "PATCH"]


def test_verify_records_auth_failure_without_raising(monkeypatch):
    fake = _FakeGitHub()
    monkeypatch.setattr(funnel_verify.gh, "rest", fake.rest)

    def boom(agent, repo):
        raise funnel_verify.ghauth.ReviewAuthError(
            "app not installed", kind=funnel_verify.ghauth.NOT_INSTALLED
        )

    monkeypatch.setattr(funnel_verify.ghauth, "installation_auth", boom)

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    scope = next(c for c in report.checks if "checks: write" in c.name)
    assert scope.passed is False
    assert "could not mint" in scope.detail
    assert not fake.calls


def test_verify_records_head_sha_gh_error_without_raising(monkeypatch):
    monkeypatch.setattr(
        funnel_verify.ghauth,
        "installation_auth",
        lambda agent, repo: {"token": "ghs_tok", "permissions": {"checks": "write"}},
    )

    def rest(path, *, method=None, body=None, paginate=False, token=None):

        raise ExecError(["gh"], rc=1, stderr="PR not accessible")

    monkeypatch.setattr(funnel_verify.gh, "rest", rest)

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    head = next(c for c in report.checks if "head sha" in c.name)
    assert head.passed is False


def test_verify_records_transition_failure_without_raising(monkeypatch):
    fake = _FakeGitHub()

    def rest(path, *, method=None, body=None, paginate=False, token=None):

        if method == "PATCH":
            raise ExecError(["gh"], rc=1, stderr="PATCH 403")
        return fake.rest(path, method=method, body=body, token=token)

    monkeypatch.setattr(funnel_verify.gh, "rest", rest)
    monkeypatch.setattr(
        funnel_verify.ghauth,
        "installation_auth",
        lambda agent, repo: {"token": "ghs_tok", "permissions": {"checks": "write"}},
    )
    monkeypatch.setattr(
        funnel_verify.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    concl = next(c for c in report.checks if "conclusion is success" in c.name)
    assert concl.passed is False
    assert "transition failed" in concl.detail


def test_verify_fails_when_pr_head_cannot_be_resolved(monkeypatch):
    fake = _FakeGitHub()

    def rest(path, *, method=None, body=None, paginate=False, token=None):
        if path.endswith("/pulls/7"):
            return {}
        return fake.rest(path, method=method, body=body, token=token)

    monkeypatch.setattr(funnel_verify.gh, "rest", rest)
    monkeypatch.setattr(
        funnel_verify.ghauth,
        "installation_auth",
        lambda agent, repo: {"token": "ghs_tok", "permissions": {"checks": "write"}},
    )
    monkeypatch.setattr(
        funnel_verify.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    assert any("head sha" in c.name and not c.passed for c in report.checks)


def test_format_report_shows_verdict_and_each_check(healthy):
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)
    text = funnel_verify.format_report(report, agent="codex", repo="owner/repo", pr=7)
    assert "PASS" in text
    assert "checks: write" in text
    for check in report.checks:
        assert check.name in text


def test_main_requires_an_explicit_canary_target(monkeypatch):
    monkeypatch.delenv("SHIPIT_FUNNEL_CANARY_REPO", raising=False)
    monkeypatch.delenv("SHIPIT_FUNNEL_CANARY_PR", raising=False)
    with pytest.raises(SystemExit) as exc:
        funnel_verify.main([])
    assert exc.value.code != 0


def test_main_returns_zero_on_pass_and_one_on_fail(monkeypatch):
    passing = funnel_verify.Report()
    passing.record("ok", True)
    monkeypatch.setattr(funnel_verify, "verify", lambda *a, **k: passing)
    assert (
        funnel_verify.main(["--repo", "owner/repo", "--pr", "7", "--agent", "codex"])
        == 0
    )

    failing = funnel_verify.Report()
    failing.record("nope", False)
    monkeypatch.setattr(funnel_verify, "verify", lambda *a, **k: failing)
    assert funnel_verify.main(["--repo", "owner/repo", "--pr", "7"]) == 1
