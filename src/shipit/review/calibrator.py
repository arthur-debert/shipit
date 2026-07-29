"""The one judge between a reviewer's passes and the posted review. See docs/adr/0045-dimension-fanout-single-calibrator.md."""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .. import execrun
from ..agent import backend as agent_backend
from ..agent.invocation import ReasoningLevel
from ..finding import (
    DEFAULT_SEVERITY,
    Disposition,
    Finding,
    parse_severity,
)
from ..spawn import launch
from ..spawn.backends import resolve as resolve_adapter
from ..spawn.backends.antigravity import AntigravityAdapter
from ..spawn.backends.claude import ClaudeAdapter
from ..spawn.backends.codex import CodexAdapter
from ..tree.cleanup import parse_duration
from .artifacts import RunArtifacts
from .backends import BackendError, BackendUnavailable
from .schema import extract_json
from .usage import UNREPORTED, TokenUsage, from_claude_envelope, from_codex_stderr

logger = logging.getLogger("shipit.review")

_CALIBRATOR_ROLE = "reviewer"

#: The canonical `<N>s` duration shape a calibrator ``timeout`` carries.
_TIMEOUT_SHAPE = re.compile(r"^[1-9][0-9]*s$")


@dataclass(frozen=True)
class CalibratorConfig:
    """The table-level calibrator launch config; constructing it validates it."""

    backend: str = "claude"
    model: str | None = None
    reasoning: str = "high"
    timeout: str = "600s"

    def __post_init__(self) -> None:
        try:
            agent_backend.by_name(self.backend)
        except (KeyError, TypeError):
            known = ", ".join(b.name for b in agent_backend.REGISTRY)
            raise ValueError(
                f"calibrator backend must be one of: {known}; got {self.backend!r}"
            ) from None
        if self.model is not None and (
            not isinstance(self.model, str) or not self.model.strip()
        ):
            raise ValueError("calibrator model must be a non-empty string")
        if ReasoningLevel.coerce(self.reasoning) is None:
            levels = ", ".join(level.value for level in ReasoningLevel)
            raise ValueError(
                f"calibrator reasoning must be one of: {levels}; got {self.reasoning!r}"
            )
        if not isinstance(self.timeout, str) or not _TIMEOUT_SHAPE.match(self.timeout):
            raise ValueError(
                f"calibrator timeout must be a canonical `<N>s` duration "
                f"(e.g. '600s'), got {self.timeout!r}"
            )


DEFAULT_CALIBRATOR = CalibratorConfig()


class CalibrationContractError(RuntimeError):
    """The calibrator's output violated its I/O contract."""


@dataclass(frozen=True)
class CalibratedFinding:
    """One judged union finding; an entry with ``duplicate_of`` set was merged away and never posts."""

    id: int
    finding: Finding
    disposition: Disposition
    merged: tuple[int, ...] = ()
    duplicate_of: int | None = None


@dataclass(frozen=True)
class CalibrationResult:
    overall_feedback: str
    entries: tuple[CalibratedFinding, ...]


#: Prose schema for the calibrator's output — described in-prose for every backend.
_CALIBRATION_SCHEMA_PROSE = """\
Output JSON shape (your ENTIRE stdout must be exactly one JSON object of this \
shape — no prose, no markdown fences, nothing before or after it):
{
  "summary": {
    "overall_feedback": "2-6 sentences: what the change does, the overall verdict, and anything systemic."
  },
  "findings": [
    {
      "id": 0,
      "merged": [3, 7],
      "severity": "critical" | "major" | "minor" | "nit",
      "disposition": "post" | "drop-unverified" | "nit-suppressed" | "out-of-scope",
      "text": "the final finding text (see the verification rules)",
      "evidence": "the quoted code the finding rests on",
      "fix": "the suggested remedy (may be empty)"
    }
  ]
}"""


