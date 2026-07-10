"""Unit tests for `shipit.review.calibrator` — the one fixed judge (RVW02-WS04,
ADR-0045).

Two surfaces are pinned here, per the RVW02 testing decision that the
calibrator's WISDOM is never unit-tested (that is the offline A/B harness's
job), only its I/O contract:

  * :func:`parse_calibration` — the contract boundary: schema shape, a known
    disposition on every judged finding, exact union coverage (never
    originates / never double-judges / never silently drops), the fail-safe
    severity, the code-enforced evidence floor, and the merged-dedup
    materialization; and
  * :func:`run_calibrator` — the launch seam: read-only reviewer posture in
    the shared Tree, the config's model/timeout threading, the claude result
    envelope unwrap (session_id → run id), and the timeout / nonzero-child
    error normalization.
"""

from __future__ import annotations

import json

import pytest

from shipit import execrun
from shipit.finding import Disposition, Severity
from shipit.review import calibrator
from shipit.review.backends import BackendError, BackendUnavailable
from shipit.review.calibrator import (
    CalibrationContractError,
    CalibratorConfig,
    build_calibrator_task,
    parse_calibration,
    run_calibrator,
)
from shipit.spawn.launch import LaunchResult


def _candidate(i: int, **overrides) -> dict:
    base = {
        "id": i,
        "dimension": "correctness",
        "file": f"src/mod{i}.py",
        "line": 10 + i,
        "severity": "minor",
        "category": "correctness",
        "confidence": 0.8,
        "text": f"claim {i}",
        "evidence": f"code {i}",
        "fix": f"fix {i}",
    }
    base.update(overrides)
    return base


def _entry(i: int, **overrides) -> dict:
    base = {
        "id": i,
        "merged": [],
        "severity": "minor",
        "disposition": "post",
        "text": f"judged {i}",
        "evidence": f"quoted {i}",
        "fix": "",
    }
    base.update(overrides)
    return base


# --- CalibratorConfig: construction is validation ------------------------------


def test_default_calibrator_is_claude_at_high_reasoning():
    """The ADR-0045 shipped default: the claude backend at high ReasoningLevel."""
    config = calibrator.DEFAULT_CALIBRATOR
    assert config.backend == "claude"
    assert config.model is None
    assert config.reasoning == "high"
    assert config.timeout == "600s"


def test_config_rejects_unknown_backend():
    with pytest.raises(ValueError, match="calibrator backend"):
        CalibratorConfig(backend="gpt-cli")


def test_config_rejects_bad_reasoning_and_timeout():
    with pytest.raises(ValueError, match="reasoning"):
        CalibratorConfig(reasoning="ultra")
    with pytest.raises(ValueError, match="timeout"):
        CalibratorConfig(timeout="10m")
    with pytest.raises(ValueError, match="model"):
        CalibratorConfig(model="  ")


# --- parse_calibration: the I/O contract ---------------------------------------


def test_happy_path_judges_every_candidate():
    union = [_candidate(0, severity="nit"), _candidate(1)]
    payload = {
        "summary": {"overall_feedback": "looks solid"},
        "findings": [
            _entry(0, severity="major", disposition="post"),
            _entry(1, disposition="out-of-scope"),
        ],
    }
    result = parse_calibration(payload, union)
    assert result.overall_feedback == "looks solid"
    by_id = {e.id: e for e in result.entries}
    # Severity is NORMALIZED by the judge (the pass's own claim is a prior).
    assert by_id[0].finding.severity is Severity.MAJOR
    assert by_id[0].disposition is Disposition.POST
    assert by_id[1].disposition is Disposition.OUT_OF_SCOPE
    # Location/category/confidence always come from the union candidate.
    assert by_id[0].finding.file == "src/mod0.py"
    assert by_id[0].finding.line == 10
    assert by_id[0].finding.confidence == 0.8


