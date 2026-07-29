from __future__ import annotations

from types import SimpleNamespace

import pytest

from shipit.finding import Disposition, Finding, Severity
from shipit.identity import Sha
from shipit.review import fanout
from shipit.review.calibrator import (
    CalibratedFinding,
    CalibrationContractError,
    CalibrationResult,
    CalibratorConfig,
    CalibratorRun,
)
from shipit.review.dimensions import by_name
from shipit.review.producer import CapturedReview
from shipit.review.usage import UNREPORTED, TokenUsage

_CAL = CalibratorConfig()


def _ctx():
    return SimpleNamespace(
        number=5,
        repo="owner/repo",
        head_ref="feature/x",
        workdir="/checkout",
        diff="",
    )


def _incremental_ctx():
    return SimpleNamespace(
        number=5,
        repo="owner/repo",
        head_ref="feature/x",
        workdir="/checkout",
        diff="",
        base_sha=Sha("b" * 40),
        head_sha=Sha("c" * 40),
    )


def _pass_review(comments, reviewed=("a.py",), skipped=()):
    return {
        "summary": {
            "status": "COMMENT",
            "overall_feedback": "pass feedback",
            "coverage": {"reviewed": list(reviewed), "skipped": list(skipped)},
        },
        "comments": list(comments),
    }


def _comment(text, severity="minor", file="a.py", line=3):
    return {
        "file": file,
        "line": line,
        "text": text,
        "severity": severity,
        "category": "",
        "confidence": 0.9,
        "evidence": f"evidence for {text}",
        "fix": "",
    }


def _finding(severity=Severity.MINOR, text="t", file="a.py", evidence="e"):
    return Finding(severity=severity, text=text, file=file, evidence=evidence)


def _calibrated(i, finding, disposition=Disposition.POST, **kw):
    return CalibratedFinding(id=i, finding=finding, disposition=disposition, **kw)


@pytest.fixture(autouse=True)
def _tmp_state_root(monkeypatch, tmp_path):
    from shipit.harness.eval import store

    monkeypatch.setattr(
        store.platformdirs, "user_state_dir", lambda name: str(tmp_path / "state")
    )
    return tmp_path / "state"


@pytest.fixture
def _seams(monkeypatch):
    capture: dict = {
        "reviews": {},
        "union": None,
        "result": None,
        "trees": [],
        "preflights": [],
    }

    monkeypatch.setattr(
        fanout.producer,
        "preflight_round",
        lambda backends: capture["preflights"].append(list(backends)),
    )

    monkeypatch.setattr(
        fanout.producer,
        "provision_review_tree",
        lambda ctx, backend, *, naming=None: (
            capture["trees"].append("/tree") or "/tree"
        ),
    )

    def fake_pass_task_text(
        backend,
        number,
        *,
        instructions_path=None,
        dimension=None,
        incremental_range=None,
        diff=None,
    ):
        del diff
        if incremental_range is not None:
            return (
                f"incremental task for {incremental_range[0]}..{incremental_range[1]}"
            )
        if dimension is None:
            return "single full-scope task"
        return f"task for {dimension.name}"

    monkeypatch.setattr(fanout.producer, "pass_task_text", fake_pass_task_text)

    def fake_run_tree_review(backend, ctx, **kw):
        if kw.get("dimension") is not None:
            key = kw["dimension"].name
        elif kw.get("incremental_range") is not None:
            key = "incremental"
        else:
            key = "single"
        outcome = capture["reviews"][key]
        assert kw["tree_path"] == "/tree"
        if isinstance(outcome, Exception):
            raise outcome
        return CapturedReview(
            review=outcome,
            usage=capture.get("usage", {}).get(key, UNREPORTED),
            reasoning=kw.get("reasoning"),
        )

    monkeypatch.setattr(fanout.producer, "run_tree_review", fake_run_tree_review)

    def fake_run_calibrator(
        config,
        union,
        *,
        cwd,
        pr_number=None,
        commit_range=None,
        launcher=None,
        artifacts=None,
        correlation=None,
    ):
        capture["union"] = union
        capture["calibrator_target"] = {
            "pr_number": pr_number,
            "commit_range": commit_range,
        }
        capture["calibrator_artifacts"] = artifacts
        capture["calibrator_correlation"] = correlation
        assert cwd == "/tree"
        return CalibratorRun(
            result=capture["result"],
            run_id="cal-run-id",
            task="calibrator task",
            usage=capture.get("usage", {}).get("calibrator", UNREPORTED),
            reasoning=config.reasoning,
        )

    monkeypatch.setattr(fanout, "run_calibrator", fake_run_calibrator)
    return capture


def test_fanout_unions_passes_calibrates_and_posts(monkeypatch, _seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": _pass_review(
            [_comment("missing test", severity="minor", file="t.py")],
            reviewed=("t.py",),
        ),
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="the verdict",
        entries=(
            _calibrated(0, _finding(Severity.MAJOR, "bug")),
            _calibrated(
                1,
                _finding(Severity.MINOR, "missing test", file="t.py"),
                Disposition.OUT_OF_SCOPE,
            ),
        ),
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        calibrator=_CAL,
    )

    union = _seams["union"]
    assert [c["id"] for c in union] == [0, 1]
    assert union[0]["dimension"] == "correctness"
    assert union[0]["category"] == "correctness"
    assert union[1]["dimension"] == "test-quality"

    review = outcome.review
    assert [c["text"] for c in review["comments"]] == ["bug"]
    assert review["summary"]["status"] == "REQUEST_CHANGES"
    assert "the verdict" in review["summary"]["overall_feedback"]
    assert (
        "2 candidate finding(s) -> 1 posted" in (review["summary"]["overall_feedback"])
    )
    assert review["summary"]["coverage"]["reviewed"] == ["a.py", "t.py"]

    assert dict((j.finding.text, j.disposition) for j in outcome.findings) == {
        "bug": Disposition.POST,
        "missing test": Disposition.OUT_OF_SCOPE,
    }

    kinds = [run["kind"] for run in outcome.runs]
    assert kinds == ["dimension-pass", "dimension-pass", "calibrator"]
    assert all(run["run_id"] for run in outcome.runs)
    assert all(
        run["variant"]["content_hash"].startswith("sha256:") for run in outcome.runs
    )
    assert outcome.runs[0]["dimension"] == "correctness"
    assert outcome.runs[2]["run_id"] == "cal-run-id"
    assert outcome.runs[2]["reasoning"] == "high"


