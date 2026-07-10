"""The review-round stopping rule + its fold-in to the state machine.

The rule (ADR-0044 / RVW02): address every review comment each round EXCEPT
stop when the round cap is reached (shipped default 6; repo policy overrides
it via `Roster.round_cap`), or when the latest round has findings but NONE
major-or-worse — each finding's Severity resolved through the precedence chain
(machine marker → reviewer-adapter mapping → `major` fail-safe, beaten only by
a write-once Severity override). There is no classification step: findings
arrive pre-classified, and nothing gates on a recorded verdict any more. A
fired breaker suppresses every RE-REQUEST (the fix push cannot re-open the
loop), while the round's leftover minor/nit threads still resolve before READY.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from itertools import count

import shipit.prstate.breakers as breakers_module
from shipit.finding import Severity
from shipit.identity import Sha
from shipit.prstate.breakers import (
    NO_MAJOR_FINDING,
    ROUND_CAP,
    build_rounds,
    evaluate_breakers,
    has_blocking_finding,
)
from shipit.prstate.model import Review, ReviewComment, Thread, readiness_view
from shipit.prstate.reviewers import by_name
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import TaskState, evaluate


# Marker-carrying finding bodies (the WS01 wire format): the machine marker is
# the chain's strongest reviewer-emitted rung, so a body built here resolves to
# exactly the named tier with no adapter/default involvement.
def marked(severity: str, text: str = "the claim") -> str:
    return f"<!-- shipit:finding severity={severity} -->\nnitpick: {text}"


# An UNMARKED Copilot body — the unmappable shape (#412's cosmetic nits carried
# no format at all): no marker, no Copilot native vocabulary → the chain lands
# on the `major` fail-safe, which is exactly what keeps it minting rounds.
UNMARKED = "capitalize the first word of the sentence for correct English grammar"


def sha(seed: str) -> Sha:
    """A deterministic full `Sha` from a short readable seed (COR02: a commit
    identity must be a validated full sha, so tests derive one per label)."""
    return Sha(hashlib.sha1(seed.encode()).hexdigest())


def review(rid: int, head: str, author: str = "Copilot") -> Review:
    return Review(
        review_id=rid, author=author, state="COMMENTED", commit_id=sha(head), body=""
    )


_FID = count(9000)  # unique comment/thread ids for synthetic findings


def finding(rid: int, path: str, line: int, body: str = UNMARKED) -> Thread:
    """A review thread holding one finding submitted with review `rid`.

    Resolved on purpose: a resolved finding was still a finding of that round,
    so the round builder must count it (resolution clears the *open*-thread hold,
    not the round history). Grab its override key via `fid(thread)`.
    """
    cid = next(_FID)
    comment = ReviewComment(
        comment_id=cid, path=path, line=line, body=body, author="Copilot", review_id=rid
    )
    return Thread(thread_id=f"PRT_f{cid}", is_resolved=True, comments=(comment,))


def fid(thread: Thread) -> int:
    """The finding thread's Severity-override key — its root comment id."""
    assert thread.root is not None
    return thread.root.comment_id


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
    overrides=None,
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
        # The write-once Severity overrides (ADR-0044): comment id -> Severity,
        # exactly what the gather seam folds on from the dev-cycle event log.
        # Omitted means NO overrides — each finding resolves through the rest
        # of the precedence chain.
        overrides=overrides,
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
    assert [f.body for f in rounds[0].findings] == [UNMARKED]


def test_build_rounds_findings_come_from_threads_even_when_resolved():
    # Findings derive from review threads (the GraphQL source of truth) keyed
    # by review_id; a RESOLVED thread still counts toward its round's findings.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [finding(1, "a.py", 1, "fix A"), finding(2, "b.py", 2, "fix B")]
    rounds = build_rounds(ctx(reviews, findings=findings))
    assert [f.body for f in rounds[0].findings] == ["fix A"]
    assert [f.body for f in rounds[1].findings] == ["fix B"]


def test_rounds_carry_finding_identity_for_the_override_key():
    # A round's findings ride WHOLE (ADR-0044): the body carries the machine
    # marker the severity chain reads, and the comment id is the override key,
    # so the breaker and the classify verb read one finding identity.
    f = finding(1, "a.py", 3, marked("minor"))
    rounds = build_rounds(ctx([review(1, "c1")], findings=[f]))
    (only,) = rounds[0].findings
    assert only.comment_id == fid(f)
    assert only.body == marked("minor")


def test_two_required_reviewers_across_two_heads_is_two_rounds_not_four():
    # The release#622 double-count shape: with TWO required reviewers, two
    # iteration rounds (heads h1, h2) get four review objects (each reviewer
    # reviews each head). Rounds are iterations, not reviews — so this is 2
    # rounds, well under the cap of 6, and nothing stops (the findings are
    # unmarked → `major` fail-safe, so the no-major+ stop stays quiet too).
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


def test_cap_fires_regardless_of_severity_state():
    # Six rounds of major-resolving findings still trip the raw count cap —
    # the cap is mechanical, not severity-aware.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i, marked("critical")) for i in range(1, 7)]
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


