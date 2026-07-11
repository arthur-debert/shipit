"""Unit tests for `shipit.review.fanout` — round-1 review orchestration +
union post (RVW02-WS04/WS08, ADR-0045, ADR-0052).

The orchestration is pinned with the producer + calibrator seams FAKED (no
Tree, no model run, no gh): the round-1 shape switch (default single
monolithic pass, ADR-0052; explicit `dimensions` → the fan-out), the pass
fan-out over one shared Tree, the union's shape and trust-boundary coercion,
pass-failure tolerance vs all-failed, the empty-union short-circuit, the
deterministic routing (duplicates never post, the nit cap, the derived
status), the merged coverage attestation, and the contributing-run trail
(run ids + variant hashes) the round record persists.

Both round-1 paths are covered: the DEFAULT mechanically-deduped union
(RVW02-WS08, calibrator off — no model run, pass severities kept, same-location
same-claim merged) and the DORMANT LLM calibrator when a reviewer opts it back
on (`calibrator=_CAL`).
"""

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

#: Opt the dormant LLM calibrator back ON. The default (``calibrator=None``,
#: RVW02-WS08) is OFF — the mechanically-deduped union — so every test that
#: exercises the judge path passes this explicitly.
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
    # A fix-range-rescoped view: base_sha is the last-reviewed head, head_sha the
    # new head (RVW02-WS06). The fan-out derives the incremental range from these.
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
    """Keep the RVW03-WS02 artifact bundles out of the real user state dir.

    `run_fanout_review` writes per-run bundles under the store family root by
    default; redirecting `platformdirs.user_state_dir` pins every un-injected
    write to this test's tmp dir, so the suite never touches a real `$HOME`."""
    from shipit.harness.eval import store

    monkeypatch.setattr(
        store.platformdirs, "user_state_dir", lambda name: str(tmp_path / "state")
    )
    return tmp_path / "state"


@pytest.fixture
def _seams(monkeypatch):
    """Fake the producer + calibrator seams; returns the capture dict tests
    read (per-dimension reviews or exceptions in `reviews`, the union handed
    to the calibrator in `union`, the calibration result in `result`, the
    round preflight's backend sets in `preflights`)."""
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
        lambda ctx: capture["trees"].append("/tree") or "/tree",
    )

    def fake_pass_task_text(
        backend,
        number,
        *,
        instructions_path=None,
        dimension=None,
        incremental_range=None,
    ):
        if incremental_range is not None:
            return (
                f"incremental task for {incremental_range[0]}..{incremental_range[1]}"
            )
        if dimension is None:
            # The round-1 DEFAULT single pass (ADR-0052) launches unscoped.
            return "single full-scope task"
        return f"task for {dimension.name}"

    monkeypatch.setattr(fanout.producer, "pass_task_text", fake_pass_task_text)

    def fake_run_tree_review(backend, ctx, **kw):
        # An incremental round runs ONE pass (dimension=None, incremental_range
        # set) keyed as "incremental"; the round-1 DEFAULT single pass
        # (ADR-0052) is also unscoped (dimension=None, no range) and keys as
        # "single"; a fan-out pass is keyed by dimension name.
        if kw.get("dimension") is not None:
            key = kw["dimension"].name
        elif kw.get("incremental_range") is not None:
            key = "incremental"
        else:
            key = "single"
        outcome = capture["reviews"][key]
        assert kw["tree_path"] == "/tree"  # every pass shares the ONE Tree
        if isinstance(outcome, Exception):
            raise outcome
        # Mirror the real producer's capture (RVW03-WS04): per-launch usage
        # (per-key override via capture["usage"], else explicitly unreported)
        # and the APPLIED reasoning — the fake plays a knob-carrying backend
        # (codex), so the requested level is what lands in argv.
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
            # The fake plays the default claude judge, whose `--effort` knob is
            # real (RVW03-WS04) — the config level IS the applied level here.
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
    assert dict((j.finding.text, j.disposition) for j in outcome.findings) == {
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


def test_merged_away_duplicate_rides_the_record_without_double_posting(_seams):
    """A deduped duplicate (the expected overlap dedup exists for) must reach the
    record with its `duplicate_of` edge intact, count once in the attestation as
    a duplicate, and NEVER emit a second posted comment — so the persisted
    disposition==post set can never disagree with what actually posted."""
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {
        "correctness": _pass_review([_comment("same bug", severity="major")]),
        "test-quality": _pass_review(
            [_comment("same bug", severity="major", file="t.py")], reviewed=("t.py",)
        ),
    }
    # The calibrator merges candidate 1 INTO canonical 0 (both `post`); parse
    # materializes the duplicate carrying the canonical's disposition.
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
    # ONE posted comment though the union carried two candidates.
    assert [c["text"] for c in review["comments"]] == ["same bug"]
    # The attestation arithmetic balances: 2 candidates -> 1 posted + 1 duplicate.
    assert "2 candidate finding(s) -> 1 posted" in review["summary"]["overall_feedback"]
    assert "1 duplicate)" in review["summary"]["overall_feedback"]
    # The FULL judged set persists both entries — the duplicate keeps its edge,
    # so it is NOT counted as posted downstream (eval report reads duplicate_of).
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
    # The surviving pass's finding still made it through calibration.
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]
    # The failed pass is attested in the summary AND recorded in the run trail.
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
    # The fatal path returns no FanoutOutcome, so the ONLY witness of the failed
    # passes is the state-root + log side effects — assert they survive the
    # raise: each failed pass wrote a bundle (meta carrying its failed outcome +
    # full error) and emitted its launched + settled events.
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
    """RVW03-WS03: the round's configured backend set is preflighted ONCE, and
    it happens BEFORE the Tree provisions — so a missing binary can never cost
    a Tree clone or a pass launch."""
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
        lambda ctx: order.append("provision") or real_provision(ctx),
    )
    fanout.run_fanout_review(agent_backend.CODEX, _ctx(), dimensions=["correctness"])
    assert order == ["preflight", "provision"]
    assert _seams["preflights"] == [[agent_backend.CODEX]]


