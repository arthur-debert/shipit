"""Unit tests for `shipit.review.fanout` — round-1 dimension fan-out +
calibration (RVW02-WS04, ADR-0045).

The orchestration is pinned with the producer + calibrator seams FAKED (no
Tree, no model run, no gh): pass fan-out over one shared Tree, the union's
shape and trust-boundary coercion, pass-failure tolerance vs all-failed, the
empty-union short-circuit (no calibrator run), the deterministic routing
(duplicates never post, the nit cap, the derived status), the merged coverage
attestation, and the contributing-run trail (run ids + variant hashes) the
round record persists.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shipit.finding import Disposition, Finding, Severity
from shipit.review import fanout
from shipit.review.calibrator import CalibratedFinding, CalibrationResult
from shipit.review.dimensions import by_name


def _ctx():
    return SimpleNamespace(
        number=5,
        repo="owner/repo",
        head_ref="feature/x",
        workdir="/checkout",
        diff="",
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


@pytest.fixture
def _seams(monkeypatch):
    """Fake the producer + calibrator seams; returns the capture dict tests
    read (per-dimension reviews or exceptions in `reviews`, the union handed
    to the calibrator in `union`, the calibration result in `result`)."""
    capture: dict = {"reviews": {}, "union": None, "result": None, "trees": []}

    monkeypatch.setattr(
        fanout.producer,
        "provision_review_tree",
        lambda ctx: capture["trees"].append("/tree") or "/tree",
    )
    monkeypatch.setattr(
        fanout.producer,
        "pass_task_text",
        lambda backend, number, *, instructions_path=None, dimension=None: (
            f"task for {dimension.name}"
        ),
    )

    def fake_run_tree_review(backend, ctx, **kw):
        outcome = capture["reviews"][kw["dimension"].name]
        assert kw["tree_path"] == "/tree"  # every pass shares the ONE Tree
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fanout.producer, "run_tree_review", fake_run_tree_review)

    def fake_run_calibrator(config, union, *, pr_number, cwd, launcher=None):
        capture["union"] = union
        assert cwd == "/tree"
        return capture["result"], "cal-run-id", "calibrator task"

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
    )

    # The union handed to the calibrator: ids == index, dimension-tagged, the
    # trust-boundary coercion applied (category defaults to the dimension).
    union = _seams["union"]
    assert [c["id"] for c in union] == [0, 1]
    assert union[0]["dimension"] == "correctness"
    assert union[0]["category"] == "correctness"
    assert union[1]["dimension"] == "test-quality"

    review = outcome.review
    # Only the post-disposition finding posts; major-or-worse blocks.
    assert [c["text"] for c in review["comments"]] == ["bug"]
    assert review["summary"]["status"] == "REQUEST_CHANGES"
    # The calibrator's summary + the fan-out attestation land in the body.
    assert "the verdict" in review["summary"]["overall_feedback"]
    assert (
        "2 candidate finding(s) -> 1 posted" in (review["summary"]["overall_feedback"])
    )
    # Coverage attestation is the union of the passes'.
    assert review["summary"]["coverage"]["reviewed"] == ["a.py", "t.py"]

    # The FULL judged set persists — the routed-out finding included.
    assert dict((f.text, d) for f, d in outcome.findings) == {
        "bug": Disposition.POST,
        "missing test": Disposition.OUT_OF_SCOPE,
    }

    # The contributing-run trail: one entry per pass + the calibrator, each
    # with a run id and a variant hash (the WS03 record/eval join handles).
    kinds = [run["kind"] for run in outcome.runs]
    assert kinds == ["dimension-pass", "dimension-pass", "calibrator"]
    assert all(run["run_id"] for run in outcome.runs)
    assert all(
        run["variant"]["content_hash"].startswith("sha256:") for run in outcome.runs
    )
    assert outcome.runs[0]["dimension"] == "correctness"
    assert outcome.runs[2]["run_id"] == "cal-run-id"
    assert outcome.runs[2]["reasoning"] == "high"


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
    )
    # The surviving pass's finding still made it through calibration.
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]
    # The failed pass is attested in the summary AND recorded in the run trail.
    assert "DEGRADED COVERAGE" in outcome.review["summary"]["overall_feedback"]
    failed = [r for r in outcome.runs if r["kind"] == "dimension-pass"][1]
    assert failed["dimension"] == "security-robustness"
    assert failed["outcome"] == "failed"
    assert "codex exited 1" in failed["detail"]


def test_all_passes_failing_fails_the_round(_seams):
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": RuntimeError("boom a"),
        "test-quality": RuntimeError("boom b"),
    }
    with pytest.raises(RuntimeError, match="all 2 dimension passes failed"):
        fanout.run_fanout_review(
            agent_backend.CODEX,
            _ctx(),
            dimensions=["correctness", "test-quality"],
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
    )
    assert _seams["union"] is None  # the calibrator never ran
    review = outcome.review
    assert review["comments"] == []
    assert review["summary"]["status"] == "APPROVED"
    assert "0 candidate finding(s)" in review["summary"]["overall_feedback"]
    assert review["summary"]["coverage"]["reviewed"] == ["a.py", "t.py"]
    assert outcome.findings == ()
    assert [r["kind"] for r in outcome.runs] == ["dimension-pass", "dimension-pass"]


def test_empty_union_with_a_failed_pass_never_reads_approved(_seams):
    """Degraded coverage must not overstate: with a pass missing, an empty
    union is a COMMENT (attested), never an APPROVED."""
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


# --- route_calibrated: the deterministic routing (pure) ------------------------


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
    assert posts == ["c", "n"]  # the duplicate is judged but never double-posts
    # ... yet it is retained in the routed set with its twin's disposition.
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
    """The cap only SUPPRESSES: a nit the calibrator already routed out stays
    routed out and never consumes cap budget."""
    entries = (
        _calibrated(0, _finding(Severity.NIT, "dropped"), Disposition.DROP_UNVERIFIED),
        _calibrated(1, _finding(Severity.NIT, "kept")),
    )
    routed = fanout.route_calibrated(entries, nit_cap=1)
    by_text = {e.finding.text: d for e, d in routed}
    assert by_text["dropped"] is Disposition.DROP_UNVERIFIED
    assert by_text["kept"] is Disposition.POST


def test_dry_run_prints_per_pass_argv_and_bills_nothing(monkeypatch, capsys):
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
    assert "would calibrate the union" in capsys.readouterr().out
    assert outcome.review["comments"] == []
    assert outcome.runs == ()


def test_default_dimension_set_is_used_when_none_configured(_seams):
    from shipit.agent import backend as agent_backend
    from shipit.review.dimensions import DEFAULT_DIMENSION_NAMES

    _seams["reviews"] = {name: _pass_review([]) for name in DEFAULT_DIMENSION_NAMES}
    outcome = fanout.run_fanout_review(agent_backend.CODEX, _ctx())
    assert [r["dimension"] for r in outcome.runs] == list(DEFAULT_DIMENSION_NAMES)


def test_by_name_is_the_prompt_slice_the_passes_launch_with():
    """The pass prompt embeds the dimension focus (the narrow-attention
    mechanism) — pinned against the real prompt builder, not the fake."""
    from shipit.agent import backend as agent_backend
    from shipit.review.producer import pass_task_text

    task = pass_task_text(
        agent_backend.CODEX, 5, dimension=by_name("cross-file-invariants")
    )
    assert "DIMENSION FOCUS — Cross-file invariants" in task
    assert "READ BEYOND THE DIFF" in task
    assert "do not silently drop" in task
