"""Tests for `shipit.review.post` — payload build + the as-app post boundary.

The payload build is a pure transform (no network); the post path is exercised
with the `gh` boundary and the `ghauth` installation-token mint mocked, asserting
the bot-token seam (`gh.rest(..., token=...)`) is used when `as_app=True`.
"""

from __future__ import annotations

import pytest

from shipit import finding
from shipit.agent import backend as agent_backend
from shipit.identity import repo_from_slug
from shipit.review import post
from shipit.review.diff import ReviewView, review_view

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
 y = 2
"""


def _ctx() -> ReviewView:
    return review_view(
        number=5,
        repo="owner/repo",
        head_sha="deadbeef" * 5,  # a full 40-hex sha (COR02)
        base_ref="main",
        base_sha="cafe" * 10,  # a full 40-hex sha (PROC03)
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
    )


def test_commentable_lines_anchors_added_and_context_lines():
    lines = post.commentable_lines(_DIFF)
    # New-file lines 1 (context "import os"), 2 (added "x = 1"), 3 (context "y = 2").
    assert lines["foo.py"] == {1, 2, 3}


def test_payload_anchors_in_diff_and_folds_unanchored():
    review = {
        "summary": {"status": "REQUEST_CHANGES", "overall_feedback": "needs work"},
        "comments": [
            {
                "file": "foo.py",
                "line": 2,
                "text": "bad",
                "severity": "major",
                "category": "correctness",
                "confidence": 0.9,
                "evidence": "x=1",
                "fix": "",
            },
            {
                "file": "foo.py",
                "line": 99,
                "text": "offdiff",
                "severity": "minor",
                "category": "tests",
                "confidence": 0.5,
                "evidence": "",
                "fix": "",
            },
        ],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    assert payload["commit_id"] == "deadbeef" * 5
    assert payload["event"] == "REQUEST_CHANGES"
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["line"] == 2
    assert payload["comments"][0]["side"] == "RIGHT"
    # The off-diff finding is folded into the body, not emitted inline — with the
    # Conventional Comments label, never the retired [SEVERITY] bracket.
    assert "Findings not anchored" in payload["body"]
    assert "`foo.py:99` suggestion (non-blocking): offdiff" in payload["body"]
    assert "[minor]" not in payload["body"] and "[INFO]" not in payload["body"]


def test_inline_comment_body_is_the_two_layer_rendering():
    """The inline body carries the machine marker (exact tuple recoverable) plus
    the Conventional Comments layer; the `Agent: <name> [SEVERITY]` prefix is
    retired."""
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        "comments": [
            {
                "file": "foo.py",
                "line": 2,
                "text": "boom",
                "severity": "critical",
                "category": "security",
                "confidence": 0.8,
                "evidence": "x = 1",
                "fix": "drop it",
            }
        ],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    body = payload["comments"][0]["body"]
    assert "Agent:" not in body  # the retired prefix
    assert "issue (critical, blocking): boom" in body
    recovered = finding.parse_comment(body, file="foo.py", line=2)
    assert recovered.severity is finding.Severity.CRITICAL
    assert recovered.category == "security"
    assert recovered.confidence == 0.8
    assert recovered.evidence == "x = 1"
    assert recovered.fix == "drop it"


def test_findings_are_ordered_highest_severity_first():
    def _comment(severity: str, line: int) -> dict:
        return {
            "file": "foo.py",
            "line": line,
            "text": severity,
            "severity": severity,
            "category": "",
            "confidence": 1.0,
            "evidence": "",
            "fix": "",
        }

    review = {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        # emitted out of order: nit, critical, minor — all anchorable lines
        "comments": [
            _comment("nit", 1),
            _comment("critical", 2),
            _comment("minor", 3),
        ],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    assert [c["line"] for c in payload["comments"]] == [2, 3, 1]


def test_unparseable_severity_fails_safe_to_major():
    """The fail-safe: a finding whose severity can't be parsed (incl. the retired
    ERROR/WARNING/INFO triple) posts as `major`."""
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        "comments": [
            {
                "file": "foo.py",
                "line": 2,
                "text": "legacy",
                "severity": "ERROR",
                "category": "",
                "confidence": 1.0,
                "evidence": "",
                "fix": "",
            }
        ],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    body = payload["comments"][0]["body"]
    assert "issue (blocking): legacy" in body
    assert finding.parse_comment(body).severity is finding.Severity.MAJOR


def test_coverage_attestation_renders_in_the_review_body():
    review = {
        "summary": {
            "status": "COMMENT",
            "overall_feedback": "ok",
            "coverage": {
                "reviewed": ["foo.py", "bar.py:1-40"],
                "skipped": [{"file": "big.lock", "reason": "generated"}],
            },
        },
        "comments": [],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    assert "### Coverage" in payload["body"]
    assert "`foo.py`" in payload["body"] and "`bar.py:1-40`" in payload["body"]
    assert "Skipped: `big.lock` — generated" in payload["body"]


def test_summary_without_coverage_renders_no_coverage_section():
    """The salvage / dry-run paths build summaries with no attestation — the body
    must not grow an empty Coverage header."""
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        "comments": [],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    assert "### Coverage" not in payload["body"]


def test_event_override_wins():
    review = {
        "summary": {"status": "APPROVED", "overall_feedback": "ok"},
        "comments": [],
    }
    payload = post.build_review_payload(
        review, _ctx(), agent_name="codex", event="COMMENT"
    )
    assert payload["event"] == "COMMENT"


def test_post_as_app_uses_installation_token(monkeypatch):
    """`as_app=True` mints an installation token and passes it to gh.rest as the
    bot-token seam."""
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        "comments": [],
    }

    monkeypatch.setattr(
        post.ghauth, "installation_token", lambda agent, repo: "ghs_tok"
    )
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen["path"] = path
        seen["token"] = token
        seen["method"] = method
        return {"id": 1, "user": {"login": "adr-codex-review[bot]"}}

    monkeypatch.setattr(post.gh, "rest", fake_rest)
    result = post.post_review(review, _ctx(), backend=agent_backend.CODEX, as_app=True)
    assert seen["path"] == "/repos/owner/repo/pulls/5/reviews"
    assert seen["token"] == "ghs_tok"
    assert seen["method"] == "POST"
    assert result["user"]["login"] == "adr-codex-review[bot]"


def test_post_as_app_auth_failure_is_actionable(monkeypatch):
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        "comments": [],
    }

    def boom(agent, repo):
        raise post.ghauth.ReviewAuthError("doppler down")

    monkeypatch.setattr(post.ghauth, "installation_token", boom)
    with pytest.raises(RuntimeError, match="Could not authenticate"):
        post.post_review(review, _ctx(), backend=agent_backend.CODEX, as_app=True)


def test_resolve_repo_uses_the_view_slug_when_known(monkeypatch):
    """The resolved-PR source of truth: a view carrying a real slug posts there and
    NEVER re-infers via `gh repo view`."""
    monkeypatch.setattr(
        post.gh,
        "current_repo",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer when repo known")),
    )
    assert post._resolve_repo(_ctx()) == "owner/repo"


def test_resolve_repo_falls_back_to_gh_for_handbuilt_context(monkeypatch):
    """The falsey-repo fallback (ADR-0024): a hand-built view (`repo is None`) infers
    the post target via `gh repo view` rather than posting to a `local/local`
    placeholder."""
    ctx = review_view(
        number=5,
        repo=None,
        head_sha="deadbeef" * 5,  # a full 40-hex sha (COR02)
        base_ref="main",
        base_sha="cafe" * 10,  # a full 40-hex sha (PROC03)
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
    )
    assert ctx.repo is None
    monkeypatch.setattr(
        post.gh, "current_repo", lambda: repo_from_slug("inferred/repo")
    )
    assert post._resolve_repo(ctx) == "inferred/repo"