def test_round_preflight_includes_the_calibrators_backend_when_the_judge_is_on(
    _seams,
):
    """With the dormant judge opted on, its backend is part of the round's
    configured set — its binary missing must surface at preflight, not after
    every pass already ran (the round's most expensive possible failure)."""
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"correctness": _pass_review([])}
    fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"], calibrator=_CAL
    )
    assert _seams["preflights"] == [
        [agent_backend.CODEX, agent_backend.by_name(_CAL.backend)]
    ]


def test_missing_binary_fails_the_round_before_any_pass_launches(monkeypatch, _seams):
    """RVW03-WS03 regression: a missing backend binary is ONE actionable
    BackendUnavailable from the round preflight — never 'all N dimension passes
    failed' with N truncated per-pass details — and NO pass launches, NO Tree
    provisions."""
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
    """The fan-out's central safety invariant (ADR-0045): a calibrator failure —
    unavailable / timed out / unparseable / contract-violating — PROPAGATES; an
    uncalibrated union is NEVER posted (severities off the common ruler,
    unverified). A non-empty union reaches the calibrator, the calibrator raises,
    and `run_fanout_review` raises rather than degrading to the raw union."""
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
    assert _seams["union"] is None  # the calibrator never ran
    review = outcome.review
    assert review["comments"] == []
    assert review["summary"]["status"] == "APPROVED"
    # The empty union had nothing to route, so the attestation says so instead
    # of claiming a routing "after calibration" that never ran.
    assert "no candidate findings" in review["summary"]["overall_feedback"]
    assert "after calibration" not in review["summary"]["overall_feedback"]
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


# --- the DEFAULT off path: deduped union, no calibrator (RVW02-WS08) -----------


def test_default_posts_deduped_union_with_pass_severities_no_calibrator(_seams):
    """The DEFAULT round-1 (calibrator=None): the union posts through the
    MECHANICAL dedup using each pass's OWN severity — the LLM judge never runs,
    the run trail carries no calibrator entry, and the attestation says so."""
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
    assert _seams["union"] is None  # the calibrator never ran

    review = outcome.review
    # Both distinct findings post, each with its own pass-assigned severity.
    assert {c["text"]: c["severity"] for c in review["comments"]} == {
        "real bug": "major",
        "weak test": "nit",
    }
    # A major posted -> the round blocks; the summary attests the off path.
    assert review["summary"]["status"] == "REQUEST_CHANGES"
    assert "posted as the deduped union" in review["summary"]["overall_feedback"]
    assert "calibrator off" in review["summary"]["overall_feedback"]
    assert "after calibration" not in review["summary"]["overall_feedback"]

    # The run trail is passes ONLY — no calibrator run.
    assert [r["kind"] for r in outcome.runs] == ["dimension-pass", "dimension-pass"]
    # The full judged set persists, both canonical and posted.
    assert all(j.posted and j.duplicate_of is None for j in outcome.findings)


