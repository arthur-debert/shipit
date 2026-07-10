"""Review-round record: pure build, disposition seam, store write, service tee.

The record is the persisted product of one review round (RVW02-WS03): findings
WITH dispositions (routed-out ones included — the Opportunity-harvest seam),
the coverage attestation, the range reviewed, the invocation, and the
review-instructions **Variant** — appended to the review-rounds kind of the ONE
harness store family, never a repo file. The service tee is fail-open: a record
miss never degrades the review; the posting path is untouched.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from shipit.finding import Disposition, Finding, JudgedFinding, Severity
from shipit.harness.eval import store
from shipit.identity import repo_from_slug
from shipit.review import roundrecord

_REVIEW = {
    "summary": {
        "status": "REQUEST_CHANGES",
        "overall_feedback": "one blocker",
        "coverage": {
            "reviewed": ["src/a.py"],
            "skipped": [{"file": "src/b.py", "reason": "generated"}],
        },
    },
    "comments": [
        {
            "file": "src/a.py",
            "line": 3,
            "text": "off-by-one",
            "severity": "major",
            "category": "correctness",
            "confidence": 0.9,
            "evidence": "for i in range(n-1):",
            "fix": "range(n)",
        }
    ],
}


def _build(**overrides):
    kwargs = dict(
        review=_REVIEW,
        findings=roundrecord.dispositioned(_REVIEW),
        repo="acme/widget",
        pr=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=None,
        variant={"content_hash": "sha256:abc", "label": None},
        timestamp="2026-07-09T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return roundrecord.build(**kwargs)


def test_record_carries_product_range_invocation_and_round_trips_json():
    record = _build(duration_ms=1234)
    assert record["round.schema_version"] == roundrecord.SCHEMA_VERSION
    assert record["round.repo"] == "acme/widget"
    assert record["round.pr"] == 7
    assert record["round.range"] == {"base": "a" * 40, "head": "b" * 40}
    assert record["round.reviewer"] == "codex"
    assert record["round.status"] == "REQUEST_CHANGES"
    # The coverage attestation rides verbatim — silence means "clean", not "skipped".
    assert record["round.coverage"]["skipped"][0]["reason"] == "generated"
    assert record["round.invocation"] == {
        "model": "pro",
        "timeout": "600s",
        "instructions_path": None,
    }
    assert record["round.variant"] == {"content_hash": "sha256:abc", "label": None}
    assert record["round.usage"] == {"duration_ms": 1234, "total_tokens": None}
    [finding] = record["round.findings"]
    assert finding["severity"] == "major"
    assert finding["disposition"] == "post"
    assert finding["confidence"] == 0.9
    # One JSONL line — the record must serialize as-is.
    assert json.loads(json.dumps(record)) == record


def test_offline_replay_round_has_no_pr():
    # A range replay touches no PR: the record says so honestly (`round.pr` None)
    # while still carrying the range it reviewed.
    record = _build(pr=None)
    assert record["round.pr"] is None
    assert record["round.range"] == {"base": "a" * 40, "head": "b" * 40}


def test_routed_out_findings_are_recorded_with_their_disposition():
    # The Opportunity-harvest seam: a dropped finding is RETAINED with its
    # disposition — the record is never just the posted subset.
    dropped = Finding(severity=Severity.MINOR, text="pre-existing", file="old.py")
    posted = Finding(severity=Severity.MAJOR, text="real", file="src/a.py", line=3)
    record = _build(
        findings=[
            JudgedFinding(posted, Disposition.POST),
            JudgedFinding(dropped, Disposition.OUT_OF_SCOPE),
        ]
    )
    dispositions = {f["text"]: f["disposition"] for f in record["round.findings"]}
    assert dispositions == {"real": "post", "pre-existing": "out-of-scope"}


def test_contributing_runs_ride_verbatim_as_the_ws04_seam():
    # `round.runs` carries the run ids + variant hashes of every contributing
    # run — the join key `shipit eval report` resolves eval records by. Today's
    # single-pass producer contributes none; the shape is the WS04 seam.
    runs = [{"run_id": "agent-a7c77e10", "variant": {"content_hash": "sha256:x"}}]
    assert _build(runs=runs)["round.runs"] == runs
    assert _build()["round.runs"] == []


def test_dispositioned_maps_every_comment_to_post_via_the_trust_boundary():
    # Pre-calibrator, the whole judged output reaches the PR — every finding is
    # `post`, coerced through the SAME trust boundary the posting path uses (a
    # malformed severity lands on the `major` fail-safe, not a crash).
    review = {
        "comments": [
            {"file": "a.py", "line": 1, "text": "x", "severity": "nonsense"},
            "not-a-dict",
        ]
    }
    [judged] = roundrecord.dispositioned(review)
    assert judged.disposition is Disposition.POST
    assert judged.duplicate_of is None
    assert judged.finding.severity is Severity.MAJOR


def test_malformed_summary_never_crashes_the_build():
    # The agy path is schema-unenforced: a non-dict summary/coverage degrades to
    # None fields, never an exception at the record seam.
    record = _build(review={"summary": "not-a-dict", "comments": []})
    assert record["round.status"] is None
    assert record["round.coverage"] is None


def test_record_round_appends_to_the_review_rounds_store(tmp_path, monkeypatch):
    # The boundary: timestamps, hashes the review INSTRUCTIONS as the round's
    # variant (the experiment-arm handle), and appends to the review-rounds KIND
    # of the one store family — repo-keyed, outside any repo tree.
    monkeypatch.setenv("SHIPIT_EVAL_VARIANT_LABEL", "arm-a")
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("review carefully", encoding="utf-8")
    path = roundrecord.record_round(
        _REVIEW,
        repo_slug="acme/widget",
        pr=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=str(instructions),
        duration_ms=99,
        base_dir=tmp_path / "state",
    )
    assert path == store.store_path(
        repo_from_slug("acme/widget"),
        tmp_path / "state",
        kind=store.REVIEW_ROUNDS_KIND,
    )
    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.pr"] == 7
    assert record["round.usage"]["duration_ms"] == 99
    # The variant is the instructions content-hash + the env label: identical
    # instructions pool across PRs; an edited prompt separates arms.
    assert record["round.variant"]["content_hash"].startswith("sha256:")
    assert record["round.variant"]["label"] == "arm-a"
    assert record["round.timestamp"]  # stamped at the boundary


def _write_round(tmp_path, *, pr, reviewer, head, base="0" * 40):
    return roundrecord.record_round(
        _REVIEW,
        repo_slug="acme/widget",
        pr=pr,
        base_sha=base,
        head_sha=head,
        reviewer=reviewer,
        model="pro",
        timeout="600s",
        instructions_path=None,
        base_dir=tmp_path / "state",
    )


def test_last_reviewed_head_returns_the_most_recent_differing_head(tmp_path):
    # The incremental round's fix-range BASE (RVW02-WS06): the head this reviewer
    # most recently reviewed on this PR, other than the head now being reviewed.
    _write_round(tmp_path, pr=7, reviewer="codex", head="a" * 40)
    _write_round(tmp_path, pr=7, reviewer="codex", head="b" * 40)
    got = roundrecord.last_reviewed_head(
        repo_slug="acme/widget",
        pr=7,
        reviewer="codex",
        new_head="c" * 40,
        base_dir=tmp_path / "state",
    )
    assert got == "b" * 40  # the most recent, append order = chronological


def test_last_reviewed_head_scopes_to_pr_and_reviewer_and_excludes_the_new_head(
    tmp_path,
):
    _write_round(tmp_path, pr=7, reviewer="codex", head="a" * 40)
    _write_round(tmp_path, pr=7, reviewer="agy", head="d" * 40)  # other reviewer
    _write_round(tmp_path, pr=9, reviewer="codex", head="e" * 40)  # other PR
    _write_round(tmp_path, pr=7, reviewer="codex", head="c" * 40)  # == new_head
    got = roundrecord.last_reviewed_head(
        repo_slug="acme/widget",
        pr=7,
        reviewer="codex",
        new_head="c" * 40,
        base_dir=tmp_path / "state",
    )
    # Only codex@pr7 with head != new_head qualifies → the "a" round.
    assert got == "a" * 40


def test_last_reviewed_head_none_when_no_prior_round(tmp_path):
    # No prior differing-head record → None → the caller plans a full round 1
    # (fail toward over-reviewing). An offline replay (round.pr is None) never
    # matches a real PR number, so it can't be mistaken for a prior round.
    _write_round(tmp_path, pr=None, reviewer="codex", head="a" * 40)
    got = roundrecord.last_reviewed_head(
        repo_slug="acme/widget",
        pr=7,
        reviewer="codex",
        new_head="c" * 40,
        base_dir=tmp_path / "state",
    )
    assert got is None


def test_same_instructions_pool_and_edited_instructions_separate(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")
    edited = tmp_path / "c.txt"
    edited.write_text("different", encoding="utf-8")

    def _variant(path):
        record_path = roundrecord.record_round(
            {"summary": {"status": "COMMENT"}, "comments": []},
            repo_slug="acme/widget",
            pr=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            reviewer="agy",
            model="pro",
            timeout="600s",
            instructions_path=str(path),
            base_dir=tmp_path / "state",
            env={},
        )
        return json.loads(record_path.read_text().splitlines()[-1])["round.variant"]

    assert _variant(a) == _variant(b)
    assert _variant(a) != _variant(edited)


# --- the service tee (generate time, fail-open, posting path untouched) --------


def _tee_ctx(repo="acme/widget"):
    return SimpleNamespace(
        repo=repo,
        number=5,
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="",
        workdir="/tmp/wd",
        head_ref="branch",
    )


def test_generate_review_tees_a_round_record(monkeypatch, tmp_path):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(review), findings=(), runs=()
        ),
    )
    written = []
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: written.append((r, kw)) or tmp_path / "store.jsonl",
    )
    result = service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert result == review
    [(teed, kwargs)] = written
    assert teed == review
    assert kwargs["repo_slug"] == "acme/widget"
    assert kwargs["pr"] == 5
    assert kwargs["base_sha"] == "a" * 40
    assert kwargs["head_sha"] == "b" * 40
    assert kwargs["reviewer"] == "codex"
    assert kwargs["duration_ms"] >= 0


def test_generate_review_incremental_rescopes_and_records_the_fix_range(
    monkeypatch, tmp_path
):
    # RVW02-WS06 wiring: given an incremental plan, generate_review re-diffs ctx
    # to the fix range, runs the fan-out in incremental mode, and the tee records
    # the fix range (base = last-reviewed head, head = new head) like round 1.
    from shipit.agent import backend as agent_backend
    from shipit.identity import Sha
    from shipit.review import service
    from shipit.review.rounds import RoundPlan

    monkeypatch.setattr(
        service.rounds,
        "plan_for_view",
        lambda c, reviewer, **kw: RoundPlan(
            incremental=True, base=Sha("d" * 40), head=Sha("b" * 40)
        ),
    )
    monkeypatch.setattr(
        service.diff,
        "rescoped_view",
        lambda view, base: SimpleNamespace(
            repo="acme/widget",
            number=5,
            base_sha=str(base),
            head_sha="b" * 40,
            diff="fix range",
            workdir="/tmp/wd",
            head_ref="branch",
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, c, **kw: (
            captured.update(incremental=kw.get("incremental"), base=c.base_sha)
            or service.fanout.FanoutOutcome(
                review={"summary": {"status": "COMMENT"}, "comments": []},
                findings=(),
                runs=(),
            )
        ),
    )
    written = []
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: written.append(kw) or tmp_path / "s",
    )
    service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert captured["incremental"] is True
    assert captured["base"] == "d" * 40  # the fan-out saw the fix-range base
    [kw] = written
    assert kw["base_sha"] == "d" * 40 and kw["head_sha"] == "b" * 40


def test_generate_review_force_push_fallback_keeps_full_range(monkeypatch, tmp_path):
    # RVW02-WS06 wiring: a rebase/force-push plan (incremental=False with a
    # fallback_reason) must NOT rescope — generate_review runs the fan-out in FULL
    # mode over the resolved view and the tee records the full base..head range,
    # exactly like round 1. The incremental test above only covers the incremental
    # plan at this seam; this pins the fallback branch here too, so a regression
    # that rescoped or narrowed a fallback round would fail.
    from shipit.agent import backend as agent_backend
    from shipit.identity import Sha
    from shipit.review import service
    from shipit.review.rounds import RoundPlan

    monkeypatch.setattr(
        service.rounds,
        "plan_for_view",
        lambda c, reviewer, **kw: RoundPlan(
            incremental=False,
            base=Sha("a" * 40),
            head=Sha("b" * 40),
            fallback_reason="last-reviewed head is not an ancestor (force-push)",
        ),
    )
    rescoped: list = []
    monkeypatch.setattr(
        service.diff,
        "rescoped_view",
        lambda view, base: rescoped.append(base) or view,
    )
    captured: dict = {}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, c, **kw: (
            captured.update(incremental=kw.get("incremental"), base=c.base_sha)
            or service.fanout.FanoutOutcome(
                review={"summary": {"status": "COMMENT"}, "comments": []},
                findings=(),
                runs=(),
            )
        ),
    )
    written = []
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: written.append(kw) or tmp_path / "s",
    )
    service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert rescoped == []  # a full/fallback round never rescopes the view
    assert captured["incremental"] is False  # the fan-out runs the FULL round
    assert captured["base"] == "a" * 40  # over the resolved view's full range
    [kw] = written
    assert kw["base_sha"] == "a" * 40 and kw["head_sha"] == "b" * 40


def test_tee_failure_is_fail_open_and_never_degrades_the_review(monkeypatch, caplog):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(review), findings=(), runs=()
        ),
    )

    def _boom(*a, **k):
        raise OSError("store unwritable")

    monkeypatch.setattr(service.roundrecord, "record_round", _boom)
    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        result = service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert result == review  # the review is unaffected
    assert any("review-round record" in r.getMessage() for r in caplog.records)


def test_tee_skips_cleanly_when_ctx_has_no_repo_identity(monkeypatch, caplog):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(review), findings=(), runs=()
        ),
    )
    called = []
    monkeypatch.setattr(
        service.roundrecord, "record_round", lambda *a, **k: called.append(1)
    )
    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        service.generate_review(agent_backend.CODEX, _tee_ctx(repo=None))
    assert called == []  # no record — and no crash — for a repo-less ctx


def test_record_round_persists_calibrator_findings_and_runs(tmp_path):
    # RVW02-WS04: the PR path passes the Calibrator's REAL routing plus the
    # contributing runs (every dimension pass + the calibrator, run ids +
    # variant hashes) — the record retains routed-out findings, never just the
    # posted subset, and `round.runs` is the eval-report join surface.
    findings = [
        JudgedFinding(
            Finding(severity=Severity.MAJOR, text="bug", file="a.py"), Disposition.POST
        ),
        JudgedFinding(
            Finding(severity=Severity.NIT, text="style", file="b.py"),
            Disposition.NIT_SUPPRESSED,
        ),
        # A merged-away duplicate: it carries its twin's `post` disposition but
        # its `duplicate_of` edge must persist so the report never counts it as
        # posted (RVW02-WS04 dedup edge).
        JudgedFinding(
            Finding(severity=Severity.MAJOR, text="bug-dup", file="c.py"),
            Disposition.POST,
            duplicate_of=0,
        ),
    ]
    runs = [
        {
            "run_id": "pass-1",
            "kind": "dimension-pass",
            "dimension": "correctness",
            "variant": {"content_hash": "sha256:p1", "label": None},
            "outcome": "success",
        },
        {
            "run_id": "cal-1",
            "kind": "calibrator",
            "backend": "claude",
            "reasoning": "high",
            "variant": {"content_hash": "sha256:c1", "label": None},
            "outcome": "success",
        },
    ]
    path = roundrecord.record_round(
        _REVIEW,
        repo_slug="acme/widget",
        pr=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=None,
        findings=findings,
        runs=runs,
        base_dir=tmp_path / "state",
    )
    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert [
        (f["text"], f["disposition"], f["duplicate_of"])
        for f in record["round.findings"]
    ] == [
        ("bug", "post", None),
        ("style", "nit-suppressed", None),
        ("bug-dup", "post", 0),
    ]
    assert [r["run_id"] for r in record["round.runs"]] == ["pass-1", "cal-1"]
    assert record["round.runs"][1]["kind"] == "calibrator"


def test_tee_forwards_the_fanout_findings_and_runs(monkeypatch, tmp_path):
    # The service tee hands the fan-out's routed findings + run trail through
    # to the record boundary verbatim — the record can never disagree with the
    # calibration that produced the posted review.
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    findings = (
        JudgedFinding(
            Finding(severity=Severity.MINOR, text="m"), Disposition.OUT_OF_SCOPE
        ),
    )
    runs = ({"run_id": "r1", "kind": "dimension-pass"},)
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(review), findings=findings, runs=runs
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: captured.update(kw) or tmp_path / "store.jsonl",
    )
    service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert captured["findings"] == findings
    assert captured["runs"] == runs