def test_merged_away_duplicate_rides_the_record_without_double_posting(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("same bug", severity="major")]),
        "test-quality": _pass_review(
            [_comment("same bug", severity="major", file="t.py")], reviewed=("t.py",)
        ),
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="one real bug, deduped",
        entries=(
            _calibrated(0, _finding(Severity.MAJOR, "same bug"), merged=(1,)),
            _calibrated(
                1, _finding(Severity.MAJOR, "same bug", file="t.py"), duplicate_of=0
            ),
        ),
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        calibrator=_CAL,
    )
    review = outcome.review
    assert [c["text"] for c in review["comments"]] == ["same bug"]
    assert "2 candidate finding(s) -> 1 posted" in review["summary"]["overall_feedback"]
    assert "1 duplicate)" in review["summary"]["overall_feedback"]
    judged = {j.finding.file: j for j in outcome.findings}
    assert judged["a.py"].duplicate_of is None and judged["a.py"].posted
    assert judged["t.py"].duplicate_of == 0 and not judged["t.py"].posted


def test_single_pass_failure_degrades_but_the_round_continues(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "security-robustness": RuntimeError("codex exited 1"),
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="",
        entries=(_calibrated(0, _finding(Severity.MAJOR, "bug")),),
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "security-robustness"],
        calibrator=_CAL,
    )
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]
    assert "DEGRADED COVERAGE" in outcome.review["summary"]["overall_feedback"]
    failed = [r for r in outcome.runs if r["kind"] == "dimension-pass"][1]
    assert failed["dimension"] == "security-robustness"
    assert failed["outcome"] == "failed"
    assert "codex exited 1" in failed["detail"]


def test_all_passes_failing_fails_the_round(_seams, _tmp_state_root, caplog):
    import json as _json
    import logging as _logging

    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": RuntimeError("boom a"),
        "test-quality": RuntimeError("boom b"),
    }
    caplog.set_level(_logging.INFO, logger="shipit.review")
    with pytest.raises(RuntimeError, match="all 2 dimension passes failed"):
        fanout.run_fanout_review(
            agent_backend.CODEX,
            _ctx(),
            dimensions=["correctness", "test-quality"],
        )
    repo_root = _tmp_state_root / "review-artifacts" / "owner" / "repo"
    metas = [_json.loads(p.read_text()) for p in repo_root.glob("*/*/meta.json")]
    assert len(metas) == 2
    assert {m["outcome"] for m in metas} == {"failed"}
    assert {m["error"] for m in metas} == {"boom a", "boom b"}
    names = _event_names(caplog)
    assert names.count("review.pass.launched") == 2
    assert names.count("review.pass.settled") == 2


def test_round_preflights_the_reviewer_backend_once_before_the_fanout(
    monkeypatch, _seams
):
    from shipit.agent import backend as agent_backend

    order: list[str] = []
    _seams["reviews"] = {"correctness": _pass_review([])}
    real_preflight = fanout.producer.preflight_round
    real_provision = fanout.producer.provision_review_tree
    monkeypatch.setattr(
        fanout.producer,
        "preflight_round",
        lambda backends: order.append("preflight") or real_preflight(backends),
    )
    monkeypatch.setattr(
        fanout.producer,
        "provision_review_tree",
        lambda ctx, backend, *, naming=None: (
            order.append("provision") or real_provision(ctx, backend, naming=naming)
        ),
    )
    fanout.run_fanout_review(agent_backend.CODEX, _ctx(), dimensions=["correctness"])
    assert order == ["preflight", "provision"]
    assert _seams["preflights"] == [[agent_backend.CODEX]]


def test_round_preflight_includes_the_calibrators_backend_when_the_judge_is_on(
    _seams,
):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"correctness": _pass_review([])}
    fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"], calibrator=_CAL
    )
    assert _seams["preflights"] == [
        [agent_backend.CODEX, agent_backend.by_name(_CAL.backend)]
    ]


def test_missing_binary_fails_the_round_before_any_pass_launches(monkeypatch, _seams):
    from shipit.agent import backend as agent_backend
    from shipit.review.backends import BackendUnavailable

    launched: list = []
    monkeypatch.setattr(
        fanout.producer,
        "run_tree_review",
        lambda *a, **k: launched.append(a) or _pass_review([]),
    )

    def missing(backends):
        raise BackendUnavailable("binary 'codex' not found — install/configure it")

    monkeypatch.setattr(fanout.producer, "preflight_round", missing)
    with pytest.raises(BackendUnavailable, match="install/configure"):
        fanout.run_fanout_review(agent_backend.CODEX, _ctx())
    assert launched == []
    assert _seams["trees"] == []


def test_calibrator_failure_propagates_and_no_union_is_posted(monkeypatch, _seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": _pass_review([]),
    }

    def boom(
        config,
        union,
        *,
        cwd,
        pr_number=None,
        commit_range=None,
        launcher=None,
        artifacts=None,
        correlation=None,
    ):
        raise CalibrationContractError("calibrator output missing candidate id 0")

    monkeypatch.setattr(fanout, "run_calibrator", boom)
    with pytest.raises(CalibrationContractError):
        fanout.run_fanout_review(
            agent_backend.CODEX,
            _ctx(),
            dimensions=["correctness", "test-quality"],
            calibrator=_CAL,
        )


