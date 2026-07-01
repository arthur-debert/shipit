"""Tests for `shipit.review.post` — payload build + the as-app post boundary.

The payload build is a pure transform (no network); the post path is exercised
with the `gh` boundary and the `ghauth` installation-token mint mocked, asserting
the bot-token seam (`gh.rest(..., token=...)`) is used when `as_app=True`.
"""

from __future__ import annotations

import pytest

from shipit.review import post
from shipit.review.diff import review_view

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
 y = 2
"""


def _ctx() -> object:
    return review_view(
        number=5,
        repo="owner/repo",
        head_sha="deadbeef",
        base_ref="main",
        base_sha="cafe",
        diff=_DIFF,
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
    assert payload["commit_id"] == "deadbeef"
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
    result = post.post_review(review, _ctx(), agent_name="codex", as_app=True)
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
        post.post_review(review, _ctx(), agent_name="codex", as_app=True)