def test_default_union_merges_same_location_same_claim_into_one_canonical(_seams):
    """Two passes flagging the same claim at the same file:line are ONE finding:
    mechanically deduped to one canonical (the most-severe member) that posts
    once, the duplicate riding the record with its `duplicate_of` edge."""
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
    # ONE posted comment though two passes flagged it — at the MOST-severe
    # member's severity (major beats minor); duplicates never double-post.
    assert [(c["text"], c["severity"]) for c in review["comments"]] == [
        ("off-by-one here", "major")
    ]
    assert review["summary"]["status"] == "REQUEST_CHANGES"
    assert "1 duplicate)" in review["summary"]["overall_feedback"]
    # Both union entries persist: canonical (posts) + merged-away duplicate.
    dispositions = sorted((j.duplicate_of is None, j.posted) for j in outcome.findings)
    assert dispositions == [(False, False), (True, True)]


def test_default_union_keeps_distinct_claims_at_the_same_line(_seams):
    """Same file:line but DIFFERENT claims are distinct findings — the
    conservative mechanical key never fuses them."""
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
    """The nit cap is CODE-enforced downstream (route_calibrated), so it rides
    the off path exactly as it rides the calibrated one."""
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
    # Only the first nit posts; the second flips to nit-suppressed (retained).
    assert [c["text"] for c in outcome.review["comments"]] == ["nit one"]
    suppressed = [
        j for j in outcome.findings if j.disposition is Disposition.NIT_SUPPRESSED
    ]
    assert [j.finding.text for j in suppressed] == ["nit two"]
    assert "1 nit-suppressed" in outcome.review["summary"]["overall_feedback"]


# --- dedup_union: the mechanical dedup (pure) ---------------------------------


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
    # Two groups -> two canonicals; the same-claim group merges 0+1.
    assert {e.finding.text for e in canonicals} == {"same claim", "other claim"}
    # The canonical for the merged group is the MOST severe member (major).
    merged_canonical = next(e for e in canonicals if e.finding.text == "same claim")
    assert merged_canonical.finding.severity is Severity.MAJOR
    assert merged_canonical.id == 1  # the major-severity member
    assert set(merged_canonical.merged) == {0}
    # The duplicate carries the canonical's severity and points back to it.
    assert len(duplicates) == 1
    assert duplicates[0].id == 0
    assert duplicates[0].duplicate_of == 1
    assert duplicates[0].finding.severity is Severity.MAJOR
    assert duplicates[0].finding.text == "same claim"
    # Every entry is `post`; routing (dedup/cap) is route_calibrated's job.
    assert all(e.disposition is Disposition.POST for e in entries)


def test_dedup_union_ties_break_on_lowest_id():
    union = [
        _cand(0, "tie", severity="major"),
        _cand(1, "tie", severity="major"),
    ]
    entries = fanout.dedup_union(union)
    canonical = next(e for e in entries if e.duplicate_of is None)
    assert canonical.id == 0  # equal severity -> lowest union id wins


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
    """route_calibrated relies on canonicals appearing before their duplicates
    (its stable severity sort must see the twin first) — dedup_union must emit
    them in that order so the off path routes correctly."""
    union = [_cand(0, "dup", severity="minor"), _cand(1, "dup", severity="minor")]
    entries = fanout.dedup_union(union)
    routed = fanout.route_calibrated(entries, nit_cap=None)
    posts = [e for e, d in routed if d is Disposition.POST and e.duplicate_of is None]
    assert len(posts) == 1  # exactly one posts; the duplicate does not blow up


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


def test_nit_cap_suppression_propagates_to_merged_away_duplicates():
    """A duplicate shares its canonical twin's FINAL disposition — when the cap
    flips the canonical nit to nit-suppressed, its duplicate must flip too, never
    sail through as a stale POST (else the record persists a POST that never
    posted and the flow log misses a routed-out finding)."""
    entries = (
        _calibrated(0, _finding(Severity.NIT, "nit")),
        _calibrated(1, _finding(Severity.NIT, "nit-dup"), duplicate_of=0),
    )
    routed = fanout.route_calibrated(entries, nit_cap=0)
    by_text = {e.finding.text: d for e, d in routed}
    # nit_cap=0 floors the canonical at suppressed; the duplicate follows.
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
    # Default: calibrator OFF — the note is about the mechanical union, not a judge.
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
    """ADR-0052: no configured `dimensions` → ONE unscoped full-scope pass,
    never the dimension fan-out. The pass launches with dimension=None (the
    monolithic task) and its run entry carries the `single` label."""
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
    assert seen_dimensions == [None]  # unscoped: the monolithic full-scope task
    assert (
        "Review: one full-scope pass" in (outcome.review["summary"]["overall_feedback"])
    )
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]