def test_empty_union_skips_the_calibrator_and_posts_the_attested_clean_review(
    _seams,
):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([]),
        "test-quality": _pass_review([], reviewed=("t.py",)),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        calibrator=_CAL,
    )
    assert _seams["union"] is None
    review = outcome.review
    assert review["comments"] == []
    assert review["summary"]["status"] == "APPROVED"
    assert "no candidate findings" in review["summary"]["overall_feedback"]
    assert "after calibration" not in review["summary"]["overall_feedback"]
    assert review["summary"]["coverage"]["reviewed"] == ["a.py", "t.py"]
    assert outcome.findings == ()
    assert [r["kind"] for r in outcome.runs] == ["dimension-pass", "dimension-pass"]


def test_empty_union_with_a_failed_pass_never_reads_approved(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([]),
        "test-quality": RuntimeError("dead"),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
    )
    assert outcome.review["summary"]["status"] == "COMMENT"
    assert "DEGRADED COVERAGE" in outcome.review["summary"]["overall_feedback"]


def test_unknown_dimension_fails_loud():
    from shipit.agent import backend as agent_backend

    with pytest.raises(ValueError, match="unknown review dimension 'highs-only'"):
        fanout.run_fanout_review(agent_backend.CODEX, _ctx(), dimensions=["highs-only"])


def test_default_posts_deduped_union_with_pass_severities_no_calibrator(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("real bug", severity="major")]),
        "test-quality": _pass_review(
            [_comment("weak test", severity="nit", file="t.py")],
            reviewed=("t.py",),
        ),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
    )
    assert _seams["union"] is None

    review = outcome.review
    assert {c["text"]: c["severity"] for c in review["comments"]} == {
        "real bug": "major",
        "weak test": "nit",
    }
    assert review["summary"]["status"] == "REQUEST_CHANGES"
    assert "posted as the deduped union" in review["summary"]["overall_feedback"]
    assert "calibrator off" in review["summary"]["overall_feedback"]
    assert "after calibration" not in review["summary"]["overall_feedback"]

    assert [r["kind"] for r in outcome.runs] == ["dimension-pass", "dimension-pass"]
    assert all(j.posted and j.duplicate_of is None for j in outcome.findings)


def test_default_union_merges_same_location_same_claim_into_one_canonical(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review(
            [_comment("off-by-one here", severity="minor", file="a.py", line=42)]
        ),
        "security-robustness": _pass_review(
            [_comment("off-by-one here", severity="major", file="a.py", line=42)]
        ),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "security-robustness"],
    )
    review = outcome.review
    assert [(c["text"], c["severity"]) for c in review["comments"]] == [
        ("off-by-one here", "major")
    ]
    assert review["summary"]["status"] == "REQUEST_CHANGES"
    assert "1 duplicate)" in review["summary"]["overall_feedback"]
    dispositions = sorted((j.duplicate_of is None, j.posted) for j in outcome.findings)
    assert dispositions == [(False, False), (True, True)]


def test_default_union_keeps_distinct_claims_at_the_same_line(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review(
            [_comment("null deref", severity="major", file="a.py", line=7)]
        ),
        "test-quality": _pass_review(
            [_comment("unclear name", severity="nit", file="a.py", line=7)]
        ),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
    )
    assert {c["text"] for c in outcome.review["comments"]} == {
        "null deref",
        "unclear name",
    }


def test_default_union_applies_the_nit_cap(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review(
            [
                _comment("nit one", severity="nit", file="a.py", line=1),
                _comment("nit two", severity="nit", file="a.py", line=2),
            ]
        ),
        "test-quality": _pass_review([]),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        nit_cap=1,
    )
    assert [c["text"] for c in outcome.review["comments"]] == ["nit one"]
    suppressed = [
        j for j in outcome.findings if j.disposition is Disposition.NIT_SUPPRESSED
    ]
    assert [j.finding.text for j in suppressed] == ["nit two"]
    assert "1 nit-suppressed" in outcome.review["summary"]["overall_feedback"]


def _cand(i, text, severity="minor", file="a.py", line=3):
    return {
        "id": i,
        "dimension": "correctness",
        "file": file,
        "line": line,
        "severity": severity,
        "category": "correctness",
        "confidence": 0.9,
        "text": text,
        "evidence": f"evidence {i}",
        "fix": "",
    }


def test_dedup_union_merges_by_file_line_claim():
    union = [
        _cand(0, "same claim", severity="minor"),
        _cand(1, "same claim", severity="major"),
        _cand(2, "other claim", severity="nit"),
    ]
    entries = fanout.dedup_union(union)
    canonicals = [e for e in entries if e.duplicate_of is None]
    duplicates = [e for e in entries if e.duplicate_of is not None]
    assert {e.finding.text for e in canonicals} == {"same claim", "other claim"}
    merged_canonical = next(e for e in canonicals if e.finding.text == "same claim")
    assert merged_canonical.finding.severity is Severity.MAJOR
    assert merged_canonical.id == 1
    assert set(merged_canonical.merged) == {0}
    assert len(duplicates) == 1
    assert duplicates[0].id == 0
    assert duplicates[0].duplicate_of == 1
    assert duplicates[0].finding.severity is Severity.MAJOR
    assert duplicates[0].finding.text == "same claim"
    assert all(e.disposition is Disposition.POST for e in entries)


def test_dedup_union_ties_break_on_lowest_id():
    union = [
        _cand(0, "tie", severity="major"),
        _cand(1, "tie", severity="major"),
    ]
    entries = fanout.dedup_union(union)
    canonical = next(e for e in entries if e.duplicate_of is None)
    assert canonical.id == 0


def test_dedup_union_normalizes_claim_whitespace_and_case():
    union = [
        _cand(0, "Same  Claim\nhere"),
        _cand(1, "same claim here"),
    ]
    entries = fanout.dedup_union(union)
    assert sum(1 for e in entries if e.duplicate_of is None) == 1


def test_dedup_union_distinct_lines_are_distinct():
    union = [_cand(0, "claim", line=1), _cand(1, "claim", line=2)]
    entries = fanout.dedup_union(union)
    assert sum(1 for e in entries if e.duplicate_of is None) == 2


