"""Tests for the funnel breadcrumb wired into `service.run_and_post`.

OBS02-WS01: the kickoff that opens the `in_progress` `review: <reviewer>` check
run is the SAME flow that later posts the review. The create is **best-effort** —
per the PRD prerequisite, until the App's `checks:write` re-grant propagates a
create can 403, and the local review must STILL post. So a failed breadcrumb is
logged (the failure FACT, never the token) and swallowed; `generate_review` /
`post_review` proceed unaffected.

OBS02-WS02: the SAME flow transitions that run to its terminal conclusion at
completion — posted → completed/success (the review POST still fires unchanged
first), a failed run → failure, an empty run (no parseable review) → failure with
an `empty` reason, a timeout → timed_out. The transition is best-effort too: a
PATCH failure / a `run_id is None` (create never opened a run) never crashes the
flow nor masks the review's real outcome (the original error still propagates on
the failure paths).

The App-token boundary (`ghauth`) and the `gh` check-run POST/PATCH are FAKED —
never live GitHub.
"""

from __future__ import annotations

import logging

import pytest

from shipit.review import service
from shipit.review.backends.base import BackendError
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


def _fake_checkrun_boundary(monkeypatch, *, create_id: int | None = 555) -> list[dict]:
    """Fake the App-token mint + the `gh` REST seam for BOTH the kickoff create
    (POST -> a run id) and the terminal transition (PATCH -> recorded). Returns the
    list of `{method, path, body}` calls so a test can assert one create + one
    PATCH on the same run id (never live GitHub)."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    calls: list[dict] = []

    def fake_rest(path, *, method=None, body=None, token=None):
        calls.append({"method": method, "path": path, "body": body})
        if method == "POST":
            return {"id": create_id} if create_id is not None else {}
        return {}

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)
    return calls


def test_kickoff_opens_funnel_run_then_posts(monkeypatch, _stub_pipeline):
    """The kickoff opens the in_progress funnel run (via the App token) and then
    posts the review — one flow."""
    calls = _fake_checkrun_boundary(monkeypatch)

    result = service.run_and_post("codex", 5)

    created = next(c for c in calls if c["method"] == "POST")
    assert created["path"] == "/repos/owner/repo/check-runs"
    assert created["body"]["name"] == "review: codex-local"
    assert created["body"]["status"] == "in_progress"
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_posted_transitions_run_to_success(monkeypatch, _stub_pipeline):
    """A posted review closes the SAME run to completed/success — one create, one
    PATCH to that run id (no second create), with an output message + completed_at —
    while the existing structured-review POST still fires unchanged."""
    calls = _fake_checkrun_boundary(monkeypatch)

    result = service.run_and_post("codex", 5)

    posts = [c for c in calls if c["method"] == "POST"]
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(posts) == 1  # exactly one create...
    assert len(patches) == 1  # ...and one terminal transition
    assert posts[0]["path"] == "/repos/owner/repo/check-runs"
    assert patches[0]["path"] == "/repos/owner/repo/check-runs/555"  # the SAME run
    body = patches[0]["body"]
    assert body["status"] == "completed"
    assert body["conclusion"] == "success"
    assert body["output"]["title"] and body["output"]["summary"]  # output message
    assert body["completed_at"]  # tz-aware "now" is asserted in the checkrun test
    # The existing structured-review POST fired unchanged on the success path.
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_failed_transitions_run_to_failure(monkeypatch, _stub_pipeline):
    """An agent error (a missing CLI / a crash) closes the run to completed/failure
    and the original error still propagates (the breadcrumb never swallows it)."""
    from shipit.review.backends.base import BackendUnavailable

    calls = _fake_checkrun_boundary(monkeypatch)

    def _boom(agent, ctx, **kw):
        raise BackendUnavailable("the 'codex' CLI was not found")

    monkeypatch.setattr(service, "generate_review", _boom)

    with pytest.raises(BackendUnavailable):
        service.run_and_post("codex", 5)

    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["conclusion"] == "failure"
    # Generation failed first, so the review POST never ran.
    assert _stub_pipeline.get("called") is not True


def test_empty_transitions_run_to_failure_with_empty_reason(
    monkeypatch, _stub_pipeline
):
    """An EMPTY review (no parseable output, the agy mode — a BackendError WITHOUT
    the timeout marker) closes the run to failure (or neutral) with an output reason
    of `empty` — degraded, NOT success — and re-raises."""
    calls = _fake_checkrun_boundary(monkeypatch)

    def _empty(agent, ctx, **kw):
        raise BackendError(
            "the agent returned no parseable JSON (it may have timed out or "
            "been truncated)\nraw output: <not json>"
        )

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_and_post("codex", 5)

    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] in {"failure", "neutral"}
    output = patch["body"]["output"]
    assert "empty" in (output["title"] + output["summary"]).lower()


def test_timed_out_transitions_run_to_timed_out(monkeypatch, _stub_pipeline):
    """A timeout (a BackendError carrying the `_TIMEOUT_MARKER`) closes the run to
    completed/timed_out and re-raises."""
    from shipit.review.backends.base import _TIMEOUT_MARKER

    calls = _fake_checkrun_boundary(monkeypatch)

    def _timed(agent, ctx, **kw):
        raise BackendError(
            "codex timed out before returning a complete review\n"
            f"raw output: …{_TIMEOUT_MARKER}"
        )

    monkeypatch.setattr(service, "generate_review", _timed)

    with pytest.raises(BackendError):
        service.run_and_post("codex", 5)

    patch = next(c for c in calls if c["method"] == "PATCH")
    assert patch["body"]["conclusion"] == "timed_out"


def test_transition_failure_does_not_mask_success_outcome(
    monkeypatch, _stub_pipeline, caplog
):
    """A PATCH failure on the terminal transition is best-effort: on the success
    path the review has already posted, so `run_and_post` returns its normal result
    and never crashes."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def fake_rest(path, *, method=None, body=None, token=None):
        if method == "POST":
            return {"id": 555}
        raise service.gh.GhError("PATCH 403 Resource not accessible by integration")

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        result = service.run_and_post("codex", 5)

    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "transition" in text.lower()


def test_transition_failure_on_error_path_still_raises_review_error(
    monkeypatch, _stub_pipeline
):
    """On a failure path, a PATCH failure during the terminal transition must NOT
    mask the review's real error — the original BackendError still propagates."""
    monkeypatch.setattr(
        service.checkrun.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )

    def fake_rest(path, *, method=None, body=None, token=None):
        if method == "POST":
            return {"id": 555}
        raise service.gh.GhError("PATCH failed")

    monkeypatch.setattr(service.checkrun.gh, "rest", fake_rest)

    def _empty(agent, ctx, **kw):
        raise BackendError("no parseable JSON\nraw output:")

    monkeypatch.setattr(service, "generate_review", _empty)

    with pytest.raises(BackendError):
        service.run_and_post("codex", 5)


def test_no_transition_when_create_returned_no_run_id(monkeypatch, _stub_pipeline):
    """If the kickoff create returned no run id (a 403 before the re-grant left no
    run), there is nothing to transition — no PATCH is sent, no crash, and the
    review still posts."""
    calls = _fake_checkrun_boundary(monkeypatch, create_id=None)

    result = service.run_and_post("codex", 5)

    assert not [c for c in calls if c["method"] == "PATCH"]
    assert _stub_pipeline["called"] is True
    assert result["post"] == {"id": 99}


def test_breadcrumb_failure_does_not_fail_the_review(
    monkeypatch, _stub_pipeline, caplog
):
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