def test_merged_dedup_materializes_duplicates_with_the_inverse_edge():
    """A merged id is judged THROUGH its canonical twin: it appears in the
    result as its own entry (its OWN union text/location) carrying the twin's
    severity + disposition and the `duplicate_of` inverse edge."""
    union = [_candidate(0), _candidate(1), _candidate(2)]
    payload = {
        "summary": {"overall_feedback": ""},
        "findings": [
            _entry(0, merged=[2], severity="critical"),
            _entry(1, disposition="drop-unverified"),
        ],
    }
    result = parse_calibration(payload, union)
    by_id = {e.id: e for e in result.entries}
    assert set(by_id) == {0, 1, 2}
    assert by_id[0].merged == (2,)
    assert by_id[0].duplicate_of is None
    assert by_id[2].duplicate_of == 0
    assert by_id[2].disposition is by_id[0].disposition
    assert by_id[2].finding.severity is Severity.CRITICAL
    assert by_id[2].finding.text == "claim 2"  # its OWN union content


def test_originated_finding_is_a_contract_violation():
    """The never-originates rule, enforced where checkable: an id outside the
    union is rejected loud — never posted, never recorded as judged."""
    union = [_candidate(0)]
    payload = {"findings": [_entry(0), _entry(7)]}
    with pytest.raises(CalibrationContractError, match="never originates"):
        parse_calibration(payload, union)


def test_double_judged_and_missing_ids_are_contract_violations():
    union = [_candidate(0), _candidate(1)]
    with pytest.raises(CalibrationContractError, match="more than once"):
        parse_calibration({"findings": [_entry(0), _entry(0)]}, union)
    with pytest.raises(CalibrationContractError, match="more than once"):
        parse_calibration({"findings": [_entry(0, merged=[1]), _entry(1)]}, union)
    with pytest.raises(CalibrationContractError, match="missing candidate id"):
        parse_calibration({"findings": [_entry(0)]}, union)


def test_unknown_disposition_and_bad_shapes_are_contract_violations():
    union = [_candidate(0)]
    with pytest.raises(CalibrationContractError, match="disposition"):
        parse_calibration({"findings": [_entry(0, disposition="maybe")]}, union)
    with pytest.raises(CalibrationContractError, match="'findings'"):
        parse_calibration({"summary": {}}, union)
    with pytest.raises(CalibrationContractError, match="must be objects"):
        parse_calibration({"findings": ["nope"]}, union)
    with pytest.raises(CalibrationContractError, match="integer candidate id"):
        parse_calibration({"findings": [_entry("0")]}, union)


def test_unparseable_severity_lands_on_the_major_fail_safe():
    """The domain fail-safe (ADR-0044): an unparseable judged severity is
    `major` — it forces a round rather than slipping past the Breaker."""
    union = [_candidate(0)]
    result = parse_calibration({"findings": [_entry(0, severity="HIGH")]}, union)
    assert result.entries[0].finding.severity is Severity.MAJOR


def test_blank_judged_fields_fall_back_to_the_union_candidate():
    union = [_candidate(0)]
    result = parse_calibration(
        {"findings": [_entry(0, text="", evidence="", fix="")]}, union
    )
    finding = result.entries[0].finding
    assert finding.text == "claim 0"
    assert finding.evidence == "code 0"
    assert finding.fix == "fix 0"


def test_post_without_evidence_flips_to_drop_unverified():
    """The verification floor, code-enforced: 'quoted evidence always' — a
    post-disposition finding with no evidence (judged NOR union) is routed
    drop-unverified, never posted, never downgraded-and-kept."""
    union = [_candidate(0, evidence="")]
    result = parse_calibration(
        {"findings": [_entry(0, evidence="", disposition="post")]}, union
    )
    assert result.entries[0].disposition is Disposition.DROP_UNVERIFIED
    # Severity untouched: dropped, never downgraded.
    assert result.entries[0].finding.severity is Severity.MINOR