def test_dedup_union_canonical_precedes_its_duplicate_for_stable_routing():
    union = [_cand(0, "dup", severity="minor"), _cand(1, "dup", severity="minor")]
    entries = fanout.dedup_union(union)
    routed = fanout.route_calibrated(entries, nit_cap=None)
    posts = [e for e, d in routed if d is Disposition.POST and e.duplicate_of is None]
    assert len(posts) == 1


_ZERO_FILL_A = (
    "GPU readback failure zero-fills the comparison buffer, so the compare "
    "silently passes"
)
_ZERO_FILL_B = (
    "when GPU readback fails the comparison buffer stays zero-filled and the "
    "compare silently reports a pass"
)


def test_semantic_dedup_collapses_the_differently_worded_duplicate():
    union = [
        _cand(0, _ZERO_FILL_A, severity="major", file="src/bin/eval.rs", line=1299),
        _cand(1, _ZERO_FILL_B, severity="major", file="src/bin/eval.rs", line=1299),
    ]
    mechanical = fanout.dedup_union(union)
    assert sum(1 for e in mechanical if e.duplicate_of is None) == 2
    entries = fanout.dedup_union(union, semantic=True)
    canonicals = [e for e in entries if e.duplicate_of is None]
    assert len(canonicals) == 1
    assert len(entries) == 2