# --- the no-major+ early stop: severities off the precedence chain ----------


def test_minor_and_nit_round_stops():
    # A round whose findings all resolve minor/nit (machine markers — the WS01
    # wire format) has nothing a competent reviewer would hold the merge for:
    # the loop stops rather than opening another round.
    findings = [
        finding(1, "a.py", 1, marked("minor")),
        finding(1, "b.py", 2, marked("nit")),
    ]
    c = ctx([review(1, "c1")], findings=findings)
    rnd = build_rounds(c)[0]
    assert not has_blocking_finding(rnd, {})
    v = evaluate_breakers(c)
    assert v.stop and v.breaker == NO_MAJOR_FINDING


def test_any_major_or_worse_finding_keeps_the_loop_running():
    for tier in ("critical", "major"):
        findings = [
            finding(1, "a.py", 1, marked("nit")),
            finding(1, "b.py", 2, marked(tier)),
        ]
        c = ctx([review(1, "c1")], findings=findings)
        assert has_blocking_finding(build_rounds(c)[0], {})
        assert not evaluate_breakers(c).stop


def test_unmarked_finding_resolves_major_and_keeps_the_loop_running():
    # The fail-safe: an unparseable finding (no marker, and Copilot has no
    # native severity vocabulary) resolves `major` — it forces another round
    # rather than slipping the Breaker.
    findings = [
        finding(1, "a.py", 1, marked("nit")),
        finding(1, "b.py", 2, UNMARKED),
    ]
    c = ctx([review(1, "c1")], findings=findings)
    assert not evaluate_breakers(c).stop


def test_a_write_once_override_beats_the_marker_in_both_directions():
    # Downgrade: a marker-major finding overridden to nit lets the stop fire.
    f_major = finding(1, "a.py", 1, marked("major"))
    c = ctx(
        [review(1, "c1")],
        findings=[f_major],
        overrides={fid(f_major): Severity.NIT},
    )
    v = evaluate_breakers(c)
    assert v.stop and v.breaker == NO_MAJOR_FINDING
    # Upgrade: a marker-nit finding overridden to major keeps the loop running.
    f_nit = finding(1, "b.py", 2, marked("nit"))
    c = ctx(
        [review(1, "c1")],
        findings=[f_nit],
        overrides={fid(f_nit): Severity.MAJOR},
    )
    assert not evaluate_breakers(c).stop


def test_empty_round_does_not_fire_the_no_major_stop():
    # A clean/approving pass leaves no findings — nothing to address, so the
    # normal readiness checks handle it; the breaker stays quiet.
    v = evaluate_breakers(ctx([review(1, "c1")]))
    assert not v.stop


def test_the_verdict_machinery_is_gone():
    # ADR-0044 retired classification wholesale: no recorded-verdict reads, no
    # all-nitpick rule, no CLASSIFY gate input — severity is the routing key.
    for name in ("is_all_nitpick_round", "unclassified_findings", "NITPICK"):
        assert not hasattr(breakers_module, name)


