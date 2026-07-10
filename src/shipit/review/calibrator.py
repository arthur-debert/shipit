"""calibrator — the one fixed judge between dimension passes and the posted
review (RVW02-WS04, ADR-0045; CONTEXT.md "Calibrator").

The **Calibrator** takes the UNION of a reviewer's parallel **Dimension pass**
findings and: dedups (merging duplicates into one canonical finding),
adversarially verifies each finding with tier-appropriate evidence (quoted
evidence always; a concrete failure scenario for major-or-worse, a clear
rationale for minor/nit). Its verification floor is REPRODUCTION-based
(RVW02-WS08, F2 #665): a finding is DROPPED only when adversarial verification
actively REFUTES it — never merely because the judge is unsure — and a
reproducing finding is kept, never downgraded. It then normalizes **Severity**
onto the shared ladder, and assigns every
judged finding a **Disposition**. It NEVER originates findings — a judge that
also finds is a monolithic reviewer again, with the anchoring bias the fan-out
exists to remove.

Two deliberate constraints (ADR-0045) live here:

  * the calibrator is ONE fixed TABLE-LEVEL agent/model shared by every
    reviewer (:class:`CalibratorConfig`, default ``claude`` at high
    ReasoningLevel) — per-reviewer calibrators would fork the common severity
    ruler; and
  * its contract is enforced at the I/O BOUNDARY (:func:`parse_calibration`):
    schema-validated output, a disposition on every judged finding, and no
    finding absent from the input union — an out-of-range id (an originated
    finding), a doubly-judged id, or an unjudged union finding each raises
    :class:`CalibrationContractError` loud. The calibrator's *wisdom* is
    deliberately NOT tested or enforced (that is what the offline A/B harness
    measures); only its I/O contract is.

Two code-enforced routings ride the same boundary, deterministic rather than
prompt-trusted: a POST-disposition finding with NO quoted evidence is flipped
to ``drop-unverified`` (the verification floor — quoted evidence always), and
duplicates never post (only the canonical finding a duplicate merged into
does). Severity follows the domain fail-safe (:func:`~shipit.finding.parse_severity`
else ``major``): an unparseable severity forces a round rather than slipping
past the Breaker.

Launching (:func:`run_calibrator`) rides the SAME spawn seam as every other
agent launch (:mod:`shipit.spawn.backends` adapter + :func:`shipit.spawn.launch.launch`),
read-only in the shared Tree so the judge can verify evidence against the real
checkout. The ``claude`` result envelope (``--output-format json``) is
unwrapped here: its ``session_id`` becomes the calibrator's run id (a backend
with no envelope gets a minted id) and its ``usage`` block becomes the run's
measured token cost (RVW03-WS04 — captured at launch-result level; the round
record's ``round.runs`` entry carries it, no transcript join involved).
"""

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

#: The role the calibrator launches under — the read-only reviewer posture
#: (mirrors :data:`shipit.review.producer._REVIEWER_ROLE`): the judge reads the
#: checkout and the diff to verify evidence; it never edits and never posts.
_CALIBRATOR_ROLE = "reviewer"

#: The canonical `<N>s` duration shape a calibrator ``timeout`` carries — the
#: same shape as the Roster's per-reviewer ``timeout`` (whole seconds, ``s``
#: suffix).
_TIMEOUT_SHAPE = re.compile(r"^[1-9][0-9]*s$")


@dataclass(frozen=True)
class CalibratorConfig:
    """The table-level calibrator launch config — ONE value for every reviewer.

    ``backend`` is a spawn-adapter token (``claude`` / ``codex`` /
    ``antigravity``); ``model`` an optional verbatim/alias model id (``None`` →
    the backend's own default); ``reasoning`` the chosen
    :class:`~shipit.agent.invocation.ReasoningLevel` token, threaded to REAL
    argv where the backend carries a knob (RVW03-WS04, #685: ``claude
    --effort``; codex ``-c model_reasoning_effort``; agy has none) — the round
    record's calibrator run stamps the level the adapter ACTUALLY applied, so
    a knob-less backend records unset rather than echoing this config value;
    ``timeout`` the launch-seam process deadline (canonical
    ``<N>s``). The shipped default is the ADR-0045 decision: ``claude`` at
    ``high`` reasoning.

    Construction is validation (the Roster convention): a config that
    constructs is well-formed. MEMBERSHIP (is ``backend`` a real spawn
    backend?) is validated here too — the loader wraps the ``ValueError`` into
    its config error, so an unknown calibrator backend fails loud at load.
    """

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