def test_semantic_dedup_keeps_distinct_defects_at_the_same_line():
    union = [
        _cand(0, "index off by one when the range is empty", line=7),
        _cand(1, "lock is never released on the early-return path", line=7),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    assert sum(1 for e in entries if e.duplicate_of is None) == 2


def test_semantic_dedup_never_merges_across_files():
    union = [
        _cand(0, "identical claim wording entirely", file="a.py", line=3),
        _cand(1, "identical claim wording entirely", file="b.py", line=3),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    assert sum(1 for e in entries if e.duplicate_of is None) == 2


def test_semantic_dedup_never_merges_file_less_candidates():
    union = [
        _cand(0, "alpha beta gamma delta wording", file="", line=None),
        _cand(1, "wording alpha beta gamma delta differs", file="", line=None),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    assert sum(1 for e in entries if e.duplicate_of is None) == 2


def test_semantic_dedup_never_merges_file_scoped_with_line_scoped():
    union = [
        _cand(0, _ZERO_FILL_A, file="src/bin/eval.rs", line=None),
        _cand(1, _ZERO_FILL_B, file="src/bin/eval.rs", line=1299),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    assert sum(1 for e in entries if e.duplicate_of is None) == 2


def test_semantic_dedup_zero_line_slack_keeps_adjacent_lines_separate():
    union = [
        _cand(0, _ZERO_FILL_A, line=1299),
        _cand(1, _ZERO_FILL_B, line=1300),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    assert sum(1 for e in entries if e.duplicate_of is None) == 2


def test_semantic_dedup_canonical_is_highest_severity_then_lowest_id():
    union = [
        _cand(0, _ZERO_FILL_A, severity="minor", line=1299),
        _cand(1, _ZERO_FILL_B, severity="major", line=1299),
        _cand(2, _ZERO_FILL_A + " here", severity="major", line=1299),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    canonical = next(e for e in entries if e.duplicate_of is None)
    assert canonical.id == 1
    assert canonical.finding.severity is Severity.MAJOR
    assert set(canonical.merged) == {0, 2}
    duplicates = [e for e in entries if e.duplicate_of is not None]
    assert {(e.id, e.duplicate_of) for e in duplicates} == {(0, 1), (2, 1)}
    assert all(e.finding.severity is Severity.MAJOR for e in duplicates)
    assert [e.duplicate_of is None for e in entries] == [True, False, False]


def test_semantic_dedup_never_chains_through_a_bridging_finding():
    union = [
        _cand(0, "alpha beta gamma delta", line=9),
        _cand(1, "alpha beta gamma delta epsilon zeta eta theta", line=9),
        _cand(2, "epsilon zeta eta theta", line=9),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    canonicals = [e for e in entries if e.duplicate_of is None]
    assert len(canonicals) == 2
    grouped = next(e for e in entries if e.duplicate_of is not None)
    assert (grouped.id, grouped.duplicate_of) == (1, 0)


def test_semantic_dedup_exact_restatement_follows_its_twin_into_the_group():
    union = [
        _cand(0, _ZERO_FILL_A, line=1299),
        _cand(1, _ZERO_FILL_B, line=1299),
        _cand(2, _ZERO_FILL_B, line=1299),
    ]
    entries = fanout.dedup_union(union, semantic=True)
    canonical = next(e for e in entries if e.duplicate_of is None)
    assert canonical.id == 0
    assert set(canonical.merged) == {1, 2}


def test_semantic_dedup_rides_the_off_path_and_attests_itself(_seams, caplog):
    import logging

    from shipit.agent import backend as agent_backend

    caplog.set_level(logging.INFO, logger="shipit.review")
    _seams["reviews"] = {
        "correctness": _pass_review(
            [_comment(_ZERO_FILL_A, severity="major", file="e.rs", line=1299)]
        ),
        "security-robustness": _pass_review(
            [_comment(_ZERO_FILL_B, severity="major", file="e.rs", line=1299)]
        ),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "security-robustness"],
        semantic_dedup=True,
    )
    assert _seams["union"] is None
    review = outcome.review
    assert len(review["comments"]) == 1
    assert review["comments"][0]["severity"] == "major"
    assert "semantically-deduped union" in review["summary"]["overall_feedback"]
    assert "1 duplicate)" in review["summary"]["overall_feedback"]
    canonical = next(j for j in outcome.findings if j.duplicate_of is None)
    duplicate = next(j for j in outcome.findings if j.duplicate_of is not None)
    assert canonical.posted and not duplicate.posted
    assert duplicate.duplicate_of == 0
    [completed] = [
        record
        for record in caplog.records
        if getattr(record, "_event", None) == "review.deduped"
    ]
    assert "semantic near-duplicate dedup completed" in completed.getMessage()
    assert "mechanical dedup" not in completed.getMessage()


def test_semantic_dedup_with_a_calibrator_is_a_loud_caller_error():
    with pytest.raises(ValueError, match="semantic_dedup"):
        fanout.run_fanout_review(
            object(),
            _ctx(),
            dimensions=["correctness"],
            calibrator=_CAL,
            semantic_dedup=True,
        )


def test_dry_run_semantic_notes_the_collapse_and_bills_nothing(monkeypatch, capsys):
    from shipit.agent import backend as agent_backend

    monkeypatch.setattr(
        fanout.producer, "run_tree_review", lambda backend, ctx, **kw: {}
    )
    fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness"],
        semantic_dedup=True,
        dry_run=True,
    )
    out = capsys.readouterr().out
    assert "semantic near-duplicate collapse" in out
    assert "would calibrate" not in out


def test_route_orders_by_severity_and_duplicates_never_post():
    entries = (
        _calibrated(0, _finding(Severity.NIT, "n")),
        _calibrated(1, _finding(Severity.CRITICAL, "c")),
        _calibrated(2, _finding(Severity.CRITICAL, "c-dup"), duplicate_of=1),
    )
    routed = fanout.route_calibrated(entries, nit_cap=None)
    assert [e.finding.text for e, _ in routed] == ["c", "c-dup", "n"]
    posts = [
        e.finding.text
        for e, d in routed
        if d is Disposition.POST and e.duplicate_of is None
    ]
    assert posts == ["c", "n"]
    assert routed[1][1] is Disposition.POST


def test_nit_cap_flips_over_cap_nits_to_suppressed():
    entries = (
        _calibrated(0, _finding(Severity.NIT, "n1")),
        _calibrated(1, _finding(Severity.NIT, "n2")),
        _calibrated(2, _finding(Severity.MINOR, "m")),
    )
    routed = fanout.route_calibrated(entries, nit_cap=1)
    by_text = {e.finding.text: d for e, d in routed}
    assert by_text["m"] is Disposition.POST
    assert by_text["n1"] is Disposition.POST
    assert by_text["n2"] is Disposition.NIT_SUPPRESSED


def test_nit_cap_zero_floors_the_posted_review_at_minor():
    entries = (
        _calibrated(0, _finding(Severity.NIT, "n1")),
        _calibrated(1, _finding(Severity.MINOR, "m")),
    )
    routed = fanout.route_calibrated(entries, nit_cap=0)
    by_text = {e.finding.text: d for e, d in routed}
    assert by_text["n1"] is Disposition.NIT_SUPPRESSED
    assert by_text["m"] is Disposition.POST


def test_nit_cap_never_resurrects_a_routed_out_nit():
    entries = (
        _calibrated(0, _finding(Severity.NIT, "dropped"), Disposition.DROP_UNVERIFIED),
        _calibrated(1, _finding(Severity.NIT, "kept")),
    )
    routed = fanout.route_calibrated(entries, nit_cap=1)
    by_text = {e.finding.text: d for e, d in routed}
    assert by_text["dropped"] is Disposition.DROP_UNVERIFIED
    assert by_text["kept"] is Disposition.POST


def test_nit_cap_suppression_propagates_to_merged_away_duplicates():
    entries = (
        _calibrated(0, _finding(Severity.NIT, "nit")),
        _calibrated(1, _finding(Severity.NIT, "nit-dup"), duplicate_of=0),
    )
    routed = fanout.route_calibrated(entries, nit_cap=0)
    by_text = {e.finding.text: d for e, d in routed}
    assert by_text["nit"] is Disposition.NIT_SUPPRESSED
    assert by_text["nit-dup"] is Disposition.NIT_SUPPRESSED


def test_dry_run_default_notes_the_deduped_union_and_bills_nothing(monkeypatch, capsys):
    from shipit.agent import backend as agent_backend

    printed: list = []
    monkeypatch.setattr(
        fanout.producer,
        "run_tree_review",
        lambda backend, ctx, **kw: printed.append(kw["dimension"].name) or {},
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        dry_run=True,
    )
    assert printed == ["correctness", "test-quality"]
    out = capsys.readouterr().out
    assert "calibrator OFF" in out
    assert "mechanically-deduped union" in out
    assert "would calibrate" not in out
    assert outcome.review["comments"] == []
    assert outcome.runs == ()


def test_dry_run_with_calibrator_on_notes_the_judge_and_bills_nothing(
    monkeypatch, capsys
):
    from shipit.agent import backend as agent_backend

    monkeypatch.setattr(
        fanout.producer,
        "run_tree_review",
        lambda backend, ctx, **kw: {},
    )
    fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        calibrator=_CAL,
        dry_run=True,
    )
    assert "would calibrate the union" in capsys.readouterr().out


def test_default_round1_shape_is_one_monolithic_single_pass(_seams, monkeypatch):
    from shipit.agent import backend as agent_backend

    seen_dimensions = []
    real_task_text = fanout.producer.pass_task_text

    def spying_task_text(backend, number, **kw):
        seen_dimensions.append(kw.get("dimension"))
        return real_task_text(backend, number, **kw)

    monkeypatch.setattr(fanout.producer, "pass_task_text", spying_task_text)
    _seams["reviews"] = {"single": _pass_review([_comment("bug", severity="major")])}
    outcome = fanout.run_fanout_review(agent_backend.CODEX, _ctx())
    assert [r["dimension"] for r in outcome.runs] == ["single"]
    assert [r["kind"] for r in outcome.runs] == ["single-pass"]
    assert seen_dimensions == [None]
    assert (
        "Review: one full-scope pass" in (outcome.review["summary"]["overall_feedback"])
    )
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]


def test_explicit_dimensions_config_still_routes_to_the_fanout(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("logic bug", severity="major")]),
        "test-quality": _pass_review([_comment("missing test", severity="minor")]),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness", "test-quality"]
    )
    assert [r["dimension"] for r in outcome.runs] == ["correctness", "test-quality"]
    assert all(r["kind"] == "dimension-pass" for r in outcome.runs)
    assert (
        "Review fan-out: 2 dimension pass(es)"
        in (outcome.review["summary"]["overall_feedback"])
    )
    assert [c["text"] for c in outcome.review["comments"]] == [
        "logic bug",
        "missing test",
    ]


def test_invocation_overrides_without_explicit_dimensions_fail_loud(_seams):
    from shipit.agent import backend as agent_backend

    with pytest.raises(ValueError, match="explicit `dimensions` fan-out"):
        fanout.run_fanout_review(
            agent_backend.CODEX,
            _ctx(),
            invocation_overrides={"correctness": {"model": "flash"}},
        )


def test_by_name_is_the_prompt_slice_the_passes_launch_with():
    from shipit.agent import backend as agent_backend
    from shipit.review.producer import pass_task_text

    task = pass_task_text(
        agent_backend.CODEX, 5, dimension=by_name("cross-file-invariants")
    )
    assert "DIMENSION FOCUS — Cross-file invariants" in task
    assert "READ BEYOND THE DIFF" in task
    assert "INTRODUCED or EXPOSED" in task
    assert "Your stated severity is the posted severity" in task


def test_incremental_round_runs_one_pass_suppresses_nits_records_range(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "incremental": _pass_review(
            [
                _comment("real bug", severity="major"),
                _comment("style", severity="nit", file="a.py", line=9),
            ]
        )
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _incremental_ctx(), incremental=True
    )

    assert [run["kind"] for run in outcome.runs] == ["incremental-pass"]
    run = outcome.runs[0]
    assert run["reasoning"] == fanout.DEFAULT_INCREMENTAL_REASONING
    assert run["range"] == {"base": "b" * 40, "head": "c" * 40}
    assert run["dimension"] == "incremental"

    review = outcome.review
    assert [c["text"] for c in review["comments"]] == ["real bug"]
    suppressed = {
        j.finding.text: j.disposition for j in outcome.findings if not j.posted
    }
    assert suppressed == {"style": Disposition.NIT_SUPPRESSED}


def test_incremental_round_still_runs_the_calibrator_when_configured(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "incremental": _pass_review([_comment("bug", severity="major")])
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="v", entries=(_calibrated(0, _finding(Severity.MAJOR, "bug")),)
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _incremental_ctx(), incremental=True, calibrator=_CAL
    )
    assert _seams["union"] is not None
    kinds = [run["kind"] for run in outcome.runs]
    assert kinds == ["incremental-pass", "calibrator"]
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]


