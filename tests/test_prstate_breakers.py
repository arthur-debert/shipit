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


def marked(severity: str, text: str = "the claim") -> str:
    return f"<!-- shipit:finding severity={severity} -->\nnitpick: {text}"


UNMARKED = "capitalize the first word of the sentence for correct English grammar"


def sha(seed: str) -> Sha:
    return Sha(hashlib.sha1(seed.encode()).hexdigest())


def review(rid: int, head: str, author: str = "Copilot") -> Review:
    return Review(
        review_id=rid, author=author, state="COMMENTED", commit_id=sha(head), body=""
    )


_FID = count(9000)


def finding(
    rid: int, path: str, line: int, body: str = UNMARKED, author: str = "Copilot"
) -> Thread:
    cid = next(_FID)
    comment = ReviewComment(
        comment_id=cid, path=path, line=line, body=body, author=author, review_id=rid
    )
    return Thread(thread_id=f"PRT_f{cid}", is_resolved=True, comments=(comment,))


def fid(thread: Thread) -> int:
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
        roster=roster if roster is not None else default_roster(),
        overrides=overrides,
        sightings=sightings,
    )


def open_copilot_thread(path="a.py", line=1, body="substantive open issue"):
    comment = ReviewComment(
        comment_id=1, path=path, line=line, body=body, author="Copilot"
    )
    return Thread(thread_id="PRT_1", is_resolved=False, comments=(comment,))


def test_build_rounds_one_per_copilot_review_chronological():
    reviews = [review(10, "a"), review(20, "b"), review(5, "c", author="gemini-bot")]
    rounds = build_rounds(ctx(reviews))
    assert [r.index for r in rounds] == [1, 2]
    assert [r.commit_id for r in rounds] == [
        sha("a"),
        sha("b"),
    ]


def test_build_rounds_matches_both_copilot_login_variants():
    reviews = [review(10, "a", author="copilot-pull-request-reviewer[bot]")]
    rounds = build_rounds(ctx(reviews, findings=[finding(10, "a.py", 1)]))
    assert len(rounds) == 1
    assert [f.body for f in rounds[0].findings] == [UNMARKED]


def test_build_rounds_findings_come_from_threads_even_when_resolved():
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [finding(1, "a.py", 1, "fix A"), finding(2, "b.py", 2, "fix B")]
    rounds = build_rounds(ctx(reviews, findings=findings))
    assert [f.body for f in rounds[0].findings] == ["fix A"]
    assert [f.body for f in rounds[1].findings] == ["fix B"]


def test_rounds_carry_finding_identity_for_the_override_key():
    f = finding(1, "a.py", 3, marked("minor"))
    rounds = build_rounds(ctx([review(1, "c1")], findings=[f]))
    (only,) = rounds[0].findings
    assert only.comment_id == fid(f)
    assert only.body == marked("minor")


def test_two_required_reviewers_across_two_heads_is_two_rounds_not_four():
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
    ]
    assert len(rounds) == 2
    v = evaluate_breakers(ctx(reviews))
    assert v.cycles == 2
    assert not v.stop


def test_a_round_unions_both_reviewers_findings_on_the_same_head():
    both = [by_name("copilot"), by_name("coderabbit")]
    reviews = [
        review(1, "h1", author="Copilot"),
        review(2, "h1", author="coderabbitai[bot]"),
    ]
    findings = [finding(1, "a.py", 1, "fix A"), finding(2, "b.py", 2, "fix B")]
    rounds = build_rounds(ctx(reviews, findings=findings), required=both)
    assert len(rounds) == 1
    assert {f.body for f in rounds[0].findings} == {"fix A", "fix B"}


def test_shipped_default_cap_is_six():
    assert ROUND_CAP == 6


def test_five_rounds_under_cap_no_stop():
    reviews = [review(i, f"c{i}") for i in range(1, 6)]
    findings = [finding(i, f"f{i}.py", i, marked("major")) for i in range(1, 6)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert not v.stop
    assert v.cycles == 5


def test_sixth_round_hits_the_cap():
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == "round-cap" and v.cycles == 6


def test_cap_fires_regardless_of_severity_state():
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i, marked("critical")) for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == "round-cap"


def test_configured_cap_of_two_fires_on_round_two():
    capped = replace(default_roster(), round_cap=2)
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [finding(1, "a.py", 1), finding(2, "b.py", 2)]
    v = evaluate_breakers(ctx(reviews, findings=findings, roster=capped))
    assert v.stop and v.breaker == "round-cap" and v.cycles == 2
    assert "cap of 2" in v.reason


def test_configured_cap_looser_than_default_defers_the_stop():
    capped = replace(default_roster(), round_cap=8)
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i, marked("major")) for i in range(1, 7)]
    v = evaluate_breakers(ctx(reviews, findings=findings, roster=capped))
    assert not v.stop
    assert v.cycles == 6


def test_minor_and_nit_round_stops():
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


def test_unmarked_copilot_round_fires_the_no_major_stop():
    findings = [
        finding(1, "a.py", 1, marked("nit")),
        finding(1, "b.py", 2, UNMARKED),
    ]
    c = ctx([review(1, "c1")], findings=findings)
    assert not has_blocking_finding(build_rounds(c)[0], {})
    v = evaluate_breakers(c)
    assert v.stop and v.breaker == NO_MAJOR_FINDING