#: The shipped default calibrator (ADR-0045): the ``claude`` backend at high
#: ReasoningLevel, the backend's own default model, the review path's default
#: 600s deadline.
DEFAULT_CALIBRATOR = CalibratorConfig()


class CalibrationContractError(RuntimeError):
    """The calibrator's output violated its I/O contract (RVW02-WS04).

    Raised by :func:`parse_calibration` when the output is not the documented
    shape, judges a finding absent from the input union (an ORIGINATED finding
    — the never-originates rule, enforced where checkable), judges a union
    finding twice, omits one (no disposition on a judged finding), or carries
    an unknown disposition. The fan-out treats it exactly like an unparseable
    backend: the round degrades loud (ADR-0006 — non-blocking), never posts a
    half-judged review.
    """


@dataclass(frozen=True)
class CalibratedFinding:
    """One judged union finding: the final domain Finding + its routing.

    ``id`` is the union index it judged; ``merged`` the union indices deduped
    INTO it (its duplicates — judged through it, never separately);
    ``duplicate_of`` is set on an entry that itself was merged away (the
    inverse edge, derived at parse so consumers need no second lookup). A
    merged-away entry never posts regardless of disposition.
    """

    id: int
    finding: Finding
    disposition: Disposition
    merged: tuple[int, ...] = ()
    duplicate_of: int | None = None


@dataclass(frozen=True)
class CalibrationResult:
    """The calibrator's validated output: the judged findings + its summary."""

    overall_feedback: str
    entries: tuple[CalibratedFinding, ...]


#: Prose schema for the calibrator's output — described in-prose for every
#: backend (``claude`` and ``agy`` have no native schema flag; keeping one
#: presentation keeps the parse boundary single).
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


def build_calibrator_task(candidates_json: str, pr_number: int) -> str:
    """Compose the calibrator task: judge ``candidates_json`` for PR ``pr_number``.

    The judge contract (ADR-0045, its verification floor amended by
    RVW02-WS08/F2 #665): never originate; dedup by
    merging (``merged`` ids); adversarially verify with tier-appropriate
    evidence (quoted evidence always; a concrete failure scenario for
    major-or-worse, a clear rationale for minor/nit). The verification floor is
    REPRODUCTION-based (RVW02-WS08, F2 #665): a finding is dropped
    ``drop-unverified`` only when it is actively REFUTED (misquoted evidence,
    code that does not behave as claimed, a failure that cannot occur), never
    merely because the judge is unsure or cannot phrase a perfect rationale — a
    finding that reproduces is kept, never downgraded. Route pre-existing / beyond-diff
    findings ``out-of-scope``; normalize severity on the merge-block ruler;
    cover EVERY candidate id exactly once (own ``id`` or another entry's
    ``merged``). ``candidates_json`` is the union as a JSON array of
    ``{id, dimension, file, line, severity, category, confidence, text,
    evidence, fix}`` objects; the task embeds it whole — the judge reads the
    checkout + ``gh pr diff`` for the ground truth it verifies against.
    """
    return f"""\
You are the review CALIBRATOR: the single judge of candidate code-review \
findings. You are running in a shared, READ-ONLY checkout of pull request \
#{pr_number}'s head commit. Parallel dimension-scoped review passes produced \
the candidate findings below; your job is to turn that raw union into the one \
calibrated result that gets posted.

FIRST, get the ground truth: run `gh pr diff {pr_number}` to read the pull \
request's unified diff (it uses the PR's ACTUAL base and head — do NOT assume \
the base is `main`). Read the surrounding code in this checkout wherever you \
need context to judge a candidate.

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
4. Route scope: a verified finding that is beyond this PR's diff — a \
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
nit). In "summary.overall_feedback", give the posted review's summary \
paragraph.

{_CALIBRATION_SCHEMA_PROSE}

Do NOT post anything — do not run `gh pr review` or comment on the PR; emit \
the JSON object on stdout and stop. shipit validates and posts the calibrated \
result."""


