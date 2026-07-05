"""Tests for `shipit.review.funnel_verify` — the OBS02 funnel verification harness.

The harness itself drives LIVE GitHub (kickoff create -> terminal transition on a
canary PR) and is never run by these checks. These tests cover its *wiring and
assertion logic* with the App-token boundary (`ghauth`) and the `gh` check-run
REST seam FAKED — exactly as `test_review_checkrun.py` / `test_review_funnel.py`
fake them — so the harness can't silently rot even though its live mode is opt-in.

The fake `gh.rest` is a tiny GitHub check-run simulator: POST mints a run
(`in_progress` + `started_at`, the "201" a real create returns), PATCH closes the
SAME run to its terminal conclusion, GET reads the current state, and the pulls
endpoint serves the canary head sha.
"""

from __future__ import annotations

import pytest

from shipit.agent import backend as agent_backend
from shipit.execrun import ExecError
from shipit.review import funnel_verify


class _FakeGitHub:
    """A minimal stateful stand-in for the `gh.rest` check-run surface.

    Records every call so a test can assert "exactly one create, one PATCH" and
    inspect the request bodies; serves GETs from the in-memory run store.
    """

    def __init__(self, *, head_sha: str = "deadbeef", create_403: bool = False):
        self.head_sha = head_sha
        self.create_403 = create_403
        self.runs: dict[int, dict] = {}
        self._next_id = 4242
        self.calls: list[dict] = []

    def rest(self, path, *, method=None, body=None, paginate=False, token=None):
        self.calls.append({"method": method or "GET", "path": path, "body": body})
        # Pull lookup -> head sha.
        if path.endswith("/pulls/7"):
            return {"head": {"sha": self.head_sha}}
        # Create a run (POST) -> a fresh in_progress run (the 201 body).
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
        # Transition a run (PATCH) -> the SAME run, closed.
        if method == "PATCH" and "/check-runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            self.runs[run_id].update(body)
            return dict(self.runs[run_id])
        # GET a run.
        if method is None and "/check-runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            return dict(self.runs[run_id])
        raise AssertionError(f"unexpected gh.rest call: {method} {path}")


@pytest.fixture
def healthy(monkeypatch):
    """Fake a fully provisioned owner: the token carries `checks: write` and the
    check-run REST surface behaves. Returns the `_FakeGitHub` for assertions."""
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
    # `checkrun.create`/`transition` mint via `installation_token` (same module).
    monkeypatch.setattr(
        funnel_verify.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    return fake


def test_verify_passes_on_a_healthy_boundary(healthy):
    """The full lifecycle passes: every recorded check is green and the report
    verdict is PASS."""
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is True
    assert all(c.passed for c in report.checks)
    # The harness asserted the load-bearing facts by name.
    names = " | ".join(c.name for c in report.checks)
    assert "checks: write" in names
    assert "201" in names
    assert "in_progress" in names
    assert "completed" in names


def test_verify_drives_one_create_then_one_patch_on_the_same_run(healthy):
    """Exactly one check-run create (201) and one terminal PATCH, both on the SAME
    run id — the harness proves WS01+WS02 share one run, never a second."""
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
    """The harness checks both load-bearing timestamps land on the run."""
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)
    by_name = {c.name: c for c in report.checks}
    assert by_name["kickoff run has a started_at"].passed
    assert by_name["run has a completed_at"].passed


def test_verify_drives_the_requested_conclusion(healthy):
    """A non-default conclusion is driven onto the run and asserted."""
    report = funnel_verify.verify(
        agent_backend.ANTIGRAVITY, "owner/repo", 7, conclusion="timed_out"
    )
    assert report.passed is True
    patches = [c for c in healthy.calls if c["method"] == "PATCH"]
    assert patches[0]["body"]["conclusion"] == "timed_out"


def test_verify_fails_when_token_lacks_checks_write(monkeypatch):
    """If the minted token's permissions omit `checks: write` (re-grant/consent
    not done for this owner), that check FAILS and the verdict is FAIL — but the
    rest of the lifecycle still runs and reports."""
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
    """A 403 on the check-run create (the pre-re-grant failure mode) is caught and
    recorded as the failed "201, not 403" check; the harness stops cleanly with no
    transition attempted."""
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
    # No PATCH was attempted once the create failed.
    assert not [c for c in fake.calls if c["method"] == "PATCH"]


def test_verify_records_auth_failure_without_raising(monkeypatch):
    """A `ReviewAuthError` minting the App token is recorded as the failed scope
    check, not raised — the harness still returns a report (its 0/1 contract)."""
    fake = _FakeGitHub()
    monkeypatch.setattr(funnel_verify.gh, "rest", fake.rest)

    def boom(agent, repo):
        raise funnel_verify.ghauth.ReviewAuthError("app not installed")

    monkeypatch.setattr(funnel_verify.ghauth, "installation_auth", boom)

    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)

    assert report.passed is False
    scope = next(c for c in report.checks if "checks: write" in c.name)
    assert scope.passed is False
    assert "could not mint" in scope.detail
    # Stopped before touching the check-run surface.
    assert not fake.calls


def test_verify_records_head_sha_gh_error_without_raising(monkeypatch):
    """An `ExecError` resolving the PR head sha is recorded, not raised."""
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
    """An `ExecError` on the terminal PATCH is recorded as a failed conclusion check,
    not raised — the harness still prints a structured FAIL."""
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
    """No resolvable head sha (bad PR) fails fast with the head-sha check and no
    check-run is created."""
    fake = _FakeGitHub()

    def rest(path, *, method=None, body=None, paginate=False, token=None):
        if path.endswith("/pulls/7"):
            return {}  # no head
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
    """The console report carries the PASS verdict and one line per check."""
    report = funnel_verify.verify(agent_backend.CODEX, "owner/repo", 7)
    text = funnel_verify.format_report(report, agent="codex", repo="owner/repo", pr=7)
    assert "PASS" in text
    assert "checks: write" in text
    for check in report.checks:
        assert check.name in text


def test_main_requires_an_explicit_canary_target(monkeypatch):
    """`main` REFUSES to run with no --repo/--pr (and no env) — the check against an
    accidental live fire. argparse errors out with a nonzero SystemExit."""
    monkeypatch.delenv("SHIPIT_FUNNEL_CANARY_REPO", raising=False)
    monkeypatch.delenv("SHIPIT_FUNNEL_CANARY_PR", raising=False)
    with pytest.raises(SystemExit) as exc:
        funnel_verify.main([])
    assert exc.value.code != 0


def test_main_returns_zero_on_pass_and_one_on_fail(monkeypatch):
    """`main` exits 0 when the report passes, 1 when it fails — wiring the verdict
    to the process exit code."""
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
