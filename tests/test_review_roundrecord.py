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
    assert json.loads(json.dumps(record)) == record


def test_offline_replay_round_has_no_pr():
    record = _build(pr=None)
    assert record["round.pr"] is None
    assert record["round.range"] == {"base": "a" * 40, "head": "b" * 40}


def test_routed_out_findings_are_recorded_with_their_disposition():
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
    runs = [{"run_id": "agent-a7c77e10", "variant": {"content_hash": "sha256:x"}}]
    assert _build(runs=runs)["round.runs"] == runs
    assert _build()["round.runs"] == []


def test_dispositioned_maps_every_comment_to_post_via_the_trust_boundary():
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
    record = _build(review={"summary": "not-a-dict", "comments": []})
    assert record["round.status"] is None
    assert record["round.coverage"] is None


def test_record_round_appends_to_the_review_rounds_store(tmp_path, monkeypatch):
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
    assert record["round.variant"]["content_hash"].startswith("sha256:")
    assert record["round.variant"]["label"] == "arm-a"
    assert record["round.timestamp"]


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
    _write_round(tmp_path, pr=7, reviewer="codex", head="a" * 40)
    _write_round(tmp_path, pr=7, reviewer="codex", head="b" * 40)
    got = roundrecord.last_reviewed_head(
        repo_slug="acme/widget",
        pr=7,
        reviewer="codex",
        new_head="c" * 40,
        base_dir=tmp_path / "state",
    )
    assert got == "b" * 40


def test_last_reviewed_head_scopes_to_pr_and_reviewer_and_excludes_the_new_head(
    tmp_path,
):
    _write_round(tmp_path, pr=7, reviewer="codex", head="a" * 40)
    _write_round(tmp_path, pr=7, reviewer="agy", head="d" * 40)
    _write_round(tmp_path, pr=9, reviewer="codex", head="e" * 40)
    _write_round(tmp_path, pr=7, reviewer="codex", head="c" * 40)
    got = roundrecord.last_reviewed_head(
        repo_slug="acme/widget",
        pr=7,
        reviewer="codex",
        new_head="c" * 40,
        base_dir=tmp_path / "state",
    )
    assert got == "a" * 40


def test_last_reviewed_head_none_when_no_prior_round(tmp_path):
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


def test_fanout_round_variant_folds_the_dimension_set(tmp_path):
    from shipit.harness.eval.variant import variant_of
    from shipit.review.dimensions import DEFAULT_DIMENSION_NAMES

    instructions = tmp_path / "i.txt"
    instructions.write_text("same instructions", encoding="utf-8")

    def _variant(**kw):
        record_path = roundrecord.record_round(
            {"summary": {"status": "COMMENT"}, "comments": []},
            repo_slug="acme/widget",
            pr=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            reviewer="codex",
            model="pro",
            timeout="600s",
            instructions_path=str(instructions),
            base_dir=tmp_path / "state",
            env={},
            **kw,
        )
        line = record_path.read_text().splitlines()[-1]
        return json.loads(line)["round.variant"]["content_hash"]

    plain = _variant()
    concern = _variant(dimension_names=DEFAULT_DIMENSION_NAMES)
    tiers = _variant(dimension_names=("sev-critical-high", "sev-medium", "sev-low"))
    overridden = _variant(
        dimension_names=DEFAULT_DIMENSION_NAMES,
        dimension_overrides={"correctness": {"model": "o3"}},
    )
    assert plain == variant_of("same instructions").content_hash
    assert len({plain, concern, tiers, overridden}) == 4


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
    assert kwargs["dimension_names"] is None


def test_generate_review_explicit_dimensions_fold_into_the_record(
    monkeypatch, tmp_path
):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    forwarded: dict = {}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: (
            forwarded.update(kw)
            or service.fanout.FanoutOutcome(review=dict(review), findings=(), runs=())
        ),
    )
    written = []
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: written.append((r, kw)) or tmp_path / "store.jsonl",
    )
    service.generate_review(
        agent_backend.CODEX,
        _tee_ctx(),
        dimensions=("test-quality", "correctness"),
    )
    assert forwarded["dimensions"] == ("test-quality", "correctness")
    [(_, kwargs)] = written
    assert kwargs["dimension_names"] == ("test-quality", "correctness")