def test_incremental_pass_failure_fails_the_round(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"incremental": RuntimeError("backend blew up")}
    with pytest.raises(RuntimeError, match="the incremental pass failed"):
        fanout.run_fanout_review(
            agent_backend.CODEX, _incremental_ctx(), incremental=True
        )


def test_single_pass_failure_fails_the_round(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"single": RuntimeError("backend blew up")}
    with pytest.raises(RuntimeError, match="the single review pass failed"):
        fanout.run_fanout_review(agent_backend.CODEX, _ctx())


def test_per_run_usage_is_stamped_and_the_round_total_sums_reported_runs(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": _pass_review([]),
    }
    _seams["usage"] = {
        "correctness": TokenUsage(total_tokens=11943, source="codex-stderr"),
        "test-quality": TokenUsage(total_tokens=57, source="codex-stderr"),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness", "test-quality"]
    )
    by_dim = {run["dimension"]: run for run in outcome.runs}
    assert by_dim["correctness"]["usage"] == {
        "total_tokens": 11943,
        "input_tokens": None,
        "output_tokens": None,
        "source": "codex-stderr",
    }
    assert outcome.total_tokens == 11943 + 57


def test_unreported_usage_round_totals_none_not_zero(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([]),
        "test-quality": _pass_review([]),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness", "test-quality"]
    )
    assert all(
        run["usage"]
        == {
            "total_tokens": None,
            "input_tokens": None,
            "output_tokens": None,
            "source": "unreported",
        }
        for run in outcome.runs
    )
    assert outcome.total_tokens is None


def test_failed_pass_keeps_the_explicitly_unknown_usage(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": RuntimeError("backend blew up"),
    }
    _seams["usage"] = {
        "correctness": TokenUsage(total_tokens=500, source="codex-stderr"),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness", "test-quality"]
    )
    by_dim = {run["dimension"]: run for run in outcome.runs}
    assert by_dim["test-quality"]["usage"]["total_tokens"] is None
    assert by_dim["test-quality"]["usage"]["source"] == "unreported"
    assert outcome.total_tokens == 500


def test_calibrator_usage_and_applied_reasoning_ride_its_run_entry(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
    }
    _seams["usage"] = {
        "correctness": TokenUsage(total_tokens=100, source="codex-stderr"),
        "calibrator": TokenUsage(
            total_tokens=2000,
            source="claude-envelope",
            input_tokens=1900,
            output_tokens=100,
        ),
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="v",
        entries=(_calibrated(0, _finding(Severity.MAJOR, "bug")),),
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"], calibrator=_CAL
    )
    cal = outcome.runs[-1]
    assert cal["kind"] == "calibrator"
    assert cal["usage"]["total_tokens"] == 2000
    assert cal["usage"]["source"] == "claude-envelope"
    assert cal["reasoning"] == "high"
    assert outcome.total_tokens == 2100


def test_round1_passes_record_no_reasoning_when_none_was_applied(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"correctness": _pass_review([])}
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"]
    )
    assert "reasoning" not in outcome.runs[0]


def _range_view(workdir="/replay-checkout"):
    from shipit.identity import repo_from_slug
    from shipit.review.diff import RangeView

    return RangeView(
        repo=repo_from_slug("acme/widget"),
        base_sha=Sha("d" * 40),
        head_sha=Sha("e" * 40),
        diff="diff --git a/x b/x\n",
        changed_files=["x"],
        workdir=workdir,
    )