def parse_calibration(
    payload: Mapping[str, object], union: Sequence[Mapping[str, object]]
) -> CalibrationResult:
    """Validate a calibrator output ``payload`` against the input ``union`` —
    the contract's I/O boundary. PURE.

    ``union`` is the candidate list the task embedded (index == candidate id);
    ``payload`` the JSON object the calibrator emitted. Enforces the RVW02-WS04
    contract loud (:class:`CalibrationContractError`): the documented shape, a
    known disposition on every judged finding, and EXACT union coverage — every
    candidate id exactly once across entry ``id``\\ s and ``merged`` lists, no
    id outside the union (never-originates, enforced where checkable).

    Fail-safe coercions (never violations, the domain conventions): an
    unparseable severity lands on ``major`` (forces a round rather than
    slipping the Breaker); a blank judged ``text``/``evidence``/``fix`` falls
    back to the union candidate's own. One deterministic routing is applied
    HERE, not trusted to the prompt: a ``post`` entry whose evidence is empty
    after fallback is flipped to ``drop-unverified`` — quoted evidence always
    is the verification floor. Location/category/confidence always come from
    the union candidate (the calibrator judges; it does not relocate).
    """
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
            # The verification floor, code-enforced: "quoted evidence always".
            # An unevidenced post IS an unverified finding — routed out,
            # retained in the record, never posted.
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

    # Materialize the inverse dedup edge: each merged-away candidate becomes a
    # judged entry of its own (the canonical twin's severity/disposition, its
    # OWN location/text from the union) so the round record retains every
    # union finding with an honest routing — merged-away entries never post.
    # Index the canonicals by id once (every canonical is already appended; the
    # duplicates this loop appends are never merge targets) so the lookup is O(1).
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
    """The judged string field, else the union candidate's own — a blank/absent
    calibrator field never erases what the pass reported."""
    if isinstance(value, str) and value.strip():
        return value
    return fallback if isinstance(fallback, str) else ""


@dataclass(frozen=True)
class CalibratorRun:
    """One calibrator launch's full capture (RVW03-WS04): the validated
    calibration plus the launch's measurements.

    ``run_id`` is the claude envelope's ``session_id`` when the backend yields
    one, else a minted uuid hex; ``task`` the exact prompt that ran (the caller
    variant-hashes it for the round record); ``usage`` the launch's token cost
    as the CLI reported it (claude: the envelope's ``usage`` block; codex: the
    stderr figure; agy: explicitly unreported); ``reasoning`` the ReasoningLevel
    the adapter ACTUALLY wired into argv (``None`` = no knob applied — the
    record stamps this, never ``config.reasoning``).
    """

    result: CalibrationResult
    run_id: str
    task: str
    usage: TokenUsage
    reasoning: str | None


