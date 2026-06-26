"""Tests for the funnel breadcrumb wired into `service.run_and_post`.

OBS02-WS01: the kickoff that opens the `in_progress` `review: <reviewer>` check
run is the SAME flow that later posts the review. The create is **best-effort** —
per the PRD prerequisite, until the App's `checks:write` re-grant propagates a
create can 403, and the local review must STILL post. So a failed breadcrumb is
logged (the failure FACT, never the token) and swallowed; `generate_review` /
`post_review` proceed unaffected.

The App-token boundary (`ghauth`) and the `gh` check-run POST are FAKED — never
live GitHub.
"""

from __future__ import annotations

import logging

import pytest

from shipit.review import service
from shipit.review.diff import PRContext

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
 y = 2
"""

_REVIEW = {
    "summary": {"status": "COMMENT", "overall_feedback": "looks ok"},
    "comments": [],
}


def _ctx(repo: str | None = "owner/repo") -> PRContext:
    return PRContext(
        number=5,
        repo=repo,
        head_sha="deadbeef",
        base_ref="main",
        base_sha="cafe",
        diff=_DIFF,
        changed_files=["foo.py"],
        workdir="/tmp/wd",
    )


@pytest.fixture
def _stub_pipeline(monkeypatch):
    """Stub the PR resolve + review generation + post so a `run_and_post` call
    exercises ONLY the funnel-breadcrumb wiring. Records the post call."""
    # The real local-review path passes no repo (the adapter calls
    # `run_and_post(name, pr, as_app=True)`), so ctx.repo is None and the
    # breadcrumb infers the slug from the checkout — stub that inference.
    monkeypatch.setattr(service, "resolve_pr", lambda pr, repo=None: _ctx(repo))
    monkeypatch.setattr(service.gh, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr(
        service, "generate_review", lambda agent, ctx, **kw: dict(_REVIEW)
    )
    posted: dict = {}

    def fake_post_review(review, ctx, *, agent_name, event, dry_run, as_app):
        posted["called"] = True
        posted["agent"] = agent_name
        return {"id": 99}

    monkeypatch.setattr(service.post, "post_review", fake_post_review)
    return posted


def test_kickoff_opens_funnel_run_then_posts(monkeypatch, _stub_pipeline):
    """The kickoff opens the in_progress funnel run (via the App token) and then
    posts the review — one flow."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    created: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        created["path"] = path
        created["body"] = body
        return {"id": 555}

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    result = service.run_and_post("codex", 5)

    assert created["path"] == "/repos/owner/repo/check-runs"
    assert created["body"]["name"] == "review: codex-local"
    assert created["body"]["status"] == "in_progress"
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_breadcrumb_failure_does_not_fail_the_review(monkeypatch, _stub_pipeline, caplog):
    """When the check-run create raises (simulated 403 before the `checks:write`
    re-grant), `run_and_post` STILL posts the review and returns its normal
    result — the failure is swallowed and logged, never propagated."""

    def boom(agent, repo):
        raise service.checkrun.ghauth.ReviewAuthError(
            "403 Resource not accessible by integration"
        )

    monkeypatch.setattr(service.checkrun.ghauth, "installation_token", boom)

    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        result = service.run_and_post("codex", 5)

    # The review still posted and the call returned its normal result shape.
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}
    assert result["pr"] == 5
    # The failure fact was logged (and the raw exception text never crashed out).
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "funnel" in text.lower()


def test_breadcrumb_failure_never_leaks_token(monkeypatch, _stub_pipeline, caplog):
    """Even on the failure path, no installation-token value reaches a record."""
    secret = "ghs_leakCanary000111222333"

    def fake_rest(path, *, method=None, body=None, token=None):
        raise service.gh.GhError("create failed")

    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: secret
    )
    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        service.run_and_post("codex", 5)

    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    assert _stub_pipeline["called"] is True