@pytest.fixture
def _range_seams(monkeypatch):
    capture: dict = {
        "reviews": {},
        "union": None,
        "result": None,
        "calls": [],
        "preflights": [],
    }

    def _boom(*args, **kwargs):
        raise AssertionError("a RangeView target must never touch a PR seam")

    monkeypatch.setattr(
        fanout.producer,
        "preflight_round",
        lambda backends: capture["preflights"].append(list(backends)),
    )

    monkeypatch.setattr(fanout.producer, "provision_review_tree", _boom)
    monkeypatch.setattr(fanout.producer, "run_tree_review", _boom)
    monkeypatch.setattr(fanout.producer, "pass_task_text", _boom)

    def fake_range_pass_task_text(
        backend, view, *, instructions_path=None, dimension=None
    ):
        return f"range task for {dimension.name}"

    monkeypatch.setattr(
        fanout.producer, "range_pass_task_text", fake_range_pass_task_text
    )

    def fake_run_range_review(backend, view, **kw):
        capture["calls"].append({"view": view, "dimension": kw.get("dimension")})
        dim = kw["dimension"]
        outcome = capture["reviews"][dim.name]
        if isinstance(outcome, Exception):
            raise outcome
        return CapturedReview(
            review=outcome,
            usage=capture.get("usage", {}).get(dim.name, UNREPORTED),
            reasoning=kw.get("reasoning"),
        )

    monkeypatch.setattr(fanout.producer, "run_range_review", fake_run_range_review)

    def fake_run_calibrator(
        config,
        union,
        *,
        cwd,
        pr_number=None,
        commit_range=None,
        launcher=None,
        artifacts=None,
        correlation=None,
    ):
        capture["union"] = union
        capture["calibrator_target"] = {
            "cwd": cwd,
            "pr_number": pr_number,
            "commit_range": commit_range,
        }
        return CalibratorRun(
            result=capture["result"],
            run_id="cal-run-id",
            task="calibrator task",
            usage=capture.get("usage", {}).get("calibrator", UNREPORTED),
            reasoning=config.reasoning,
        )

    monkeypatch.setattr(fanout, "run_calibrator", fake_run_calibrator)
    return capture


def test_range_target_fans_out_through_the_range_producer(_range_seams):
    from shipit.agent import backend as agent_backend

    _range_seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": _pass_review([]),
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _range_view(),
        dimensions=["correctness", "test-quality"],
    )
    assert sorted(c["dimension"].name for c in _range_seams["calls"]) == [
        "correctness",
        "test-quality",
    ]
    assert all(c["view"].workdir == "/replay-checkout" for c in _range_seams["calls"])
    assert _range_seams["preflights"] == [[agent_backend.CODEX]]
    assert [run["kind"] for run in outcome.runs] == ["dimension-pass"] * 2
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]
    assert outcome.review["summary"]["status"] == "REQUEST_CHANGES"


def test_range_target_calibrator_gets_the_range_ground_truth(_range_seams):
    from shipit.agent import backend as agent_backend

    _range_seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
    }
    _range_seams["result"] = CalibrationResult(
        overall_feedback="v", entries=(_calibrated(0, _finding(Severity.MAJOR, "bug")),)
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _range_view(),
        dimensions=["correctness"],
        calibrator=_CAL,
    )
    assert _range_seams["calibrator_target"] == {
        "cwd": "/replay-checkout",
        "pr_number": None,
        "commit_range": ("d" * 40, "e" * 40),
    }
    assert [run["kind"] for run in outcome.runs] == ["dimension-pass", "calibrator"]


def test_range_target_all_passes_failing_fails_the_round_with_the_range_label(
    _range_seams,
):
    from shipit.agent import backend as agent_backend

    _range_seams["reviews"] = {
        "correctness": RuntimeError("backend blew up"),
    }
    with pytest.raises(RuntimeError, match=f"range {'d' * 40}..{'e' * 40}"):
        fanout.run_fanout_review(
            agent_backend.CODEX, _range_view(), dimensions=["correctness"]
        )


def test_range_target_rejects_incremental_and_dry_run():
    from shipit.agent import backend as agent_backend

    with pytest.raises(ValueError, match="incremental"):
        fanout.run_fanout_review(agent_backend.CODEX, _range_view(), incremental=True)
    with pytest.raises(ValueError, match="dry_run"):
        fanout.run_fanout_review(agent_backend.CODEX, _range_view(), dry_run=True)


def _event_names(caplog):
    from shipit import events

    return [
        name
        for r in caplog.records
        if (name := getattr(r, events.EXTRA_KEY, None)) is not None
    ]


def test_fanout_persists_bundles_and_correlates_findings_to_passes(
    _seams, _tmp_state_root, caplog
):
    import json as _json
    import logging as _logging
    from pathlib import Path as _Path

    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": RuntimeError("child exploded"),
    }
    caplog.set_level(_logging.INFO, logger="shipit.review")
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
    )

    assert outcome.round_id
    assert outcome.artifacts_dir == str(
        _tmp_state_root / "review-artifacts" / "owner" / "repo" / outcome.round_id
    )

    runs = {run["dimension"]: run for run in outcome.runs}
    ok, bad = runs["correctness"], runs["test-quality"]
    assert ok["artifacts"] == str(_Path(outcome.artifacts_dir) / ok["run_id"])
    assert bad["artifacts"] == str(_Path(outcome.artifacts_dir) / bad["run_id"])
    bad_meta = _json.loads((_Path(bad["artifacts"]) / "meta.json").read_text())
    assert bad_meta["run_id"] == bad["run_id"]
    assert bad_meta["round_id"] == outcome.round_id
    assert bad_meta["outcome"] == "failed"
    assert bad_meta["error"] == "child exploded"
    ok_meta = _json.loads((_Path(ok["artifacts"]) / "meta.json").read_text())
    assert ok_meta["outcome"] == "success" and ok_meta["findings"] == 1

    assert [j.run_id for j in outcome.findings] == [ok["run_id"]]

    names = _event_names(caplog)
    assert names.count("review.pass.launched") == 2
    assert names.count("review.pass.settled") == 2
    settled = [
        r for r in caplog.records if getattr(r, "_event", None) == "review.pass.settled"
    ]
    by_dim = {r.dimension: r for r in settled}
    assert by_dim["correctness"].outcome == "success"
    assert by_dim["test-quality"].outcome == "failed"
    assert by_dim["correctness"].run_id == ok["run_id"]
    assert by_dim["correctness"].round_id == outcome.round_id