def build_calibrator_task(
    candidates_json: str,
    *,
    pr_number: int | None = None,
    commit_range: tuple[str, str] | None = None,
) -> str:
    """Compose the calibrator task; exactly one of ``pr_number`` / ``commit_range`` is required."""
    if (pr_number is None) == (commit_range is None):
        raise ValueError(
            "build_calibrator_task: exactly one of pr_number (live PR) and "
            "commit_range (offline replay) must be given — the judge needs ONE "
            "ground-truth diff source"
        )
    if commit_range is not None:
        base_sha, head_sha = commit_range
        situation = (
            "You are running in a READ-ONLY checkout of a repository; the review "
            "is an OFFLINE replay of one commit range — there is NO pull request "
            "involved."
        )
        ground_truth = (
            f"FIRST, get the ground truth: run `git diff {base_sha}..{head_sha}` "
            "to read the range's unified diff. Do NOT call `gh` — this review is "
            "offline and touches nothing on GitHub. Read the surrounding code in "
            "this checkout wherever you need context to judge a candidate."
        )
        # Follow the offline framing so the body never names a PR or a post.
        result_fate = "recorded in the local replay record"
        diff_noun = "this range's diff"
        summary_owner = "the review's"
        settle = "records it locally"
    else:
        situation = (
            "You are running in a shared, READ-ONLY checkout of pull request "
            f"#{pr_number}'s head commit."
        )
        ground_truth = (
            f"FIRST, get the ground truth: run `gh pr diff {pr_number}` to read "
            "the pull request's unified diff (it uses the PR's ACTUAL base and "
            "head — do NOT assume the base is `main`). Read the surrounding code "
            "in this checkout wherever you need context to judge a candidate."
        )
        result_fate = "posted"
        diff_noun = "this PR's diff"
        summary_owner = "the posted review's"
        settle = "posts it"
    return f"""\
You are the review CALIBRATOR: the single judge of candidate code-review \
findings. {situation} Parallel dimension-scoped review passes produced \
the candidate findings below; your job is to turn that raw union into the one \
calibrated result that gets {result_fate}.

{ground_truth}

THE CANDIDATE FINDINGS (a JSON array; each candidate has a stable "id"):
{candidates_json}

Judge EVERY candidate. The rules:

1. NEVER originate: you judge the candidates above and NOTHING else. Do not \
add findings of your own, no matter what you notice — every "id" you output \
must be a candidate id, and any new issue you spot is out of your mandate.
2. DEDUP by merging: when several candidates report the same underlying \
issue, keep the best-located, best-argued one and list the others' ids in its \
"merged" array. A merged id must not appear as its own entry.
3. ADVERSARIALLY VERIFY each kept candidate against the actual code: try to \
REFUTE it — trace the code and try to construct the failure it claims. The \
drop test is REPRODUCTION, not eloquence: a candidate gets disposition \
"drop-unverified" ONLY when you can actively refute it — its quoted evidence \
is misquoted or fabricated, the code does not behave as the finding claims, or \
the failure it describes cannot occur (it is guarded, unreachable, or \
contradicted by the surrounding code). A candidate whose failure REPRODUCES \
against the real code is verified and KEPT — keep it even if you would have \
worded or argued it differently; being unsure, or being unable to phrase a \
perfect rationale, is NOT grounds to drop a finding that reproduces. Every \
kept finding needs the quoted code it rests on in "evidence" (quote it from \
this checkout — verify the pass quoted it faithfully). A finding you judge \
major or critical must state a CONCRETE FAILURE SCENARIO in its "text" (what \
inputs/state make it go wrong, and what happens); a minor or nit needs a clear \
rationale. NEVER downgrade a finding's severity to keep it: verify it at the \
severity it deserves, or — only when you have actually refuted it — drop it.
4. Route scope: a verified finding that is beyond {diff_noun} — a \
pre-existing issue a pass reported despite its diff-only scope — gets \
disposition "out-of-scope" (it is persisted, not posted). Everything verified \
and in-scope gets "post".
5. NORMALIZE severity on the one ladder, ignoring the candidates' own \
severity claims where wrong. The major/minor boundary is the MERGE-BLOCK \
TEST: would a competent reviewer hold the merge for this? critical = merging \
would be actively harmful (security hole, data loss, crash, broken build); \
major = a concrete correctness or behavioral defect worth blocking on; minor \
= worth doing, not worth holding the merge; nit = wording, naming, or style \
with no correctness, behavioral, or security impact.
6. COVER every candidate id exactly once: as an entry's "id" or inside \
exactly one entry's "merged" array. An id you drop silently, judge twice, or \
invent is a contract violation and the whole calibration is rejected.

Order the findings array highest severity first (critical, major, minor, \
nit). In "summary.overall_feedback", give {summary_owner} summary \
paragraph.

{_CALIBRATION_SCHEMA_PROSE}

Do NOT post anything — do not run `gh pr review` or comment anywhere; emit \
the JSON object on stdout and stop. shipit validates the calibrated result \
and {settle}."""