def test_explicit_dimensions_config_still_routes_to_the_fanout(_seams):
    """The fan-out stays fully wired behind the explicit opt-in (ADR-0052):
    a named `dimensions` list runs exactly those scoped passes in parallel
    and posts their union, exactly as under ADR-0045."""
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
    """An override targets a dimension pass; the default single-pass round has
    none (ADR-0052) — silently ignoring it would run a mislabeled arm."""
    from shipit.agent import backend as agent_backend

    with pytest.raises(ValueError, match="explicit `dimensions` fan-out"):
        fanout.run_fanout_review(
            agent_backend.CODEX,
            _ctx(),
            invocation_overrides={"correctness": {"model": "flash"}},
        )


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
    # Default path (calibrator OFF, WS08): the pass self-scopes to the diff —
    # there is no routing stage to drop a purely pre-existing finding.
    assert "INTRODUCED or EXPOSED" in task
    assert "Your stated severity is the posted severity" in task


# --- incremental rounds (RVW02-WS06, ADR-0045) ------------------------------


def test_incremental_round_runs_one_pass_suppresses_nits_records_range(_seams):
    """A round after the first is ONE incremental pass over the fix range, at the
    cheaper reasoning, with new nits suppressed (default dedup path)."""
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

    # Exactly ONE pass ran — not the dimension fan-out.
    assert [run["kind"] for run in outcome.runs] == ["incremental-pass"]
    run = outcome.runs[0]
    # The cheaper reasoning level is stamped from the argv ACTUALLY applied
    # (RVW03-WS04: the fake plays a knob-carrying backend, so the requested
    # level ran) — no longer a record-only echo of config.
    assert run["reasoning"] == fanout.DEFAULT_INCREMENTAL_REASONING
    # The fix range is recorded on the run entry.
    assert run["range"] == {"base": "b" * 40, "head": "c" * 40}
    assert run["dimension"] == "incremental"

    review = outcome.review
    # The major posts; the new nit is SUPPRESSED (not posted) — a late round
    # can't be recolonized by style churn.
    assert [c["text"] for c in review["comments"]] == ["real bug"]
    suppressed = {
        j.finding.text: j.disposition for j in outcome.findings if not j.posted
    }
    assert suppressed == {"style": Disposition.NIT_SUPPRESSED}


def test_incremental_round_still_runs_the_calibrator_when_configured(_seams):
    # single-pass + calibrator (ADR-0045): opting the judge on still routes the
    # single incremental pass's union through it.
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
    assert _seams["union"] is not None  # the calibrator ran over the single pass
    kinds = [run["kind"] for run in outcome.runs]
    assert kinds == ["incremental-pass", "calibrator"]
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]


def test_incremental_pass_failure_fails_the_round(_seams):
    # The sole pass failing IS all passes failing → RuntimeError (service maps it
    # to the `failed` funnel outcome).
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"incremental": RuntimeError("backend blew up")}
    with pytest.raises(RuntimeError, match="the incremental pass failed"):
        fanout.run_fanout_review(
            agent_backend.CODEX, _incremental_ctx(), incremental=True
        )


def test_single_pass_failure_fails_the_round(_seams):
    # ADR-0052: the round-1 DEFAULT is one monolithic pass. Its sole pass failing
    # IS all passes failing → RuntimeError with the single-pass phrasing (service
    # maps it to the `failed` funnel outcome), the failure posture of the new
    # default path.
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"single": RuntimeError("backend blew up")}
    with pytest.raises(RuntimeError, match="the single review pass failed"):
        fanout.run_fanout_review(agent_backend.CODEX, _ctx())


# --- measured token usage (RVW03-WS04) ---------------------------------------


