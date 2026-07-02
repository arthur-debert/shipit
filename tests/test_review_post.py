"""Tests for `shipit.review.post` — payload build + the as-app post boundary.

The payload build is a pure transform (no network); the post path is exercised
with the `gh` boundary and the `ghauth` installation-token mint mocked, asserting
the bot-token seam (`gh.rest(..., token=...)`) is used when `as_app=True`.
"""

from __future__ import annotations

import pytest

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
        base_sha="cafe",
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
                "severity": "ERROR",
                "code_snippet": "x=1",
            },
            {
                "file": "foo.py",
                "line": 99,
                "text": "offdiff",
                "severity": "INFO",
                "code_snippet": "",
            },
        ],
    }
    payload = post.build_review_payload(review, _ctx(), agent_name="codex")
    assert payload["commit_id"] == "deadbeef" * 5
    assert payload["event"] == "REQUEST_CHANGES"
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["line"] == 2
    assert payload["comments"][0]["side"] == "RIGHT"
    # The off-diff finding is folded into the body, not emitted inline.
    assert "Findings not anchored" in payload["body"]
    assert "foo.py:99" in payload["body"]


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
        base_sha="cafe",
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
    )
    assert ctx.repo is None
    monkeypatch.setattr(
        post.gh, "current_repo", lambda: repo_from_slug("inferred/repo")
    )
    assert post._resolve_repo(ctx) == "inferred/repo"