def parse_calibration(
    payload: Mapping[str, object], union: Sequence[Mapping[str, object]]
) -> CalibrationResult:
    """Validate a calibrator ``payload`` against the ``union`` it judged, indexed by candidate id."""
    if not isinstance(payload, Mapping):
        raise CalibrationContractError(
            f"calibrator output must be a JSON object, got {type(payload).__name__}"
        )
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise CalibrationContractError(
            "calibrator output has no 'findings' array — the judged output "
            "must carry every candidate's disposition"
        )
    summary = payload.get("summary")
    overall = ""
    if isinstance(summary, Mapping):
        raw_overall = summary.get("overall_feedback")
        overall = raw_overall if isinstance(raw_overall, str) else ""

    valid_ids = set(range(len(union)))
    seen: dict[int, str] = {}  # id -> how it was covered ("entry" / "merged")

    def _cover(candidate_id: object, how: str) -> int:
        if isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
            raise CalibrationContractError(
                f"calibrator {how} id must be an integer candidate id, "
                f"got {candidate_id!r}"
            )
        if candidate_id not in valid_ids:
            raise CalibrationContractError(
                f"calibrator judged finding id {candidate_id}, which is not in "
                f"the input union (ids 0..{len(union) - 1}) — the calibrator "
                "never originates findings"
            )
        if candidate_id in seen:
            raise CalibrationContractError(
                f"calibrator judged finding id {candidate_id} more than once "
                f"(as {seen[candidate_id]} and again as {how})"
            )
        seen[candidate_id] = how
        return candidate_id

    entries: list[CalibratedFinding] = []
    duplicate_of: dict[int, int] = {}
    for raw in raw_findings:
        if not isinstance(raw, Mapping):
            raise CalibrationContractError(
                f"calibrator findings entries must be objects, got {raw!r}"
            )
        entry_id = _cover(raw.get("id"), "entry")
        raw_merged = raw.get("merged")
        if raw_merged is None:
            raw_merged = []
        if not isinstance(raw_merged, list):
            raise CalibrationContractError(
                f"calibrator 'merged' must be an array of candidate ids, "
                f"got {raw_merged!r} (finding id {entry_id})"
            )
        merged = tuple(_cover(m, "merged") for m in raw_merged)
        for merged_id in merged:
            duplicate_of[merged_id] = entry_id

        disposition_token = raw.get("disposition")
        try:
            disposition = Disposition(disposition_token)
        except ValueError:
            known = ", ".join(d.value for d in Disposition)
            raise CalibrationContractError(
                f"calibrator finding id {entry_id} has disposition "
                f"{disposition_token!r}; every judged finding needs one of: {known}"
            ) from None

        candidate = union[entry_id]
        severity = parse_severity(raw.get("severity")) or DEFAULT_SEVERITY
        text = _text_or(raw.get("text"), candidate.get("text"))
        evidence = _text_or(raw.get("evidence"), candidate.get("evidence"))
        fix = _text_or(raw.get("fix"), candidate.get("fix"))
        if disposition is Disposition.POST and not evidence.strip():
            # An unevidenced post is an unverified finding.
            disposition = Disposition.DROP_UNVERIFIED
        line = candidate.get("line")
        confidence = candidate.get("confidence")
        entries.append(
            CalibratedFinding(
                id=entry_id,
                finding=Finding(
                    severity=severity,
                    text=text,
                    file=str(candidate.get("file") or ""),
                    line=line if isinstance(line, int) else None,
                    category=str(candidate.get("category") or ""),
                    confidence=(
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool)
                        else None
                    ),
                    evidence=evidence,
                    fix=fix,
                ),
                disposition=disposition,
                merged=merged,
            )
        )

    missing = sorted(valid_ids - set(seen))
    if missing:
        raise CalibrationContractError(
            f"calibrator output is missing candidate id(s) {missing} — every "
            "judged finding needs a disposition; none may be silently dropped"
        )

    # The duplicates appended below are never merge targets, so this stays complete.
    canonical_by_id = {e.id: e for e in entries}
    for merged_id, canonical_id in duplicate_of.items():
        canonical = canonical_by_id[canonical_id]
        candidate = union[merged_id]
        line = candidate.get("line")
        confidence = candidate.get("confidence")
        entries.append(
            CalibratedFinding(
                id=merged_id,
                finding=Finding(
                    severity=canonical.finding.severity,
                    text=str(candidate.get("text") or ""),
                    file=str(candidate.get("file") or ""),
                    line=line if isinstance(line, int) else None,
                    category=str(candidate.get("category") or ""),
                    confidence=(
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool)
                        else None
                    ),
                    evidence=str(candidate.get("evidence") or ""),
                    fix=str(candidate.get("fix") or ""),
                ),
                disposition=canonical.disposition,
                duplicate_of=canonical_id,
            )
        )

    return CalibrationResult(overall_feedback=overall, entries=tuple(entries))