def run_calibrator(
    config: CalibratorConfig,
    union: Sequence[Mapping[str, object]],
    *,
    pr_number: int,
    cwd: str,
    launcher: launch.Runner | None = None,
    artifacts: RunArtifacts | None = None,
    correlation: Mapping[str, object] | None = None,
) -> CalibratorRun:
    """Launch the calibrator over ``union`` in the shared Tree at ``cwd`` and
    return its :class:`CalibratorRun`.

    The launch rides the shared spawn seam (adapter argv + auth-env scrub +
    :func:`shipit.spawn.launch.launch` under the ``config.timeout`` process
    deadline) with the read-only reviewer posture — the judge verifies evidence
    against the real checkout but can neither edit nor post. ``config.reasoning``
    reaches real argv where the backend has a knob (:func:`_adapter_for`), and
    the returned ``reasoning`` is what was actually applied (RVW03-WS04).

    ``artifacts`` (RVW03-WS02) is the judge run's fail-open bundle: the exact
    task text is written before the launch, the RAW stdout/stderr + launch meta
    (exit code, duration, timed-out flag, and — once unwrapped — the true run
    id) after, on every path — so a calibrator failure (previously only
    ``str(exc)``) leaves its full raw output inspectable on disk.

    ``correlation`` (RVW03-WS02) is the fan-out's per-pass log-correlation extras
    (``reviewer``/``round_id``/the stable surrogate ``run_id`` = ``calibrator``);
    the raw-output DEBUG record carries them so ``shipit logs --run calibrator``
    can slice the judge's trail alongside the round's progress events.

    Raises :class:`~shipit.review.backends.BackendUnavailable` (CLI missing),
    :class:`~shipit.review.backends.BackendError` (a launch-seam timeout / a
    nonzero child / unparseable output — carrying the raw for the salvage
    conventions), or :class:`CalibrationContractError` (parseable output that
    violates the judge contract) — the fan-out maps each to a degraded,
    non-blocking round (ADR-0006).
    """
    sink = artifacts if artifacts is not None else RunArtifacts.disabled()
    identity = agent_backend.by_name(config.backend)
    if shutil.which(identity.binary) is None:
        raise BackendUnavailable(
            f"The calibrator backend {config.backend!r} requires the "
            f"{identity.binary!r} CLI on your PATH, but it was not found. "
            "Install it (and log it in), then re-run."
        )
    # The union candidates carry pass-plumbing the judge must not see (the
    # RVW03-WS02 ``run_id`` correlation is the record's business, not part of
    # the judged content — and prompt bytes are variant-hashed, so plumbing in
    # the task would split experiment arms on non-content):
    # serialize exactly the documented candidate shape.
    candidates = [{k: v for k, v in c.items() if k != "run_id"} for c in union]
    task = build_calibrator_task(json.dumps(candidates, indent=2), pr_number=pr_number)
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
    # The judge's raw output at DEBUG (issue #681 item 2) — the passes get this
    # via parse_review_output; the calibrator's parse (`_unwrap_output`) now has
    # the same durable raw trail in the log sink, on top of the bundle on disk.
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
            # LOCAL breadcrumb to the full raw on disk; the absolute path stays
            # OUT of the BackendError message, which the service surfaces in the
            # GitHub-facing funnel check summary (no user-home / state leak).
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
    sink.record(run_id=run_id)  # RVW03-WS02: the true run id into the bundle meta
    if envelope_usage.reported:
        usage = envelope_usage  # the claude envelope's own usage block
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
    """The spawn :class:`~shipit.spawn.backends.base.BackendAdapter` instance
    carrying ``config``'s model, its reasoning level where the backend has a
    knob (RVW03-WS04), and, for agy, its timeout — a fresh per-run adapter
    exactly like the producer builds per reviewer; the registry default is used
    only for an unlisted-but-registered backend (whose knobs we don't know)."""
    if config.backend == "claude":
        return ClaudeAdapter(model=config.model, reasoning=config.reasoning)
    if config.backend == "codex":
        # config.model None → the adapter's own backend-defined default, never a
        # duplicated model literal here that could drift from the backend identity.
        if config.model is None:
            return CodexAdapter(reasoning=config.reasoning)
        return CodexAdapter(model=config.model, reasoning=config.reasoning)
    if config.backend == "antigravity":
        # agy has NO reasoning knob (probed 1.1.1): the config level is dropped
        # here so the record stamps unset, never an echoed unapplied value.
        # A None model defers to the adapter's own default (same non-duplication).
        if config.model is None:
            return AntigravityAdapter(timeout=config.timeout)
        return AntigravityAdapter(model=config.model, timeout=config.timeout)
    # CalibratorConfig construction already validated backend membership; an
    # unlisted-but-registered backend falls back to its registry default.
    return resolve_adapter(config.backend)


def _unwrap_output(stdout: str, *, backend: str) -> tuple[dict, str, TokenUsage]:
    """Parse a calibrator's stdout into ``(payload, run_id, usage)``.

    ``claude -p --output-format json`` wraps its answer in a result envelope
    (``{"result": "<text>", "session_id": …, "usage": …}``); the payload is
    extracted from the envelope's ``result`` text, the ``session_id`` becomes
    the run id, and the envelope's ``usage`` block becomes the launch's
    measured token usage (RVW03-WS04 — captured at launch-result level, no
    transcript join). Any other shape (codex / agy, or a claude run that
    emitted the object bare) parses directly, mints a uuid run id, and reports
    usage :data:`~shipit.review.usage.UNREPORTED` (the caller may still read a
    non-envelope source, e.g. codex's stderr figure). Unparseable output raises
    :class:`~shipit.review.backends.BackendError` with the raw attached.
    """
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