def test_no_major_latest_round_stops_early():
    # Two major rounds, then a 3rd round of minor/nit findings -> stop early
    # (no 4th round) even though we're far under the 6-round cap.
    reviews = [review(1, "c1"), review(2, "c2"), review(3, "c3")]
    findings = [
        finding(1, "a.py", 1, marked("major")),
        finding(2, "b.py", 2, marked("major")),
        finding(3, "c.py", 3, marked("minor")),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == NO_MAJOR_FINDING and v.cycles == 3


def test_earlier_nit_round_does_not_stop_when_latest_is_major():
    # Only the LATEST round's severities matter for the no-major+ stop.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, marked("nit")),
        finding(2, "b.py", 2, marked("major")),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert not v.stop


# --- fold-in to state -----------------------------------------------------


# These scenarios model Copilot review rounds; the second required reviewer is
# irrelevant here, so they pin the required set to Copilot.
_COPILOT_ONLY = [by_name("copilot")]

# A rerun=True (head-strict) Copilot — the configuration whose
# re-request-per-push is exactly what a fired breaker must suppress.
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
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert status.breaker is None
    assert "triage" in status.next_action
    assert "severity order" in status.next_action


def test_cap_with_open_threads_still_addresses_but_mints_no_round():
    # 6 rounds reached + an open thread: the loop is over (no 7th round — the
    # breaker is recorded and no re-request will follow), but the leftover
    # thread still requires resolution BEFORE Ready (ADR-0044): the state is
    # ADDRESSING, with the stop named in the prose.
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
    assert status.state is TaskState.ADDRESSING
    assert status.breaker == "round-cap"
    assert status.cycles == 6
    assert "review loop stopped" in status.next_action
    assert "no re-review" in status.next_action


def test_no_major_stop_with_threads_resolved_routes_to_ready():
    # Latest round all minor/nit + every thread resolved, CI green + CLEAN ->
    # READY, recording the no-major+ stop.
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, marked("major")),
        finding(2, "b.py", 2, marked("nit")),
    ]
    c = ctx(
        reviews,
        findings=findings,
        head="c2",
        checks=_GREEN,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.breaker == NO_MAJOR_FINDING


def test_no_major_stop_with_open_threads_addresses_without_minting_a_round():
    # The round's minor/nit threads still resolve before Ready — they just
    # never buy the reviewers another round: ADDRESSING with the stop named,
    # and nothing in `to_request`.
    reviews = [review(1, "c1")]
    findings = [finding(1, "a.py", 1, marked("nit"))]
    c = ctx(
        reviews,
        findings=findings,
        threads=[open_copilot_thread(body=marked("nit", "trailing whitespace"))],
        head="c1",
        checks=_GREEN,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    assert status.breaker == NO_MAJOR_FINDING
    assert status.to_request == []
    assert "review loop stopped" in status.next_action


def test_stop_does_not_override_a_real_ci_failure():
    # 6 rounds reached, threads resolved, but CI is failing: the real blocker
    # wins, not READY.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
    c = ctx(
        reviews,
        findings=findings,
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
        head="c6",
        checks=_GREEN,
        merge_state="DIRTY",
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.BLOCKED
    assert "conflict" in status.next_action


def test_converged_pr_not_stopped_under_cap():
    # 4 rounds of major findings, every thread resolved + green + mergeable ->
    # READY (normal path, no stop fired: the majors were addressed, the
    # reviewer settled, nothing holds).
    reviews = [review(i, f"c{i}") for i in range(1, 5)]
    findings = [finding(i, f"f{i}.py", i, marked("major")) for i in range(1, 5)]
    status = evaluate(
        ctx(
            reviews,
            findings=findings,
            threads=[],
            head="c4",
            checks=_GREEN,
        ),
        required=_COPILOT_ONLY,
    )
    assert status.state is TaskState.READY
    assert status.cycles == 4
    assert status.breaker is None


# --- no re-request after a fired breaker (ADR-0044) --------------------------


def test_no_major_stop_suppresses_the_stale_review_rerequest():
    # The convergence payoff: the shepherd fixed the round's nits and pushed.
    # The push staled rerun=True Copilot's review — but the no-major+ stop
    # means NO re-request: the stale holder is dropped, nothing is advised,
    # and the otherwise-ready PR routes READY with the breaker recorded. The
    # loop terminates by simply not asking again.
    findings = [
        finding(1, "a.py", 1, marked("minor")),
        finding(1, "b.py", 2, marked("nit")),
    ]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        head="c2",  # the nit-fix push
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
    )
    status = evaluate(c)
    assert status.state is TaskState.READY
    assert status.breaker == NO_MAJOR_FINDING
    assert status.to_request == []
    assert "RE-REQUEST" not in status.next_action


def test_round_cap_stop_also_suppresses_the_rerequest():
    # "A fired breaker still suppresses all re-requests" — the cap included:
    # at the cap there is no further round, so the push after round 6 must not
    # stale rerun=True Copilot back into the loop.
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    c = ctx(
        reviews,
        findings=findings,
        head="c7",  # pushed past the last reviewed head
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
    )
    status = evaluate(c)
    assert status.breaker == "round-cap"
    assert status.to_request == []
    assert status.state is TaskState.READY


def test_major_round_still_rerequests_per_reviewer_policy():
    # The counter-case: a round with any major+ finding runs the normal cycle —
    # the push staled rerun=True Copilot's review, so the engine advises
    # RE-REQUEST and routes it via the structured `to_request`.
    findings = [
        finding(1, "a.py", 1, marked("major")),
        finding(1, "b.py", 2, marked("nit")),
    ]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        head="c2",
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
    )
    status = evaluate(c)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST" in status.next_action


def test_breaker_cannot_waive_a_review_that_never_happened():
    # A genuinely never-requested required reviewer still holds after a stop:
    # the breaker suppresses RE-requests (stale-after-push), never the first
    # request. The dual-required config makes coderabbit never-requested while
    # copilot's minor-only round fires the stop.
    both = [by_name("copilot"), by_name("coderabbit")]
    findings = [finding(1, "a.py", 1, marked("minor"))]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        head="c1",
        checks=_GREEN,
    )
    status = evaluate(c, required=both)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["coderabbit"]


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


def test_no_major_stop_logs_breaker_fired(caplog):
    import logging

    from shipit import events

    # The no-major+ stop is a dev-cycle milestone too: `breaker.fired` with
    # the breaker name flat on the record.
    sightings = events.Sightings()
    findings = [finding(1, "a.py", 1, marked("minor"))]
    c = ctx(
        [review(1, "c1")],
        findings=findings,
        checks=_GREEN,
        sightings=sightings,
    )
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(c, required=_COPILOT_ONLY)
    assert status.breaker == NO_MAJOR_FINDING
    (fired,) = _events_named(caplog, "breaker.fired")
    assert fired.breaker == NO_MAJOR_FINDING


def test_no_breaker_means_no_breaker_event(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(ctx([review(10, "a")]))
    assert status.breaker is None
    assert not _events_named(caplog, "breaker.fired")