def test_generate_review_incremental_rescopes_and_records_the_fix_range(
    monkeypatch, tmp_path
):
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
    assert captured["base"] == "d" * 40
    [kw] = written
    assert kw["base_sha"] == "d" * 40 and kw["head_sha"] == "b" * 40
    assert kw["dimension_names"] is None


def test_generate_review_force_push_fallback_keeps_full_range(monkeypatch, tmp_path):
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
    assert rescoped == []
    assert captured["incremental"] is False
    assert captured["base"] == "a" * 40
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
    assert result == review
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
    assert called == []


def test_record_round_persists_calibrator_findings_and_runs(tmp_path):
    findings = [
        JudgedFinding(
            Finding(severity=Severity.MAJOR, text="bug", file="a.py"), Disposition.POST
        ),
        JudgedFinding(
            Finding(severity=Severity.NIT, text="style", file="b.py"),
            Disposition.NIT_SUPPRESSED,
        ),
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


def test_record_round_threads_the_measured_round_token_total(tmp_path):
    runs = [
        {
            "run_id": "pass-1",
            "kind": "dimension-pass",
            "variant": {"content_hash": "sha256:p1", "label": None},
            "usage": {
                "total_tokens": 11943,
                "input_tokens": None,
                "output_tokens": None,
                "source": "codex-stderr",
            },
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
        runs=runs,
        duration_ms=2000,
        total_tokens=11943,
        base_dir=tmp_path / "state",
    )
    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.usage"] == {"duration_ms": 2000, "total_tokens": 11943}
    assert record["round.runs"][0]["usage"]["total_tokens"] == 11943
    assert record["round.runs"][0]["usage"]["source"] == "codex-stderr"


def test_tee_threads_the_fanout_round_token_total(monkeypatch, tmp_path):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(review), findings=(), runs=(), total_tokens=12000
        ),
    )
    written = []
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: written.append(kw) or tmp_path / "s",
    )
    service.generate_review(agent_backend.CODEX, _tee_ctx())
    [kw] = written
    assert kw["total_tokens"] == 12000


def test_build_carries_round_identity_artifacts_and_finding_run_ids():
    judged = [
        JudgedFinding(
            Finding(severity=Severity.MAJOR, text="bug", file="a.py"),
            Disposition.POST,
            run_id="pass-1",
        ),
        JudgedFinding(
            Finding(severity=Severity.NIT, text="style", file="b.py"),
            Disposition.NIT_SUPPRESSED,
        ),
    ]
    record = roundrecord.build(
        review=_REVIEW,
        findings=judged,
        repo="owner/repo",
        pr=7,
        base_sha="b" * 40,
        head_sha="h" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=None,
        variant=None,
        round_id="round-hex",
        artifacts_dir="/state/review-artifacts/owner/repo/round-hex",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert record["round.schema_version"] == 4
    assert record["round.id"] == "round-hex"
    assert record["round.artifacts"] == "/state/review-artifacts/owner/repo/round-hex"
    assert [f["run_id"] for f in record["round.findings"]] == ["pass-1", None]


def test_build_defaults_round_identity_to_none():
    record = roundrecord.build(
        review=_REVIEW,
        findings=[],
        repo="owner/repo",
        pr=None,
        base_sha="b" * 40,
        head_sha="h" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=None,
        variant=None,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert record["round.id"] is None
    assert record["round.artifacts"] is None


def test_dispositioned_stamps_the_single_pass_run_id():
    review = {
        "summary": {"status": "COMMENT"},
        "comments": [{"file": "a.py", "line": 1, "text": "bug", "severity": "major"}],
    }
    judged = roundrecord.dispositioned(review, run_id="range-run")
    assert [j.run_id for j in judged] == ["range-run"]
    assert [j.run_id for j in roundrecord.dispositioned(review)] == [None]


def test_record_round_threads_round_identity_to_the_store(tmp_path):
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("review carefully", encoding="utf-8")
    path = roundrecord.record_round(
        _REVIEW,
        repo_slug="owner/repo",
        pr=3,
        base_sha="b" * 40,
        head_sha="h" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=str(instructions),
        round_id="rid-1",
        artifacts_dir="/somewhere/rid-1",
        base_dir=tmp_path / "state",
    )
    record = json.loads(path.read_text().splitlines()[-1])
    assert record["round.id"] == "rid-1"
    assert record["round.artifacts"] == "/somewhere/rid-1"