def test_unclassified_finding_without_an_adapter_policy_keeps_the_loop_running():
    findings = [
        finding(1, "a.py", 1, marked("nit")),
        finding(1, "b.py", 2, UNMARKED, author="mystery-reviewer"),
    ]
    c = ctx([review(1, "c1")], findings=findings)
    assert has_blocking_finding(build_rounds(c)[0], {})
    assert not evaluate_breakers(c).stop


def test_an_override_still_upgrades_an_unclassified_copilot_finding():
    f = finding(1, "a.py", 1, UNMARKED)
    c = ctx(
        [review(1, "c1")],
        findings=[f],
        overrides={fid(f): Severity.MAJOR},
    )
    assert not evaluate_breakers(c).stop


def test_a_write_once_override_beats_the_marker_in_both_directions():
    f_major = finding(1, "a.py", 1, marked("major"))
    c = ctx(
        [review(1, "c1")],
        findings=[f_major],
        overrides={fid(f_major): Severity.NIT},
    )
    v = evaluate_breakers(c)
    assert v.stop and v.breaker == NO_MAJOR_FINDING
    f_nit = finding(1, "b.py", 2, marked("nit"))
    c = ctx(
        [review(1, "c1")],
        findings=[f_nit],
        overrides={fid(f_nit): Severity.MAJOR},
    )
    assert not evaluate_breakers(c).stop


def test_empty_round_does_not_fire_the_no_major_stop():
    v = evaluate_breakers(ctx([review(1, "c1")]))
    assert not v.stop


def test_the_verdict_machinery_is_gone():
    for name in ("is_all_nitpick_round", "unclassified_findings", "NITPICK"):
        assert not hasattr(breakers_module, name)


def test_no_major_latest_round_stops_early():
    reviews = [review(1, "c1"), review(2, "c2"), review(3, "c3")]
    findings = [
        finding(1, "a.py", 1, marked("major")),
        finding(2, "b.py", 2, marked("major")),
        finding(3, "c.py", 3, marked("minor")),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert v.stop and v.breaker == NO_MAJOR_FINDING and v.cycles == 3


def test_earlier_nit_round_does_not_stop_when_latest_is_major():
    reviews = [review(1, "c1"), review(2, "c2")]
    findings = [
        finding(1, "a.py", 1, marked("nit")),
        finding(2, "b.py", 2, marked("major")),
    ]
    v = evaluate_breakers(ctx(reviews, findings=findings))
    assert not v.stop


_COPILOT_ONLY = [by_name("copilot")]

_RERUN_COPILOT_ROSTER = Roster(
    (RosterEntry(name="copilot", required=True, rerun=True),)
)

_GREEN = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]


def test_open_thread_under_cap_routes_to_addressing():
    reviews = [review(i, f"c{i}") for i in range(1, 3)]
    findings = [
        finding(1, "a.py", 1, marked("major")),
        finding(2, "b.py", 2, marked("major")),
    ]
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


def test_unmarked_copilot_round_converges_to_ready_when_threads_resolve():
    reviews = [review(1, "c1")]
    findings = [finding(1, "a.py", 1, UNMARKED)]
    c = ctx(
        reviews,
        findings=findings,
        head="c1",
        checks=_GREEN,
    )
    status = evaluate(c, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.breaker == NO_MAJOR_FINDING


def test_stop_does_not_override_a_real_ci_failure():
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


def test_no_major_stop_suppresses_the_stale_review_rerequest():
    findings = [
        finding(1, "a.py", 1, marked("minor")),
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
    assert status.state is TaskState.READY
    assert status.breaker == NO_MAJOR_FINDING
    assert status.to_request == []
    assert "RE-REQUEST" not in status.next_action


def test_round_cap_stop_also_suppresses_the_rerequest():
    reviews = [review(i, f"c{i}") for i in range(1, 7)]
    findings = [finding(i, f"f{i}.py", i) for i in range(1, 7)]
    c = ctx(
        reviews,
        findings=findings,
        head="c7",
        checks=_GREEN,
        roster=_RERUN_COPILOT_ROSTER,
    )
    status = evaluate(c)
    assert status.breaker == "round-cap"
    assert status.to_request == []
    assert status.state is TaskState.READY


def test_major_round_still_rerequests_per_reviewer_policy():
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

    sightings = events.Sightings()
    reviews = [review(10, "a"), review(20, "b")]
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx(reviews, sightings=sightings))
    rounds = _events_named(caplog, "round.detected")
    assert [(r.round, r.commit) for r in rounds] == [
        (1, str(sha("a"))),
        (2, str(sha("b"))),
    ]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx(reviews, sightings=sightings))
    assert not _events_named(caplog, "round.detected")

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx([*reviews, review(30, "c")], sightings=sightings))
    (fresh,) = _events_named(caplog, "round.detected")
    assert (fresh.round, fresh.commit) == (3, str(sha("c")))


def test_evaluate_tags_breaker_fired_once(caplog):
    import logging

    from shipit import events

    sightings = events.Sightings()
    reviews = [review(10 * i, f"h{i}") for i in range(1, ROUND_CAP + 1)]
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        status = evaluate(ctx(reviews, sightings=sightings))
    assert status.breaker == "round-cap"
    (fired,) = _events_named(caplog, "breaker.fired")
    assert fired.breaker == "round-cap"
    assert fired.cycles == ROUND_CAP
    assert fired.pr == status.pr

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        evaluate(ctx(reviews, sightings=sightings))
    assert not _events_named(caplog, "breaker.fired")


def test_no_major_stop_logs_breaker_fired(caplog):
    import logging

    from shipit import events

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