def _text_or(value: object, fallback: object) -> str:
    """The judged string field, else the union candidate's own."""
    if isinstance(value, str) and value.strip():
        return value
    return fallback if isinstance(fallback, str) else ""


@dataclass(frozen=True)
class CalibratorRun:
    result: CalibrationResult
    run_id: str
    task: str
    usage: TokenUsage
    reasoning: str | None


def run_calibrator(
    config: CalibratorConfig,
    union: Sequence[Mapping[str, object]],
    *,
    cwd: str,
    pr_number: int | None = None,
    commit_range: tuple[str, str] | None = None,
    launcher: launch.Runner | None = None,
    artifacts: RunArtifacts | None = None,
    correlation: Mapping[str, object] | None = None,
) -> CalibratorRun:
    sink = artifacts if artifacts is not None else RunArtifacts.disabled()
    identity = agent_backend.by_name(config.backend)
    if shutil.which(identity.binary) is None:
        raise BackendUnavailable(
            f"The calibrator backend {config.backend!r} requires the "
            f"{identity.binary!r} CLI on your PATH, but it was not found. "
            "Install it (and log it in), then re-run."
        )
    # Prompt bytes are variant-hashed, so plumbing would split arms on non-content.
    candidates = [{k: v for k, v in c.items() if k != "run_id"} for c in union]
    task = build_calibrator_task(
        json.dumps(candidates, indent=2),
        pr_number=pr_number,
        commit_range=commit_range,
    )
    adapter = _adapter_for(config)
    cmd = adapter.build_command(task, _CALIBRATOR_ROLE, read_only=True, cwd=cwd)
    deadline = float(parse_duration(config.timeout))
    sink.write_prompt(task)
    sink.record(argv=list(cmd), cwd=cwd, seam_deadline_s=deadline)
    start = time.monotonic()
    try:
        result = launch.launch(
            cmd,
            cwd=cwd,
            env=adapter.child_env(),
            timeout=deadline,
            runner=launcher,
        )
    except execrun.ExecError as exc:
        timed_out = exc.cause == execrun.CAUSE_TIMEOUT
        sink.write_streams(exc.stdout, exc.stderr)
        sink.record(
            duration_ms=int((time.monotonic() - start) * 1000),
            exit_code=None,
            timed_out=timed_out,
            error=str(exc),
        )
        if not timed_out:
            raise
        raise BackendError(
            f"the calibrator ({config.backend}) timed out — the launch seam "
            f"killed it at {deadline:.0f}s (configured calibrator timeout "
            f"{config.timeout})",
            raw=f"{exc.stdout}\n{exc.stderr}".strip(),
            timed_out=True,
        ) from exc
    sink.write_streams(result.stdout, result.stderr)
    sink.record(
        duration_ms=int((time.monotonic() - start) * 1000),
        exit_code=result.returncode,
        timed_out=False,
    )
    logger.debug(
        "calibrator (%s) raw output for pr#%s (%d chars):\n%s",
        config.backend,
        pr_number,
        len(result.stdout or ""),
        result.stdout or "",
        extra={**dict(correlation or {}), "pr": pr_number},
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if sink.dir is not None:
            # The path stays out of the GitHub-facing BackendError message.
            logger.warning(
                "the calibrator (%s) exited %d — full raw output at %s",
                config.backend,
                result.returncode,
                sink.dir,
                extra={**dict(correlation or {}), "pr": pr_number},
            )
        raise BackendError(
            f"the calibrator ({config.backend}) exited {result.returncode}: "
            f"{detail[:500]}",
            raw=f"{result.stdout}\n{result.stderr}".strip(),
        )
    payload, run_id, envelope_usage = _unwrap_output(
        result.stdout or "", backend=config.backend
    )
    sink.record(run_id=run_id)
    if envelope_usage.reported:
        usage = envelope_usage
    elif config.backend == "codex":
        usage = from_codex_stderr(result.stderr or "")
    else:
        usage = UNREPORTED
    return CalibratorRun(
        result=parse_calibration(payload, union),
        run_id=run_id,
        task=task,
        usage=usage,
        reasoning=adapter.reasoning,
    )


def _adapter_for(config: CalibratorConfig):
    """A fresh spawn adapter carrying ``config``'s model and, where the backend has one, its knobs."""
    if config.backend == "claude":
        return ClaudeAdapter(model=config.model, reasoning=config.reasoning)
    if config.backend == "codex":
        # A None model defers to the adapter's own default, never a literal here.
        if config.model is None:
            return CodexAdapter(reasoning=config.reasoning)
        return CodexAdapter(model=config.model, reasoning=config.reasoning)
    if config.backend == "antigravity":
        # agy has no reasoning knob, so the level is dropped, not stamped as applied.
        if config.model is None:
            return AntigravityAdapter(timeout=config.timeout)
        return AntigravityAdapter(model=config.model, timeout=config.timeout)
    return resolve_adapter(config.backend)


def _unwrap_output(stdout: str, *, backend: str) -> tuple[dict, str, TokenUsage]:
    """Parse a calibrator's stdout, bare or enveloped, into ``(payload, run_id, usage)``."""
    try:
        parsed = extract_json(stdout)
    except ValueError as exc:
        raise BackendError(
            f"the calibrator ({backend}) returned no parseable JSON",
            raw=stdout,
        ) from exc
    run_id = ""
    usage = UNREPORTED
    if "findings" not in parsed and isinstance(parsed.get("result"), str):
        session = parsed.get("session_id")
        run_id = str(session) if session else ""
        usage = from_claude_envelope(parsed)
        try:
            parsed = extract_json(parsed["result"])
        except ValueError as exc:
            raise BackendError(
                f"the calibrator ({backend}) result envelope carried no "
                "parseable JSON payload",
                raw=stdout,
            ) from exc
    return parsed, run_id or uuid.uuid4().hex, usage
