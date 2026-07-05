"""The review-round stopping rule + its fold-in to the state machine.

The rule: address every comment each round EXCEPT stop when the round cap is
reached (shipped default 6; repo policy overrides it via `Roster.round_cap`),
or when the latest round's RECORDED verdicts are all nitpick (#423 — the agent
addressing the round classifies every finding; the breaker consumes verdicts
only, never a body regex; the old `_NITPICK_MARKERS` machinery is deleted). A
stop on an otherwise-ready PR routes to READY (the leftover threads no longer
hold Ready) — and an all-nitpick stop suppresses every RE-REQUEST, so the
nit-fix push cannot re-open the loop; a real CI/merge problem still blocks on
its own terms. In front of it all sits the CLASSIFY gate: a latest round with
any unclassified finding reports CLASSIFY and refuses to advance.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from itertools import count

import shipit.prstate.breakers as breakers_module
from shipit.identity import Sha
from shipit.prstate.breakers import (
    ROUND_CAP,
    build_rounds,
    evaluate_breakers,
    is_all_nitpick_round,
    unclassified_findings,
)
from shipit.prstate.model import Review, ReviewComment, Thread, readiness_view
from shipit.prstate.reviewers import by_name
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import TaskState, evaluate

# The REAL cosmetic finding bodies from PR #412 — the rounds that motivated
# #423: Copilot does NOT tag its nits, so under the old marker regex these read
# as substantive and the all-nitpick breaker could never fire. Under the
# verdict model the body TEXT is irrelevant — only the recorded verdict counts.
PR412_NIT_GRAMMAR = (
    "capitalize the first word of the sentence for correct English grammar"
)
PR412_NIT_PRD_LINK = "consider referencing the CLI02 PRD inline"


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
    not the round history). Grab its verdict key via `fid(thread)`.
    """
    cid = next(_FID)
    comment = ReviewComment(
        comment_id=cid, path=path, line=line, body=body, author="Copilot", review_id=rid
    )
    return Thread(thread_id=f"PRT_f{cid}", is_resolved=True, comments=(comment,))


def fid(thread: Thread) -> int:
    """The finding thread's verdict key — its root comment id (#423)."""
    assert thread.root is not None
    return thread.root.comment_id


def classified(threads, verdict: str) -> dict[int, str]:
    """A verdict record classifying every given finding thread as `verdict`."""
    return {fid(t): verdict for t in threads}