# --- build_calibrator_task: the F2 reproduction-based verification floor -------
#
# The calibrator's WISDOM is not unit-tested (the A/B harness measures that),
# but the PROMPT's verification-floor framing is a contract the F2 fix
# (RVW02-WS08, #665) turns on: drop only on active REFUTATION, never on mere
# uncertainty. A regression that reverts the prompt to "prove-it-or-drop" would
# silently reintroduce the true-positive over-pruning, so pin the framing.


def test_calibrator_task_drops_only_on_refutation_not_uncertainty():
    task = build_calibrator_task("[]", pr_number=42)
    lowered = task.lower()
    # The drop test is reproduction/refutation, not failure-to-justify.
    assert "refute" in lowered
    assert "reproduces" in lowered
    assert 'drop-unverified" only when you can actively refute' in lowered
    # Uncertainty is explicitly NOT grounds to drop a reproducing finding.
    assert "not grounds to drop" in lowered
    # The preserved-discipline invariants stay: evidence always, concrete
    # failure scenario for major+, never downgrade.
    assert "evidence" in lowered
    assert "concrete failure scenario" in lowered
    assert "never downgrade" in lowered


# --- run_calibrator: the launch seam -------------------------------------------


def _union() -> list[dict]:
    return [_candidate(0)]


def _calibration_json() -> str:
    return json.dumps(
        {
            "summary": {"overall_feedback": "verdict"},
            "findings": [_entry(0)],
        }
    )