def test_per_run_usage_is_stamped_and_the_round_total_sums_reported_runs(_seams):
    """Each run entry carries the usage its CLI reported (RVW03-WS04, #667) and
    the round total sums exactly the REPORTED runs — a partially-reported round
    is a lower bound, never padded with fabricated zeros."""
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
    """A round where NO run reported usage totals None — the explicit
    latency-only marker the eval report distinguishes — never a fake 0."""
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
    """A failed pass's run entry keeps usage explicitly-unknown (its launch may
    have billed tokens nobody reported) while the surviving pass's measurement
    still reaches the round total."""
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
    """The calibrator's run entry carries its measured usage and the reasoning
    level the adapter ACTUALLY applied (RVW03-WS04) — and its usage joins the
    round total alongside the passes'."""
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
    assert cal["reasoning"] == "high"  # applied by the claude --effort knob
    assert outcome.total_tokens == 2100


def test_round1_passes_record_no_reasoning_when_none_was_applied(_seams):
    """Round-1 passes request no ReasoningLevel, so no run entry carries one —
    the record never invents a level that was not in argv (#685)."""
    from shipit.agent import backend as agent_backend

    _seams["reviews"] = {"correctness": _pass_review([])}
    outcome = fanout.run_fanout_review(
        agent_backend.CODEX, _ctx(), dimensions=["correctness"]
    )
    assert "reasoning" not in outcome.runs[0]


# --- the offline range target (RVW03-WS01) -----------------------------------


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
    """Fake the RANGE producer seams (the offline arm's dispatch targets) and
    BOOBY-TRAP the PR-coupled ones — a RangeView target must never provision a
    Tree or compose a `gh pr diff` task."""
    capture: dict = {
        "reviews": {},
        "union": None,
        "result": None,
        "calls": [],
        "preflights": [],
    }

    def _boom(*args, **kwargs):
        raise AssertionError("a RangeView target must never touch a PR seam")

    # The range arm still preflights its backend binaries (RVW03-WS03 preflight,
    # shared with the PR arm) — stub it so the offline tests don't require the
    # real `codex`/`claude` binaries on PATH, capturing the round's backend set.
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
        # Mirror the live arm (RVW03-WS04): the range pass's capture carries its
        # measured usage (per-dimension override via capture["usage"]) so range
        # run entries carry usage exactly like live entries, plus applied reasoning.
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
    """One code path (RVW03-WS01): a RangeView target runs the SAME fan-out —
    union, dedup, routing, run trail — with the passes dispatched to the range
    producer (no Tree, no gh) in the replay checkout."""
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
    # Every pass launched through run_range_review with its dimension slice
    # (the passes run in parallel, so compare order-insensitively).
    assert sorted(c["dimension"].name for c in _range_seams["calls"]) == [
        "correctness",
        "test-quality",
    ]
    assert all(c["view"].workdir == "/replay-checkout" for c in _range_seams["calls"])
    # The range round preflights its backend binaries too (RVW03-WS03, shared
    # with the PR arm) — once, over the reviewer's configured backend.
    assert _range_seams["preflights"] == [[agent_backend.CODEX]]
    # The routed outcome is the fan-out's usual product: runs per pass, the
    # deduped union posted with pass severities.
    assert [run["kind"] for run in outcome.runs] == ["dimension-pass"] * 2
    assert [c["text"] for c in outcome.review["comments"]] == ["bug"]
    assert outcome.review["summary"]["status"] == "REQUEST_CHANGES"


def test_range_target_calibrator_gets_the_range_ground_truth(_range_seams):
    # The third PR-coupled seam: with the judge on, an offline round hands the
    # calibrator the RANGE (its `git diff` ground truth) and the replay
    # checkout as cwd — never a PR number, never a Tree.
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
    # Rounds are keyed to a live PR head (multi-round fix-range replay is out
    # of the Review Lab's scope) and the dry-run contract prints a would-run
    # TREE launch — both are caller errors on a RangeView, refused loud.
    from shipit.agent import backend as agent_backend

    with pytest.raises(ValueError, match="incremental"):
        fanout.run_fanout_review(agent_backend.CODEX, _range_view(), incremental=True)
    with pytest.raises(ValueError, match="dry_run"):
        fanout.run_fanout_review(agent_backend.CODEX, _range_view(), dry_run=True)


