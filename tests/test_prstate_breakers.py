"""The review-round stopping rule + its fold-in to the state machine.

The rule: address every comment each round EXCEPT stop when 6 rounds have
happened, or when the latest round is all nitpicks. A stop on an otherwise-ready
PR routes to READY (the leftover threads no longer hold Ready); a real CI/merge
problem still blocks on its own terms.
"""

from __future__ import annotations

import hashlib
from itertools import count

from shipit.identity import Sha
from shipit.prstate.breakers import (
    ROUND_CAP,
    build_rounds,
    evaluate_breakers,
    is_all_nitpick_round,
)
from shipit.prstate.model import readiness_view, Review, ReviewComment, Thread
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import TaskState, evaluate


def sha(seed: str) -> Sha:
    """A deterministic full `Sha` from a short readable seed (COR02: a commit
    identity must be a validated full sha, so tests derive one per label)."""
    return Sha(hashlib.sha1(seed.encode()).hexdigest())


def review(rid: int, head: str, author: str = "Copilot") -> Review:
    return Review(
        review_id=rid, author=author, state="COMMENTED", commit_id=sha(head), body=""
    )


_FID = count(9000)  # unique comment/thread ids for synthetic findings


def finding(
    rid: int, path: str, line: int, body: str = "substantive bug here"
) -> Thread:
    """A review thread holding one finding submitted with review `rid`.

    Resolved on purpose: a resolved finding was still a finding of that round,
    so the round builder must count it (resolution clears the *open*-thread hold,
    not the round history). `body` defaults to a substantive comment; pass a
    nitpick-marked body to model a cosmetic finding.
    """
    cid = next(_FID)
    comment = ReviewComment(
        comment_id=cid, path=path, line=line, body=body, author="Copilot", review_id=rid
    )
    return Thread(thread_id=f"PRT_f{cid}", is_resolved=True, comments=(comment,))


def ctx(
    reviews,
    *,
    findings=None,
    threads=None,
    head=None,
    mergeable="MERGEABLE",
    merge_state="CLEAN",
    checks=None,
):
    return readiness_view(
        number=1,
        head_sha=sha(head)
        if head
        else (reviews[-1].commit_id if reviews else sha("h")),
        is_draft=True,
        base_ref="main",
        mergeable=mergeable,
        merge_state=merge_state,
        reviews=list(reviews),
        threads=[*(findings or []), *(threads or [])],
        checks=checks or [],
    )


def open_copilot_thread(path="a.py", line=1, body="substantive open issue"):
    comment = ReviewComment(
        comment_id=1, path=path, line=line, body=body, author="Copilot"
    )
    return Thread(thread_id="PRT_1", is_resolved=False, comments=(comment,))


# --- round counting -------------------------------------------------------


def test_build_rounds_one_per_copilot_review_chronological():
    reviews = [review(10, "a"), review(20, "b"), review(5, "c", author="gemini-bot")]
    rounds = build_rounds(ctx(reviews))
    assert [r.index for r in rounds] == [1, 2]
    assert [r.commit_id for r in rounds] == [
        sha("a"),
        sha("b"),
    ]  # gemini excluded, id-ordered


def test_build_rounds_matches_both_copilot_login_variants():
    # The review login is `copilot-pull-request-reviewer[bot]` but the comment
    # author renders as `Copilot` — both must group into rounds (release#455).
    reviews = [review(10, "a", author="copilot-pull-request-reviewer[bot]")]
    rounds = build_rounds(ctx(reviews, findings=[finding(10, "a.py", 1)]))
    assert len(rounds) == 1
    assert rounds[0].bodies == ("substantive bug here",)


def test_build_rounds_findings_come_from_threads_even_when_resolved():
    # Findings derive from review threads (the GraphQL source of truth) keyed
    # by review_id; a RESOLVED thread still counts toward its round's findings.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [finding(1, "a.py", 1, "fix A"), finding(2, "b.py", 2, "fix B")]
    rounds = build_rounds(ctx(reviews, findings=findings))
    assert rounds[0].bodies == ("fix A",)
    assert rounds[1].bodies == ("fix B",)


def test_two_required_reviewers_across_two_heads_is_two_rounds_not_four():
    # The release#622 double-count shape: with TWO required reviewers, two
    # iteration rounds (heads h1, h2) get four review objects (each reviewer
    # reviews each head). Rounds are iterations, not reviews — so this is 2
    # rounds, well under the cap of 6, and nothing stops.
    reviews = [
        review(1, "h1", author="Copilot"),
        review(2, "h1", author="coderabbitai[bot]"),
        review(3, "h2", author="Copilot"),
        review(4, "h2", author="coderabbitai[bot]"),
    ]
    rounds = build_rounds(ctx(reviews))
    assert [r.commit_id for r in rounds] == [
        sha("h1"),
        sha("h2"),
    ]  # one per head, not per review
    assert len(rounds) == 2
    v = evaluate_breakers(ctx(reviews))
    assert v.cycles == 2
    assert not v.stop


def test_a_round_unions_both_reviewers_findings_on_the_same_head():
    # Both required reviewers flag the same head: the round's findings are the
    # UNION of their thread comments. The dual set is the opt-in (phos pilot)
    # config, not the default, so pass it explicitly.
    both = [by_name("copilot"), by_name("coderabbit")]
    reviews = [
        review(1, "h1", author="Copilot"),
        review(2, "h1", author="coderabbitai[bot]"),
    ]
    findings = [finding(1, "a.py", 1, "fix A"), finding(2, "b.py", 2, "fix B")]
    rounds = build_rounds(ctx(reviews, findings=findings), required=both)
    assert len(rounds) == 1
    assert set(rounds[0].bodies) == {"fix A", "fix B"}