def test_calibrator_gets_its_own_bundle_and_run_entry_artifacts(
    _seams, _tmp_state_root
):
    from pathlib import Path as _Path

    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")])
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="v",
        entries=(_calibrated(0, _finding(Severity.MAJOR, "bug")),),
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"], calibrator=_CAL
    )
    cal = next(run for run in outcome.runs if run["kind"] == "calibrator")
    assert cal["artifacts"] == str(_Path(outcome.artifacts_dir) / "calibrator")
    assert _seams["calibrator_artifacts"] is not None
    assert str(_seams["calibrator_artifacts"].dir) == cal["artifacts"]


def test_calibrator_progress_events_carry_the_stable_surrogate_run_id(
    _seams, _tmp_state_root, caplog
):
    import logging as _logging

    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review(
            [
                _comment("real bug", severity="major"),
                _comment("out of scope", severity="minor", line=9),
            ]
        )
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="v",
        entries=(
            _calibrated(0, _finding(Severity.MAJOR, "real bug")),
            _calibrated(
                1,
                _finding(Severity.MINOR, "out of scope", file="a.py"),
                Disposition.OUT_OF_SCOPE,
            ),
        ),
    )
    caplog.set_level(_logging.INFO, logger="shipit.review")
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"], calibrator=_CAL
    )

    corr = _seams["calibrator_correlation"]
    assert corr["run_id"] == "calibrator"
    assert corr["round_id"] == outcome.round_id
    assert corr["dimension"] == "calibrator"

    by_event: dict[str, list] = {}
    for r in caplog.records:
        name = getattr(r, "_event", None)
        if name is not None:
            by_event.setdefault(name, []).append(r)

    cal_launched = [
        r for r in by_event["review.pass.launched"] if r.dimension == "calibrator"
    ]
    cal_settled = [
        r for r in by_event["review.pass.settled"] if r.dimension == "calibrator"
    ]
    assert len(cal_launched) == 1 and len(cal_settled) == 1
    assert cal_launched[0].run_id == "calibrator"
    assert cal_settled[0].run_id == "calibrator"
    assert cal_settled[0].round_id == outcome.round_id

    [completed] = by_event["review.calibrated"]
    assert completed.round_id == outcome.round_id

    pass_run_id = next(
        run["run_id"] for run in outcome.runs if run["kind"] == "dimension-pass"
    )
    [disp] = by_event["finding.dispositioned"]
    assert disp.round_id == outcome.round_id
    assert disp.run_id == pass_run_id


def test_calibrator_failure_settled_event_carries_the_surrogate_run_id(
    monkeypatch, _seams, _tmp_state_root, caplog
):
    import logging as _logging

    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")])
    }

    def boom(
        config,
        union,
        *,
        cwd,
        pr_number=None,
        commit_range=None,
        launcher=None,
        artifacts=None,
        correlation=None,
    ):
        raise CalibrationContractError("calibrator output missing candidate id 0")

    monkeypatch.setattr(fanout, "run_calibrator", boom)
    caplog.set_level(_logging.INFO, logger="shipit.review")
    with pytest.raises(CalibrationContractError):
        fanout.run_fanout_review(
            agent_backend.CODEX, _ctx(), dimensions=["correctness"], calibrator=_CAL
        )
    settled = [
        r
        for r in caplog.records
        if getattr(r, "_event", None) == "review.pass.settled"
        and getattr(r, "dimension", None) == "calibrator"
    ]
    assert len(settled) == 1
    assert settled[0].run_id == "calibrator"
    assert settled[0].outcome == "failed"


def test_union_candidates_carry_the_pass_run_id(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")]),
        "test-quality": _pass_review(
            [_comment("gap", severity="minor", file="t.py")], reviewed=("t.py",)
        ),
    }
    _seams["result"] = CalibrationResult(
        overall_feedback="v",
        entries=(
            _calibrated(0, _finding(Severity.MAJOR, "bug")),
            _calibrated(1, _finding(Severity.MINOR, "gap", file="t.py")),
        ),
    )
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX,
        _ctx(),
        dimensions=["correctness", "test-quality"],
        calibrator=_CAL,
    )
    union = _seams["union"]
    runs = {run["dimension"]: run for run in outcome.runs if "dimension" in run}
    assert union[0]["run_id"] == runs["correctness"]["run_id"]
    assert union[1]["run_id"] == runs["test-quality"]["run_id"]
    by_text = {j.finding.text: j for j in outcome.findings}
    assert by_text["bug"].run_id == runs["correctness"]["run_id"]
    assert by_text["gap"].run_id == runs["test-quality"]["run_id"]


def test_bundles_fail_open_when_ctx_has_no_repo_identity(_seams, _tmp_state_root):
    from types import SimpleNamespace as _NS

    from shipit.agent import backend as agent_backend

    ctx = _NS(number=5, repo=None, head_ref="feature/x", workdir="/checkout", diff="")
    _seams["reviews"] = {
        "correctness": _pass_review([_comment("bug", severity="major")])
    }
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, ctx, dimensions=["correctness"]
    )
    assert outcome.artifacts_dir is None
    assert outcome.round_id
    assert all(run["artifacts"] is None for run in outcome.runs)
    assert [j.run_id for j in outcome.findings] == [outcome.runs[0]["run_id"]]
    assert not (_tmp_state_root / "review-artifacts").exists()