def ctx(
    reviews,
    *,
    findings=None,
    threads=None,
    head=None,
    mergeable="MERGEABLE",
    merge_state="CLEAN",
    checks=None,
    roster=None,
    verdicts=None,
    sightings=None,
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
        # The SHIPPED default roster as a passed value (CLI01-WS04): these
        # tests count rounds against the default required set (copilot-only),
        # not this repo's deployed `.shipit.toml` policy. Pass `roster` to
        # model a repo policy override (e.g. a configured `round_cap`).
        roster=roster if roster is not None else default_roster(),
        # The recorded finding verdicts (#423): comment id -> verdict, exactly
        # what the gather seam folds on from the dev-cycle event log. Omitted
        # means UNCLASSIFIED — nothing auto-classifies.
        verdicts=verdicts,
        # The invocation's first-sight registry (ADR-0021 rule 4): pass one to
        # thread it across several views, as `pr next`'s gathers do.
        sightings=sightings,
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
    assert [f.body for f in rounds[0].findings] == ["substantive bug here"]


def test_build_rounds_findings_come_from_threads_even_when_resolved():
    # Findings derive from review threads (the GraphQL source of truth) keyed
    # by review_id; a RESOLVED thread still counts toward its round's findings.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [finding(1, "a.py", 1, "fix A"), finding(2, "b.py", 2, "fix B")]
    rounds = build_rounds(ctx(reviews, findings=findings))
    assert [f.body for f in rounds[0].findings] == ["fix A"]
    assert [f.body for f in rounds[1].findings] == ["fix B"]


def test_rounds_carry_finding_identity_for_the_verdict_key():
    # A round's findings ride WHOLE (#423): the comment id is the verdict key,
    # so the breaker, the classify verb, and the gate all read one identity.
    f = finding(1, "a.py", 3, PR412_NIT_GRAMMAR)
    rounds = build_rounds(ctx([review(1, "c1")], findings=[f]))
    (only,) = rounds[0].findings
    assert only.comment_id == fid(f)
    assert only.body == PR412_NIT_GRAMMAR


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
    assert {f.body for f in rounds[0].findings} == {"fix A", "fix B"}


# --- the round cap (shipped default 6, configurable) -----------------------


def test_shipped_default_cap_is_six():
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


def test_cap_fires_regardless_of_verdict_state():
    # Six rounds of plainly substantive, entirely UNCLASSIFIED findings still
    # trip the raw count cap — the cap is mechanical, not verdict-aware.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i, "real correctness bug") for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == "round-cap"


def test_configured_cap_of_two_fires_on_round_two():
    # The cap is repo policy on the snapshot roster (`[reviewers].round_cap` →
    # Roster.round_cap): with a cap of 2, the 2nd round is the last — the
    # breaker fires well under the shipped default of 6, and the reason
    # reports the CONFIGURED cap, not the constant.
    capped = replace(default_roster(), round_cap=2)
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [finding(1, "a.py", 1), finding(2, "b.py", 2)]
    v = evaluate_breakers(ctx(reviews, findings=findings, roster=capped))
    assert v.stop and v.breaker == "round-cap" and v.cycles == 2
    assert "cap of 2" in v.reason


def test_configured_cap_looser_than_default_defers_the_stop():
    # A cap ABOVE the shipped default also holds: 6 rounds under a cap of 8
    # do not stop — the roster value replaces the constant in both directions.
    capped = replace(default_roster(), round_cap=8)
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings, roster=capped))
    assert not v.stop
    assert v.cycles == 6


# --- the all-nitpick early stop: recorded verdicts ONLY (#423) --------------


def test_all_nitpick_round_from_recorded_verdicts():
    # The REAL PR #412 shape: untagged Copilot cosmetics. The bodies carry no
    # marker at all — the round is all-nitpick purely because the agent
    # recorded a `nitpick` verdict for every finding.
    findings = [
        finding(1, "a.py", 1, PR412_NIT_GRAMMAR),
        finding(1, "b.py", 2, PR412_NIT_PRD_LINK),
    ]
    c = ctx([review(1, "c1")], findings=findings)
    rnd = build_rounds(c)[0]
    assert is_all_nitpick_round(rnd, classified(findings, "nitpick"))


def test_unclassified_finding_means_not_all_nitpick():
    # No auto-classification: the same cosmetic bodies WITHOUT verdicts are
    # simply unclassified — not nitpicks, not substantive, and never all-nitpick.
    findings = [
        finding(1, "a.py", 1, PR412_NIT_GRAMMAR),
        finding(1, "b.py", 2, PR412_NIT_PRD_LINK),
    ]
    rnd = build_rounds(ctx([review(1, "c1")], findings=findings))[0]
    assert not is_all_nitpick_round(rnd, {})
    # ...including partially classified: one verdict recorded, one missing.
    assert not is_all_nitpick_round(rnd, classified(findings[:1], "nitpick"))
    assert unclassified_findings(rnd, classified(findings[:1], "nitpick")) == (
        rnd.findings[1],
    )


def test_reviewer_nit_tag_is_not_a_verdict():
    # A reviewer's own `nit:` tag is just input to the agent's judgment: a
    # tagged body with NO recorded verdict does not classify itself.
    findings = [finding(1, "a.py", 1, "nit: rename this var")]
    rnd = build_rounds(ctx([review(1, "c1")], findings=findings))[0]
    assert not is_all_nitpick_round(rnd, {})


def test_any_substantive_verdict_means_not_all_nitpick():
    findings = [
        finding(1, "a.py", 1, PR412_NIT_GRAMMAR),
        finding(1, "b.py", 2, "this is an actual logic bug"),
    ]
    rnd = build_rounds(ctx([review(1, "c1")], findings=findings))[0]
    verdicts = {
        fid(findings[0]): "nitpick",
        fid(findings[1]): "substantive",
    }
    assert not is_all_nitpick_round(rnd, verdicts)