# ---------------------------------------------------------------------------
# RVW03-WS02 — per-run artifact bundles + finding↔pass correlation + progress
# ---------------------------------------------------------------------------


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
    """Every pass — success AND failure — gets an artifact bundle keyed by its
    run id under the round's directory; the round id/artifacts location ride
    the outcome; every finding carries its originating pass's run id."""
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

    # The round's observability identity, discoverable from the outcome (and
    # thence the round record): bundles live under the shared state root.
    assert outcome.round_id
    assert outcome.artifacts_dir == str(
        _tmp_state_root / "review-artifacts" / "owner" / "repo" / outcome.round_id
    )

    runs = {run["dimension"]: run for run in outcome.runs}
    ok, bad = runs["correctness"], runs["test-quality"]
    # Each run entry points at its own bundle dir (named by its run id).
    assert ok["artifacts"] == str(_Path(outcome.artifacts_dir) / ok["run_id"])
    assert bad["artifacts"] == str(_Path(outcome.artifacts_dir) / bad["run_id"])
    # UNCONDITIONAL: the FAILED pass's bundle exists too, meta carrying the
    # run identity + settled outcome + the untruncated error.
    bad_meta = _json.loads((_Path(bad["artifacts"]) / "meta.json").read_text())
    assert bad_meta["run_id"] == bad["run_id"]
    assert bad_meta["round_id"] == outcome.round_id
    assert bad_meta["outcome"] == "failed"
    assert bad_meta["error"] == "child exploded"
    ok_meta = _json.loads((_Path(ok["artifacts"]) / "meta.json").read_text())
    assert ok_meta["outcome"] == "success" and ok_meta["findings"] == 1

    # Finding↔pass correlation: the posted finding traces to the pass that
    # emitted it.
    assert [j.run_id for j in outcome.findings] == [ok["run_id"]]

    # The coarse progress trail: one launched + one settled event per pass,
    # each carrying the correlation extras.
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
    """The judge's bundle lives at the fixed `calibrator` name under the round
    dir (one judge per round; its true run id is post-hoc) and its run entry
    points there."""
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
    # The bundle handle was threaded into run_calibrator (which writes the
    # prompt/streams at the launch seam).
    assert _seams["calibrator_artifacts"] is not None
    assert str(_seams["calibrator_artifacts"].dir) == cal["artifacts"]


def test_calibrator_progress_events_carry_the_stable_surrogate_run_id(
    _seams, _tmp_state_root, caplog
):
    """The one judge per round has no true run id until AFTER it launches (and
    none at all on a pre-id failure), so its launch + settle events and its
    raw-output log correlate by the STABLE surrogate `run_id=calibrator` (the
    fixed bundle name) — `shipit logs --run calibrator` slices its whole trail.
    The round-level completion + disposition events carry `round_id`/`run_id`
    too, so no round-level record falls outside the `--round`/`--run` filters."""
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

    # The correlation extras threaded into run_calibrator (for its DEBUG raw log).
    corr = _seams["calibrator_correlation"]
    assert corr["run_id"] == "calibrator"
    assert corr["round_id"] == outcome.round_id
    assert corr["dimension"] == "calibrator"

    by_event: dict[str, list] = {}
    for r in caplog.records:
        name = getattr(r, "_event", None)
        if name is not None:
            by_event.setdefault(name, []).append(r)

    # Both calibrator progress events correlate by the surrogate run id.
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

    # The round completion event is round-correlated.
    [completed] = by_event["review.calibrated"]
    assert completed.round_id == outcome.round_id

    # The routed-out finding's disposition event traces to its round AND its
    # originating pass run.
    pass_run_id = next(
        run["run_id"] for run in outcome.runs if run["kind"] == "dimension-pass"
    )
    [disp] = by_event["finding.dispositioned"]
    assert disp.round_id == outcome.round_id
    assert disp.run_id == pass_run_id


def test_calibrator_failure_settled_event_carries_the_surrogate_run_id(
    monkeypatch, _seams, _tmp_state_root, caplog
):
    """Even a calibrator that fails BEFORE yielding a session id settles a
    `review.pass.settled` failure event that `--run calibrator` can select."""
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
    """The union tags every candidate with the run id of the pass that emitted
    it — the join `_pass_run_id` reads back onto the judged findings."""
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
    """A hand-built ctx with no repo slug disables the bundles — the round
    still runs, runs carry `artifacts: None`, the outcome carries no
    artifacts dir, and nothing is written."""
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
    assert outcome.round_id  # the round is still identified
    assert all(run["artifacts"] is None for run in outcome.runs)
    assert [j.run_id for j in outcome.findings] == [outcome.runs[0]["run_id"]]
    assert not (_tmp_state_root / "review-artifacts").exists()