# --- the 6-round hard cap -------------------------------------------------


def test_cap_is_six():
    assert ROUND_CAP == 6


def test_five_rounds_under_cap_no_stop():
    reviews = [review(i, f"c{i}") for i in range(1, 6)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 6)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert not v.stop
    assert v.cycles == 5


def test_sixth_round_hits_the_cap():
    # The 6th round is the last; the cap fires once 6 rounds have happened.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == "round-cap" and v.cycles == 6


def test_cap_fires_regardless_of_finding_content():
    # Six rounds of plainly substantive findings still trip the raw count cap —
    # the cap is mechanical, not content-aware.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i, "real correctness bug") for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == "round-cap"


# --- the all-nitpick early stop -------------------------------------------


def test_nitpick_round_detected():
    rnd = build_rounds(
        ctx(
            [review(1, "c1")],
            findings=[
                finding(1, "a.py", 1, "nitpick: rename this var"),
                finding(1, "b.py", 2, "typo in the docstring"),
            ],
        )
    )[0]
    assert is_all_nitpick_round(rnd)


def test_mixed_round_is_not_all_nitpick():
    rnd = build_rounds(
        ctx(
            [review(1, "c1")],
            findings=[
                finding(1, "a.py", 1, "nitpick: rename this var"),
                finding(1, "b.py", 2, "this is an actual logic bug"),
            ],
        )
    )[0]
    assert not is_all_nitpick_round(rnd)


def test_empty_round_is_not_all_nitpick():
    # A clean/approving pass leaves no findings — not "all nitpicks".
    rnd = build_rounds(ctx([review(1, "c1")]))[0]
    assert not is_all_nitpick_round(rnd)


def test_nit_marker_is_word_bounded_not_substring():
    # `nit:` must NOT fire inside an unrelated word like "unit:" — otherwise a
    # substantive round ("unit: add a test for X") would read as all-nitpick and
    # stop the loop early.
    rnd = build_rounds(
        ctx(
            [review(1, "c1")],
            findings=[finding(1, "a.py", 1, "unit: add a test for the new path")],
        )
    )[0]
    assert not is_all_nitpick_round(rnd)


def test_all_nitpick_latest_round_stops_early():
    # Two substantive rounds, then a 3rd round that is purely cosmetic -> stop
    # early (no 4th round) even though we're far under the 6-round cap.
    reviews = [review(1, "c1"), review(2, "c2"), review(3, "c3")]
    findings = [
        finding(1, "a.py", 1, "real bug"),
        finding(2, "b.py", 2, "another real bug"),
        finding(3, "c.py", 3, "nit: tweak the wording here"),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == "all-nitpick" and v.cycles == 3


def test_substantive_latest_round_does_not_stop():
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, "nit: cosmetic"),
        finding(2, "b.py", 2, "a real correctness problem"),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert not v.stop


def test_earlier_nitpick_round_does_not_stop_when_latest_is_substantive():
    # Only the LATEST round's content matters for the all-nitpick stop.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, "nit: rename"),
        finding(2, "b.py", 2, "fix the off-by-one"),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert not v.stop


# --- fold-in to state -----------------------------------------------------


# These scenarios model Copilot review rounds; the second required reviewer is
# irrelevant here, so they pin the required set to Copilot.
_COPILOT_ONLY = [by_name("copilot")]


def test_open_thread_under_cap_routes_to_addressing():
    reviews = [review(i, f"c{i}") for i in range(1, 3)]
    findings = [finding(1, "a.py", 1), finding(2, "b.py", 2)]
    c = ctx(reviews, findings=findings, threads=[open_copilot_thread()], head="c2")
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert status.breaker is None


def test_cap_with_open_threads_routes_to_ready_when_otherwise_ready():
    # 6 rounds reached + an open thread, but CI green + CLEAN merge -> the
    # stopping rule means no 7th round: flip to READY, recording the breaker.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread()],
        head="c6",
        checks=rollup,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.breaker == "round-cap"
    assert status.cycles == 6


def test_all_nitpick_with_open_threads_routes_to_ready():
    # Latest round is all nitpicks + an open nitpick thread, CI green + CLEAN ->
    # READY, recording the all-nitpick stop.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, "real bug"),
        finding(2, "b.py", 2, "nit: wording"),
    ]
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread(body="nit: trailing whitespace")],
        head="c2",
        checks=rollup,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.breaker == "all-nitpick"


def test_stop_does_not_override_a_real_ci_failure():
    # 6 rounds reached, but CI is failing: the real blocker wins, not READY.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread()],
        head="c6",
        checks=rollup,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.BLOCKED
    assert "CI" in status.next_action


def test_stop_does_not_override_a_merge_conflict():
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread()],
        head="c6",
        checks=rollup,
        merge_state="DIRTY",
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.BLOCKED
    assert "conflict" in status.next_action


def test_converged_pr_not_stopped_under_cap():
    # 4 rounds, every thread resolved + green + mergeable -> READY (normal path,
    # no stop fired).
    reviews = [review(i, f"c{i}") for i in range(1, 5)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 5)]
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    status = evaluate(
        ctx(reviews, findings=findings, threads=[], head="c4", checks=rollup),
        required=_COPILOT_ONLY,
    )
    assert status.state is TaskState.READY
    assert status.cycles == 4
    assert status.breaker is None