def test_empty_round_is_not_all_nitpick():
    # A clean/approving pass leaves no findings — not "all nitpicks".
    rnd = build_rounds(ctx([review(1, "c1")]))[0]
    assert not is_all_nitpick_round(rnd, {})


def test_the_marker_regex_machinery_is_gone():
    # #423 deleted auto-classification wholesale: no marker list, no body
    # regex, no fallback — extending a marker list just chases reviewer
    # phrasing forever.
    for name in ("_NITPICK_MARKERS", "_NITPICK_RE", "_is_nitpick", "_marker_pattern"):
        assert not hasattr(breakers_module, name)


def test_all_nitpick_latest_round_stops_early():
    # Two substantive rounds, then a 3rd round classified all-nitpick -> stop
    # early (no 4th round) even though we're far under the 6-round cap.
    reviews = [review(1, "c1"), review(2, "c2"), review(3, "c3")]
    findings = [
        finding(1, "a.py", 1, "real bug"),
        finding(2, "b.py", 2, "another real bug"),
        finding(3, "c.py", 3, PR412_NIT_GRAMMAR),
    ]
    verdicts = {
        fid(findings[0]): "substantive",
        fid(findings[1]): "substantive",
        fid(findings[2]): "nitpick",
    }
    v = evaluate_breakers(ctx(reviews, findings=findings, verdicts=verdicts))
    assert v.stop and v.breaker == "all-nitpick" and v.cycles == 3


def test_substantive_latest_round_does_not_stop():
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, PR412_NIT_GRAMMAR),
        finding(2, "b.py", 2, "a real correctness problem"),
    ]
    verdicts = {
        fid(findings[0]): "nitpick",
        fid(findings[1]): "substantive",
    }
    v = evaluate_breakers(ctx(reviews, findings=findings, verdicts=verdicts))
    assert not v.stop


def test_earlier_nitpick_round_does_not_stop_when_latest_is_substantive():
    # Only the LATEST round's verdicts matter for the all-nitpick stop.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, "nit: rename"),
        finding(2, "b.py", 2, "fix the off-by-one"),
    ]
    verdicts = {
        fid(findings[0]): "nitpick",
        fid(findings[1]): "substantive",
    }
    v = evaluate_breakers(ctx(reviews, findings=findings, verdicts=verdicts))
    assert not v.stop


# --- fold-in to state -----------------------------------------------------


# These scenarios model Copilot review rounds; the second required reviewer is
# irrelevant here, so they pin the required set to Copilot.
_COPILOT_ONLY = [by_name("copilot")]

# A rerun=True (head-strict) Copilot — the PR #412 configuration whose
# re-request-per-push is exactly what the all-nitpick stop must suppress.
_RERUN_COPILOT_ROSTER = Roster(
    (RosterEntry(name="copilot", required=True, rerun=True),)
)

_GREEN = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]


def test_open_thread_under_cap_routes_to_addressing():
    reviews = [review(i, f"c{i}") for i in range(1, 3)]
    findings = [finding(1, "a.py", 1), finding(2, "b.py", 2)]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread()],
        head="c2",
        verdicts=classified(findings, "substantive"),
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert status.breaker is None
    assert "triage" in status.next_action


def test_cap_with_open_threads_routes_to_ready_when_otherwise_ready():
    # 6 rounds reached + an open thread, but CI green + CLEAN merge -> the
    # stopping rule means no 7th round: flip to READY, recording the breaker.
    # The findings stay UNCLASSIFIED on purpose: the classify gate yields to
    # the round-cap stop (at the cap no verdict can decide anything).
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread()],
        head="c6",
        checks=_GREEN,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.breaker == "round-cap"
    assert status.cycles == 6


