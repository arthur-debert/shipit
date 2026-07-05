"""Model-level invariants: thread accessors and head/resolved filtering."""

from __future__ import annotations

from shipit.identity import Sha
from shipit.prstate.model import Review, ReviewComment, Thread, readiness_view

HEAD = Sha("beef" * 10)
STALE = Sha("dead" * 10)


def _thread(thread_id, resolved, *comments):
    return Thread(thread_id=thread_id, is_resolved=resolved, comments=tuple(comments))


def test_thread_location_comes_from_root_comment():
    root = ReviewComment(comment_id=1, path="a.py", line=10, body="x", author="Copilot")
    reply = ReviewComment(comment_id=2, path="a.py", line=10, body="ok", author="me")
    t = _thread("PRT_1", False, root, reply)
    assert t.path == "a.py"
    assert t.line == 10
    assert t.root_comment_id == 1
    assert t.author == "Copilot"


def test_empty_thread_has_no_location():
    t = _thread("PRT_empty", False)
    assert t.path is None
    assert t.line is None
    assert t.root_comment_id is None


def test_reviews_on_head_filters_stale():
    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[
            Review(1, "Copilot", "COMMENTED", HEAD, ""),
            Review(2, "Copilot", "COMMENTED", STALE, ""),
        ],
    )
    assert [r.review_id for r in ctx.reviews_on_head()] == [1]


def test_reviews_on_head_case_mismatch_cannot_flip_staleness():
    # COR02 (#251): the head and the review's commit are both `Sha`s —
    # lowercase-normalized at construction — so a case-varying source can no
    # longer make a current-head review silently read stale.
    ctx = readiness_view(
        number=1,
        head_sha=str(HEAD).upper(),
        is_draft=True,
        reviews=[Review(1, "Copilot", "COMMENTED", Sha(str(HEAD).upper()), "")],
    )
    assert [r.review_id for r in ctx.reviews_on_head()] == [1]


def test_review_with_unknown_commit_is_not_on_head():
    # A review whose wire node carried no commit is honestly-unknown (None) — it
    # never counts as on-head.
    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        reviews=[Review(1, "Copilot", "COMMENTED", None, "")],
    )
    assert ctx.reviews_on_head() == []


def test_open_threads_excludes_resolved():
    ctx = readiness_view(
        number=1,
        head_sha=HEAD,
        is_draft=True,
        threads=[_thread("a", False), _thread("b", True)],
    )
    assert [t.thread_id for t in ctx.open_threads()] == ["a"]