def test_run_calibrator_launches_claude_read_only_and_unwraps_the_envelope(
    monkeypatch,
):
    """The claude path: the argv is the read-only reviewer posture carrying the
    config's model; the result envelope is unwrapped and its session_id becomes
    the run id (the eval-record join key)."""
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen.update({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        envelope = json.dumps({"result": _calibration_json(), "session_id": "sess-42"})
        return LaunchResult(returncode=0, stdout=envelope, stderr="")

    config = CalibratorConfig(model="opus-x", timeout="30s")
    result, run_id, task = run_calibrator(
        config, _union(), pr_number=9, cwd="/tree", launcher=fake_runner
    )
    assert run_id == "sess-42"
    assert result.overall_feedback == "verdict"
    assert result.entries[0].disposition is Disposition.POST
    cmd = seen["cmd"]
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--model") + 1] == "opus-x"
    assert "--tools" in cmd  # the read-only reviewer posture
    assert seen["cwd"] == "/tree"
    assert seen["timeout"] == 30.0
    # The task embeds the candidates and the judge contract.
    assert "NEVER originate" in task
    assert '"id": 0' in task


def test_run_calibrator_bare_json_mints_a_run_id(monkeypatch):
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")

    def fake_runner(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout=_calibration_json(), stderr="")

    _, run_id, _ = run_calibrator(
        CalibratorConfig(), _union(), pr_number=9, cwd="/tree", launcher=fake_runner
    )
    assert run_id  # minted — never an empty join key


def test_run_calibrator_missing_cli_is_backend_unavailable(monkeypatch):
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: None)
    with pytest.raises(BackendUnavailable, match="claude"):
        run_calibrator(CalibratorConfig(), _union(), pr_number=9, cwd="/tree")


def test_run_calibrator_seam_timeout_is_a_timed_out_backend_error(monkeypatch):
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")

    def fake_runner(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(
            cmd, rc=None, cause=execrun.CAUSE_TIMEOUT, stdout="partial", stderr=""
        )

    with pytest.raises(BackendError, match="timed out") as excinfo:
        run_calibrator(
            CalibratorConfig(timeout="5s"),
            _union(),
            pr_number=9,
            cwd="/tree",
            launcher=fake_runner,
        )
    assert excinfo.value.timed_out is True
    assert "partial" in excinfo.value.raw


def test_run_calibrator_nonzero_child_and_unparseable_output_raise(monkeypatch):
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")

    def exit_one(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=1, stdout="", stderr="boom")

    with pytest.raises(BackendError, match="exited 1"):
        run_calibrator(
            CalibratorConfig(), _union(), pr_number=9, cwd="/tree", launcher=exit_one
        )

    def prose(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout="no json here", stderr="")

    with pytest.raises(BackendError, match="no parseable JSON"):
        run_calibrator(
            CalibratorConfig(), _union(), pr_number=9, cwd="/tree", launcher=prose
        )


def test_run_calibrator_contract_violation_propagates(monkeypatch):
    """Parseable output that violates the judge contract fails the calibration
    loud — an uncalibrated union is never posted."""
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")
    bad = json.dumps({"findings": [_entry(3)]})  # id 3 not in the union

    def fake_runner(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout=bad, stderr="")

    with pytest.raises(CalibrationContractError, match="never originates"):
        run_calibrator(
            CalibratorConfig(), _union(), pr_number=9, cwd="/tree", launcher=fake_runner
        )


# ---------------------------------------------------------------------------
# RVW03-WS02 — the calibrator run fills its artifact bundle, every path
# ---------------------------------------------------------------------------


def test_run_calibrator_fills_the_bundle_and_records_the_true_run_id(
    monkeypatch, tmp_path
):
    from shipit.review.artifacts import RunArtifacts

    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")

    def fake_runner(cmd, *, cwd, env, timeout=None):
        envelope = json.dumps({"result": _calibration_json(), "session_id": "sess-7"})
        return LaunchResult(returncode=0, stdout=envelope, stderr="warn line")

    bundle = RunArtifacts(tmp_path / "calibrator")
    _, run_id, task = run_calibrator(
        CalibratorConfig(),
        _union(),
        pr_number=9,
        cwd="/tree",
        launcher=fake_runner,
        artifacts=bundle,
    )
    assert run_id == "sess-7"
    # The exact prompt + raw streams landed, and the meta carries the TRUE
    # (post-unwrap) run id — the bundle's dir name is fixed, the id is data.
    assert (bundle.dir / "prompt.txt").read_text() == task
    assert "sess-7" in (bundle.dir / "stdout.raw").read_text()
    assert (bundle.dir / "stderr.raw").read_text() == "warn line"
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["run_id"] == "sess-7"
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is False


def test_run_calibrator_failure_bundle_keeps_full_raw(monkeypatch, tmp_path):
    """A nonzero judge (previously surviving only as detail[:500]) leaves its
    FULL raw output in the bundle, and the error points at it."""
    from shipit.review.artifacts import RunArtifacts

    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")
    long_err = "y" * 2000

    def exit_one(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=1, stdout="half an answer", stderr=long_err)

    bundle = RunArtifacts(tmp_path / "calibrator")
    with pytest.raises(BackendError) as exc:
        run_calibrator(
            CalibratorConfig(),
            _union(),
            pr_number=9,
            cwd="/tree",
            launcher=exit_one,
            artifacts=bundle,
        )
    assert str(bundle.dir) in str(exc.value)
    assert (bundle.dir / "stderr.raw").read_text() == long_err
    assert (bundle.dir / "stdout.raw").read_text() == "half an answer"
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["exit_code"] == 1


def test_calibrator_task_never_carries_the_run_id_plumbing(monkeypatch):
    """The union candidates carry the RVW03-WS02 `run_id` correlation; the
    judge's serialized candidates must NOT (plumbing is the record's business,
    and prompt bytes are variant-hashed)."""
    monkeypatch.setattr(calibrator.shutil, "which", lambda binary: "/usr/bin/claude")

    def fake_runner(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout=_calibration_json(), stderr="")

    union = [dict(_candidate(0), run_id="pass-run-id-hex")]
    _, _, task = run_calibrator(
        CalibratorConfig(), union, pr_number=9, cwd="/tree", launcher=fake_runner
    )
    assert "pass-run-id-hex" not in task
    assert "run_id" not in task