def test_all_nitpick_with_open_threads_routes_to_ready():
    # Latest round classified all-nitpick + an open nitpick thread, CI green +
    # CLEAN -> READY, recording the all-nitpick stop.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, "real bug"),
        finding(2, "b.py", 2, PR412_NIT_PRD_LINK),
    ]
    verdicts = {
        fid(findings[0]): "substantive",
        fid(findings[1]): "nitpick",
    }
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread(body="nit: trailing whitespace")],
        head="c2",
        checks=_GREEN,
        verdicts=verdicts,
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
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread()],
        head="c6",
        checks=_GREEN,
        merge_state="DIRTY",
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.BLOCKED
    assert "conflict" in status.next_action


def test_converged_pr_not_stopped_under_cap():
    # 4 rounds, every thread resolved + every finding classified + green +
    # mergeable -> READY (normal path, no stop fired).
    reviews = [review(i, f"c{i}") for i in range(1, 5)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 5)]
    status = evaluate(
        ctx(
            reviews,
            findings=findings,
            threads=[],
            head="c4",
            checks=_GREEN,
            verdicts=classified(findings, "substantive"),
        ),
        required=_COPILOT_ONLY,
    )
    assert status.state is TaskState.READY
    assert status.cycles == 4
    assert status.breaker is None


# --- the CLASSIFY gate (#423): the authoritative seam ----------------------


def test_unclassified_latest_round_reports_classify_and_refuses_rerequest():
    # The post-push shape that used to open round N+1: threads resolved, the
    # head moved (nit-fix push), rerun=True Copilot's review is stale — but the
    # round is UNCLASSIFIED, so the one next action is CLASSIFY (with the
    # literal command) and NO re-request is advised (`to_request` empty ⇒ the
    # dispatcher can only report).
    findings = [finding(1, "a.py", 1, PR412_NIT_GRAMMAR)]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        head="c2",  # pushed past the reviewed head
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
    )
    status = evaluate(c)
    assert status.state is TaskState.ADDRESSING
    assert "shipit pr classify" in status.next_action
    assert "nitpick|substantive" in status.next_action
    assert status.to_request == []
    assert status.breaker is None


def test_classify_gate_holds_ready_even_for_a_review_once_reviewer():
    # The review-once (rerun=False, the default) flow has NO stale-review
    # re-request to refuse — the gate must still hold READY: a green, CLEAN,
    # threads-resolved PR with an unclassified round cannot flip.
    findings = [finding(1, "a.py", 1, "a real correctness problem")]
    c = ctx([review(1, "c1")], findings=findings, checks=_GREEN)
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert "shipit pr classify" in status.next_action


def test_classify_gate_mentions_open_threads_still_to_triage():
    # The gate never hides the triage half of the round's work.
    findings = [finding(1, "a.py", 1)]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        threads=[open_copilot_thread()],
        head="c1",
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert "shipit pr classify" in status.next_action
    assert "1 open thread(s)" in status.next_action


def test_fully_classified_round_passes_the_gate():
    # Verdicts recorded for every finding of the latest round: the gate opens
    # and the normal flow resumes (here: substantive round, all resolved,
    # green, CLEAN -> READY with no breaker).
    findings = [finding(1, "a.py", 1)]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        checks=_GREEN,
        verdicts=classified(findings, "substantive"),
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.breaker is None


# --- no re-request on an all-nitpick round (#423) ---------------------------


def test_all_nitpick_round_suppresses_the_stale_review_rerequest():
    # The #412 payoff: the shepherd fixed the nits, classified them all
    # nitpick, and pushed. The push staled rerun=True Copilot's review — but
    # the all-nitpick stop means NO re-request: the stale holder is dropped,
    # nothing is advised, and the otherwise-ready PR routes READY with the
    # breaker recorded. The loop terminates by simply not asking again.
    findings = [
        finding(1, "a.py", 1, PR412_NIT_GRAMMAR),
        finding(1, "b.py", 2, PR412_NIT_PRD_LINK),
    ]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        head="c2",  # the nit-fix push
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
        verdicts=classified(findings, "nitpick"),
    )
    status = evaluate(c)
    assert status.state is TaskState.READY
    assert status.breaker == "all-nitpick"
    assert status.to_request == []
    assert "RE-REQUEST" not in status.next_action


def test_substantive_round_still_rerequests_per_reviewer_policy():
    # The counter-case: a round with any substantive verdict runs the normal
    # cycle — the push staled rerun=True Copilot's review, so the engine
    # advises RE-REQUEST and routes it via the structured `to_request`.
    findings = [
        finding(1, "a.py", 1, "a real correctness problem"),
        finding(1, "b.py", 2, PR412_NIT_GRAMMAR),
    ]
    verdicts = {
        fid(findings[0]): "substantive",
        fid(findings[1]): "nitpick",
    }
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        head="c2",
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
        verdicts=verdicts,
    )
    status = evaluate(c)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST" in status.next_action


# --- round.detected / breaker.fired dev-cycle events (LOG04-WS02 / ADR-0032) --
# The engine's evaluation is the seam that SEES a reviewed head and a fired
# breaker, so `evaluate` tags them — on first sight against the snapshot's
# `events.Sightings` registry (a passed value, ADR-0021 rule 4; `pr next`
# evaluates several snapshots per invocation and threads ONE registry), with
# the round/breaker identity flat on the record.


def _events_named(caplog, name):
    import logging

    from shipit import events

    return [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == name and r.levelno == logging.INFO
    ]


def test_evaluate_tags_one_round_detected_per_reviewed_head(caplog):
    import logging

    from shipit import events

    # ONE first-sight registry threaded across the invocation's snapshots —
    # exactly how `pr next` threads its Sightings through every gather.
    sightings = events.Sightings()
    reviews = [review(10, "a"), review(20, "b")]
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx(reviews, sightings=sightings))
    rounds = _events_named(caplog, "round.detected")
    assert [(r.round, r.commit) for r in rounds] == [
        (1, str(sha("a"))),
        (2, str(sha("b"))),
    ]

    # Re-evaluating the same milestones in the same invocation re-reads the
    # same heads — not a new milestone, nothing re-tagged.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx(reviews, sightings=sightings))
    assert not _events_named(caplog, "round.detected")

    # A NEW head reviewed later IS a new round — only the new one is tagged.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx([*reviews, review(30, "c")], sightings=sightings))
    (fresh,) = _events_named(caplog, "round.detected")
    assert (fresh.round, fresh.commit) == (3, str(sha("c")))


def test_evaluate_tags_breaker_fired_once(caplog):
    import logging

    from shipit import events

    # The round cap fires the stopping rule (default 6 — no 7th round). One
    # Sightings registry threads the invocation's evaluations, as `pr next` does.
    sightings = events.Sightings()
    reviews = [review(10 * i, f"h{i}") for i in range(1, ROUND_CAP + 1)]
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(ctx(reviews, sightings=sightings))
    assert status.breaker == "round-cap"
    (fired,) = _events_named(caplog, "breaker.fired")
    assert fired.breaker == "round-cap"
    assert fired.cycles == ROUND_CAP
    assert fired.pr == status.pr

    # Same breaker on a re-evaluation in the same invocation: already
    # witnessed, not re-tagged.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx(reviews, sightings=sightings))
    assert not _events_named(caplog, "breaker.fired")


def test_all_nitpick_stop_logs_breaker_fired(caplog):
    import logging

    from shipit import events

    # The all-nitpick stop is a dev-cycle milestone too: `breaker.fired` with
    # the breaker name flat on the record (#423 acceptance).
    sightings = events.Sightings()
    findings = [finding(1, "a.py", 1, PR412_NIT_GRAMMAR)]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        checks=_GREEN,
        verdicts=classified(findings, "nitpick"),
        sightings=sightings,
    )
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(c, required=_COPILOT_ONLY)
    assert status.breaker == "all-nitpick"
    (fired,) = _events_named(caplog, "breaker.fired")
    assert fired.breaker == "all-nitpick"


def test_no_breaker_means_no_breaker_event(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(ctx([review(10, "a")]))
    assert status.breaker is None
    assert not _events_named(caplog, "breaker.fired")
