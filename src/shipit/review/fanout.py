"""fanout — round-1 dimension fan-out + union post (RVW02-WS04/WS08, ADR-0045).

The orchestration between the review producer and the posting service: a
local-agent reviewer's detached review run no longer makes one monolithic
"find everything" pass — it fans out into parallel **Dimension passes**
(:mod:`shipit.review.dimensions`) on the reviewer's own backend against ONE
shared read-only Tree, unions the results, and posts them.

By DEFAULT (RVW02-WS08) the union is posted through a MECHANICAL, deterministic
dedup (:func:`dedup_union`): findings sharing a ``(file, line, claim)`` merge
into one canonical that posts with its OWN pass-assigned severity — no LLM judge
in the default path. The WS05/F2 baseline (#638, #665) showed the LLM
**Calibrator** (:mod:`shipit.review.calibrator`) net-negative on round-1 major
recall (it refuted a true major the passes found, dragging recall below the
single-pass baseline), so it is OPTIONAL and OFF by default. It is kept warm —
concept, config, hooks, and the F2 reproduction-based floor all wired but
dormant (the ADR-0044 ``classify`` pattern) — and it is opted back on by setting
the table-level ``[reviewers].calibrator`` key (one shared judge, not a
per-reviewer entry); when on it dedups,
adversarially verifies, normalizes severity onto the one ruler, and assigns
every judged finding a **Disposition**.

What this module owns:

  * the pass fan-out (provision the Tree once, launch the configured
    dimension set in parallel through :func:`shipit.review.producer.run_tree_review`,
    tolerate per-pass failures — a pass failure degrades coverage, it never
    kills the round unless EVERY pass failed);
  * the union (each successful pass's comments, coerced through the ONE trust
    boundary :func:`shipit.review.schema.finding_from_dict`, tagged with the
    dimension that found them) and the merged coverage attestation;
  * the default MECHANICAL dedup (:func:`dedup_union`) that merges same-location
    same-claim candidates into one canonical carrying its pass severity — the
    off-path replacement for the LLM judge;
  * the deterministic post routing (:func:`route_calibrated`), shared by both
    paths: duplicates never post, round-1 nits post under the TABLE-LEVEL nit
    cap (over-cap nits flip to ``nit-suppressed``, recorded; ``0`` floors the
    posted review at minor), the posted status derives from what posts
    (major-or-worse → ``REQUEST_CHANGES``); and
  * the round's contributing-run trail: one entry per pass (and, when the
    calibrator is on, one for it), each with a run id and the **Variant** hash
    of the exact prompt that ran — what the review-round record's ``round.runs``
    carries and ``shipit eval report`` joins on (WS03).

The fan-out is INVISIBLE below the reviewer boundary (ADR-0045): the service
posts ONE review through the reviewer's own bot exactly as before; the funnel,
reconcile, and prstate machinery are untouched. An EMPTY union has nothing to
post — it skips both the dedup and the (dormant) calibrator and posts the
attested clean review.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .. import events
from ..agent.backend import Backend
from ..finding import (
    DEFAULT_SEVERITY,
    Disposition,
    Finding,
    JudgedFinding,
    Severity,
    parse_severity,
)
from ..harness.eval.variant import label_from_env, variant_of
from ..spawn import launch
from . import producer
from .calibrator import (
    CalibratedFinding,
    CalibratorConfig,
    run_calibrator,
)
from .dimensions import Dimension, known_dimension_names, resolve_dimensions
from .schema import finding_from_dict

logger = logging.getLogger("shipit.review")


@dataclass(frozen=True)
class FanoutOutcome:
    """One fan-out round's product, ready for the service seam.

    ``review`` is the routed REVIEW_SCHEMA-shaped dict the posting path
    consumes unchanged (the fan-out's invisibility below the reviewer
    boundary) — the deduped union by default, or the calibrated result when the
    dormant judge is on; ``findings`` is the FULL judged set as
    :class:`JudgedFinding`\\ s
    — routed-out findings AND merged-away duplicates included, never erased (the
    round record's Opportunity-harvest seam); ``runs`` the contributing-run
    entries (every pass + the calibrator, run ids + variant hashes) for
    ``round.runs``.
    """

    review: dict
    findings: tuple[JudgedFinding, ...]
    runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _PassResult:
    """One dimension pass's outcome: its run entry + the review it captured
    (``None`` when the pass failed — the run entry carries the why)."""

    dimension: Dimension
    run: dict[str, Any]
    review: dict | None


#: The shipped cheaper **ReasoningLevel** an INCREMENTAL round's single pass runs
#: at (RVW02-WS06, ADR-0045). Round 1 is exhaustive; rounds after it review only
#: the fix range, so they run cheaper. This is a RECORD-only constant, not an argv
#: flag (no CLI carries a reasoning knob) — it is stamped on the incremental pass's
#: run entry so the review-round record shows the round ran at the cheaper level.
#: :func:`run_fanout_review` takes it as an argument (defaulting here), but no
#: ``[reviewers]`` config key wires it and the service never overrides the default;
#: moving it today means changing this constant.
DEFAULT_INCREMENTAL_REASONING = "low"

#: The synthetic **Dimension** an INCREMENTAL round's single pass carries so it
#: flows through the SAME union / coverage / attestation machinery as a round-1
#: dimension pass (RVW02-WS06). A round after the first is ONE full-scope pass over
#: the fix range, NOT a dimension fan-out — so this is not a member of the closed
#: :data:`shipit.review.dimensions.DIMENSIONS` registry; it exists only to label the
#: incremental pass in the record and attestation.
_INCREMENTAL_DIMENSION = Dimension(
    name="incremental",
    title="Incremental fix-range",
    focus="the fix range only, with mandatory dependency-neighborhood context",
)


def run_fanout_review(
    backend: Backend,
    ctx,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    nit_cap: int | None = None,
    incremental: bool = False,
    incremental_reasoning: str = DEFAULT_INCREMENTAL_REASONING,
    dry_run: bool = False,
    launcher: launch.Runner | None = None,
) -> FanoutOutcome:
    """Fan ``backend``'s review of ``ctx`` out into dimension passes (round 1), or
    run ONE incremental fix-range pass (round ≥ 2), and return the routed
    :class:`FanoutOutcome`.

    By DEFAULT (``calibrator=None``, RVW02-WS08) the union is posted through the
    MECHANICAL dedup (:func:`dedup_union`) using each pass's OWN severity — no
    model run. A ``calibrator`` :class:`CalibratorConfig` opts the dormant LLM
    judge back on (the WS05/F2 baseline found it net-negative on major recall,
    #638/#665), routing the union through :func:`run_calibrator` instead.

    ``incremental`` (RVW02-WS06, ADR-0045) selects the round-≥2 shape: ONE
    full-scope pass over the FIX RANGE — ``ctx.base_sha..ctx.head_sha``, where
    ``ctx`` is the caller's fix-range-rescoped view
    (:func:`shipit.review.diff.rescoped_view`) — with mandatory
    dependency-neighborhood context, INSTEAD of the parallel dimension fan-out.
    The pass runs at ``incremental_reasoning`` (the cheaper level, stamped on its
    run entry) and NEW NITS ARE SUPPRESSED: the routing runs with an effective
    ``nit_cap`` of ``0``, so every fresh nit routes ``nit-suppressed`` (recorded,
    not posted) — a late round can't be recolonized by style churn. The
    calibrator, if configured, still runs (single-pass + calibrator). Round 1
    (``incremental=False``) is unchanged: the ``dimensions`` fan-out with the
    table-level ``nit_cap``.

    ``dimensions`` names the reviewer's configured pass set (the per-reviewer
    Roster option; ``None``/empty → the shipped default set), used only in round
    1; ``calibrator`` the table-level judge config (``None`` → judge OFF, deduped
    union); ``nit_cap`` the table-level round-1 nit budget (``None`` → uncapped,
    ``0`` → floor at minor; IGNORED in an incremental round, which forces ``0``).
    ``model`` / ``timeout`` / ``instructions_path`` are the reviewer's own run
    options and apply to every pass, exactly as they applied to the monolithic
    run.

    Failure posture: a SINGLE pass failure is tolerated — its run entry records
    the outcome, the posted summary attests the degraded coverage; ALL passes
    failing raises ``RuntimeError`` (the service maps it to the ``failed``
    funnel outcome; in an incremental round the sole pass failing IS all passes
    failing). When the judge is ON, a calibrator failure (unavailable /
    timed out / unparseable / contract-violating output) PROPAGATES — an
    uncalibrated union is never posted under the judge's ruler; the round
    degrades non-blocking exactly like a failed monolithic review (ADR-0006).
    The default dedup path is pure and cannot fail this way.

    With ``dry_run=True``: prints each pass's would-run argv (one per
    dimension, or the single incremental pass, no clone, no model bill) plus a
    note on how the union would be posted (mechanical dedup, or the configured
    calibrator), and returns an empty outcome — the same honest dry-run contract
    as the producer's.
    """
    incremental_range: tuple[str, str] | None = None
    if incremental:
        incremental_range = (str(ctx.base_sha), str(ctx.head_sha))
        dims = (_INCREMENTAL_DIMENSION,)
        effective_nit_cap = 0
    else:
        try:
            dims = resolve_dimensions(dimensions)
        except KeyError as exc:
            raise ValueError(
                f"unknown review dimension {exc.args[0]!r} — known dimensions: "
                f"{', '.join(known_dimension_names())}"
            ) from None
        effective_nit_cap = nit_cap
    agent = backend.funnel_agent or backend.name

    if dry_run:
        for dim in dims:
            producer.run_tree_review(
                backend,
                ctx,
                model=model,
                timeout=timeout,
                instructions_path=instructions_path,
                dry_run=True,
                dimension=None if incremental else dim,
                incremental_range=incremental_range,
            )
        if calibrator is None:
            print(
                "(dry-run: calibrator OFF — would post the mechanically-deduped "
                "union using each pass's own severity)"
            )
        else:
            print(
                f"(dry-run: would calibrate the union with {calibrator.backend} "
                f"[model={calibrator.model or 'default'}, "
                f"reasoning={calibrator.reasoning}])"
            )
        return FanoutOutcome(
            review={
                "summary": {"status": "COMMENT", "overall_feedback": "(dry-run)"},
                "comments": [],
            },
            findings=(),
            runs=(),
        )

    tree_path = producer.provision_review_tree(ctx)
    label = label_from_env()

    def _one_pass(dim: Dimension) -> _PassResult:
        task = producer.pass_task_text(
            backend,
            ctx.number,
            instructions_path=instructions_path,
            dimension=None if incremental else dim,
            incremental_range=incremental_range,
        )
        run: dict[str, Any] = {
            "run_id": uuid.uuid4().hex,
            "kind": "incremental-pass" if incremental else "dimension-pass",
            "dimension": dim.name,
            "backend": agent,
            "model": model,
            "variant": variant_of(task, label=label).as_record(),
        }
        if incremental:
            # The cheaper reasoning is config + RECORD only (no CLI knob) — stamp
            # it on the run entry so the round record shows the level it ran at.
            run["reasoning"] = incremental_reasoning
            run["range"] = {"base": incremental_range[0], "head": incremental_range[1]}
        start = time.monotonic()
        try:
            review = producer.run_tree_review(
                backend,
                ctx,
                model=model,
                timeout=timeout,
                instructions_path=instructions_path,
                launcher=launcher,
                dimension=None if incremental else dim,
                tree_path=tree_path,
                incremental_range=incremental_range,
            )
        except Exception as exc:  # noqa: BLE001 - a pass failure degrades, never kills
            run["duration_ms"] = int((time.monotonic() - start) * 1000)
            run["outcome"] = (
                "timed_out" if getattr(exc, "timed_out", False) else "failed"
            )
            run["detail"] = str(exc)[:500]
            logger.warning(
                "%s pass %s failed for pr#%s (agent=%s) — coverage degrades, "
                "the round continues",
                "incremental" if incremental else "dimension",
                dim.name,
                ctx.number,
                agent,
                exc_info=True,
                extra={"pr": ctx.number, "reviewer": agent},
            )
            return _PassResult(dimension=dim, run=run, review=None)
        run["duration_ms"] = int((time.monotonic() - start) * 1000)
        run["outcome"] = "success"
        run["findings"] = len(review.get("comments") or [])
        return _PassResult(dimension=dim, run=run, review=review)

    with ThreadPoolExecutor(max_workers=len(dims)) as pool:
        results = list(pool.map(_one_pass, dims))

    runs = [r.run for r in results]
    succeeded = [r for r in results if r.review is not None]
    failed = [r for r in results if r.review is None]
    if not succeeded:
        details = "; ".join(
            f"{r.dimension.name}: {r.run.get('detail', 'failed')}" for r in failed
        )
        kind = (
            "the incremental pass failed"
            if incremental
            else f"all {len(dims)} dimension passes failed"
        )
        raise RuntimeError(f"{kind} for pr#{ctx.number} (agent={agent}) — {details}")

    union = _build_union(succeeded)
    coverage = _merge_coverage(succeeded)

    calibrated = calibrator is not None
    if not union:
        # Nothing to post: neither the mechanical dedup nor the (dormant, never
        # originating) calibrator has anything to do with an empty union. Post
        # the attested clean review.
        review = {
            "summary": {
                "status": "COMMENT" if failed else "APPROVED",
                "overall_feedback": _attestation(
                    dims,
                    failed,
                    union_size=0,
                    entries=(),
                    posted=0,
                    calibrated=calibrated,
                ),
                "coverage": coverage,
            },
            "comments": [],
        }
        return FanoutOutcome(review=review, findings=(), runs=tuple(runs))

    if calibrator is None:
        # DEFAULT (RVW02-WS08): post the MECHANICALLY-deduped union using each
        # pass's own severity — no model run, no LLM judge.
        entries = dedup_union(union)
        feedback = ""
    else:
        # Dormant judge opted back on: route the union through the calibrator.
        calibrator_run: dict[str, Any] = {
            "kind": "calibrator",
            "backend": calibrator.backend,
            "model": calibrator.model,
            "reasoning": calibrator.reasoning,
        }
        start = time.monotonic()
        result, run_id, task = run_calibrator(
            calibrator,
            union,
            pr_number=ctx.number,
            cwd=tree_path,
            launcher=launcher,
        )
        calibrator_run.update(
            {
                "run_id": run_id,
                "duration_ms": int((time.monotonic() - start) * 1000),
                "outcome": "success",
                "judged": len(result.entries),
                "variant": variant_of(task, label=label).as_record(),
            }
        )
        runs.append(calibrator_run)
        entries = result.entries
        feedback = result.overall_feedback.strip()

    routed = route_calibrated(entries, nit_cap=effective_nit_cap)
    findings = tuple(
        JudgedFinding(entry.finding, d, entry.duplicate_of) for entry, d in routed
    )
    posted_entries = [judged for judged in findings if judged.posted]
    comments = [_comment_dict(judged.finding) for judged in posted_entries]
    posted = len(comments)
    status = _derive_status(
        (judged.finding for judged in posted_entries), degraded=bool(failed)
    )
    attestation = _attestation(
        dims,
        failed,
        union_size=len(union),
        entries=findings,
        posted=posted,
        calibrated=calibrated,
    )
    review = {
        "summary": {
            "status": status,
            "overall_feedback": (
                f"{feedback}\n\n{attestation}" if feedback else attestation
            ),
            "coverage": coverage,
        },
        "comments": comments,
    }

    events.emit(
        logger,
        "review.calibrated" if calibrated else "review.deduped",
        "%s completed for pr#%s (agent=%s): %d candidate(s) -> %d posted",
        "calibration" if calibrated else "mechanical dedup",
        ctx.number,
        agent,
        len(union),
        posted,
        extra={
            "pr": ctx.number,
            "reviewer": agent,
            "candidates": len(union),
            "posted": posted,
        },
    )
    for judged in findings:
        if judged.posted:
            continue
        finding = judged.finding
        events.emit(
            logger,
            "finding.dispositioned",
            "finding routed out on pr#%s: %s (%s) -> %s",
            ctx.number,
            finding.file or "(no file)",
            finding.severity.value,
            judged.disposition.value,
            extra={
                "pr": ctx.number,
                "reviewer": agent,
                "severity": finding.severity.value,
                "disposition": judged.disposition.value,
            },
        )

    return FanoutOutcome(review=review, findings=findings, runs=tuple(runs))


def route_calibrated(
    entries: Sequence[CalibratedFinding], *, nit_cap: int | None
) -> tuple[tuple[CalibratedFinding, Disposition], ...]:
    """The deterministic post routing, shared by both paths (the calibrator's
    judged entries and the mechanical :func:`dedup_union`). PURE.

    Two policies the CODE enforces rather than the judge (deterministic, so
    they are testable and prompt-drift-proof):

      * DUPLICATES NEVER POST — an entry merged into a canonical twin
        (``duplicate_of`` set) shares the twin's FINAL disposition (including a
        nit-cap flip applied to the twin — it IS the same underlying finding, and
        its substance reaches the PR through the twin) but is never emitted as a
        second posted comment; and
      * the ROUND-1 NIT CAP — among post-disposition canonical findings, nits
        beyond ``nit_cap`` flip to ``nit-suppressed`` (recorded, not posted;
        severity order keeps the first-``nit_cap`` strongest-ordered nits).
        ``None`` = uncapped; ``0`` = floor at minor (no nit posts).

    Returns EVERY judged finding (canonical + merged-away duplicates) with its
    FINAL disposition, ordered highest severity first — the exact set the round
    record persists (routed-out findings ride along, never erased). A finding
    POSTS iff its final disposition is ``post`` AND it is canonical
    (``duplicate_of is None``).
    """
    ordered = sorted(entries, key=lambda e: e.finding.severity.rank)
    nits_posted = 0
    routed: list[tuple[CalibratedFinding, Disposition]] = []
    final_disposition_for: dict[int, Disposition] = {}
    for entry in ordered:
        disposition = entry.disposition
        if entry.duplicate_of is not None:
            # A merged-away duplicate shares its canonical twin's FINAL
            # disposition — including a nit-cap flip applied to the twin below.
            # Canonical-before-duplicate ordering is guaranteed: parse_calibration
            # appends duplicates after all canonicals carrying the canonical's
            # severity, and the severity sort is stable, so the twin is seen first.
            disposition = final_disposition_for[entry.duplicate_of]
        elif (
            disposition is Disposition.POST
            and entry.finding.severity is Severity.NIT
            and nit_cap is not None
        ):
            if nits_posted >= nit_cap:
                disposition = Disposition.NIT_SUPPRESSED
            else:
                nits_posted += 1
        if entry.duplicate_of is None:
            final_disposition_for[entry.id] = disposition
        routed.append((entry, disposition))
    return tuple(routed)


def dedup_union(
    union: Sequence[Mapping[str, Any]],
) -> tuple[CalibratedFinding, ...]:
    """Mechanically dedup the pass ``union`` into judged entries — the DEFAULT
    round-1 path (RVW02-WS08, calibrator off). PURE, no model.

    Candidates sharing a ``(file, line, claim)`` key — where ``claim`` is the
    finding text whitespace-collapsed and case-folded — are ONE underlying
    finding: the group's most-severe member (ties → lowest union id) becomes the
    canonical (disposition ``post``, its group-mates listed in ``merged``); each
    other member becomes a merged-away duplicate (``duplicate_of`` the canonical,
    carrying the canonical's severity like :func:`~shipit.review.calibrator.parse_calibration`
    materializes its inverse edge) so the round record retains every union
    finding. The canonical keeps its OWN pass-assigned severity — there is no
    judge to renormalize onto a common ruler, and the whole point of the off
    path is to trust the passes' severities. Nothing is ever DROPPED here:
    mechanical dedup only merges duplicates; a candidate's substance always
    reaches the record (and, unless a nit-cap flip in :func:`route_calibrated`
    suppresses it, the PR).

    Every entry is disposition ``post`` — the nit cap and the duplicates-never-
    post rule are applied downstream by :func:`route_calibrated`, exactly as for
    the calibrator's entries. Canonicals are emitted first (in first-seen group
    order), then their duplicates, so the stable severity sort in
    :func:`route_calibrated` always sees a canonical before its duplicate.
    """
    groups: dict[tuple[str, int | None, str], list[Mapping[str, Any]]] = {}
    order: list[tuple[str, int | None, str]] = []
    for candidate in union:
        key = _dedup_key(candidate)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)

    entries: list[CalibratedFinding] = []
    for key in order:
        members = groups[key]
        canonical = min(
            members,
            key=lambda c: (
                (parse_severity(c.get("severity")) or DEFAULT_SEVERITY).rank,
                _candidate_id(c),
            ),
        )
        canonical_finding = _finding_from_candidate(canonical)
        merged = tuple(_candidate_id(c) for c in members if c is not canonical)
        entries.append(
            CalibratedFinding(
                id=_candidate_id(canonical),
                finding=canonical_finding,
                disposition=Disposition.POST,
                merged=merged,
            )
        )
        for member in members:
            if member is canonical:
                continue
            entries.append(
                CalibratedFinding(
                    id=_candidate_id(member),
                    finding=_finding_from_candidate(
                        member, severity=canonical_finding.severity
                    ),
                    disposition=Disposition.POST,
                    duplicate_of=_candidate_id(canonical),
                )
            )
    return tuple(entries)


def _dedup_key(candidate: Mapping[str, Any]) -> tuple[str, int | None, str]:
    """The mechanical dedup identity: file + line + normalized claim.

    The claim is the finding text with runs of whitespace collapsed and
    case-folded, so trivially-reworded-but-identical restatements of the same
    claim at the same location still merge; anything more (semantic overlap of
    differently-worded findings) is the LLM judge's job, deliberately NOT
    attempted here — a mechanical merge stays conservative so it never fuses two
    genuinely distinct findings.
    """
    line = candidate.get("line")
    claim = " ".join(str(candidate.get("text") or "").split()).casefold()
    return (
        str(candidate.get("file") or ""),
        line if isinstance(line, int) and not isinstance(line, bool) else None,
        claim,
    )


def _candidate_id(candidate: Mapping[str, Any]) -> int:
    """A union candidate's stable id (its index in the union — the join key
    :func:`_build_union` stamps)."""
    raw = candidate.get("id")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def _finding_from_candidate(
    candidate: Mapping[str, Any], *, severity: Severity | None = None
) -> Finding:
    """Coerce one union candidate dict back into a domain :class:`Finding`.

    ``severity`` overrides the candidate's own (a merged-away duplicate carries
    its canonical twin's severity); ``None`` keeps the candidate's pass-assigned
    severity through the domain fail-safe (:func:`~shipit.finding.parse_severity`
    else ``major``). The candidate already passed the ONE trust boundary in
    :func:`_build_union`, so the fields are just re-typed here.
    """
    resolved = (
        severity
        if severity is not None
        else (parse_severity(candidate.get("severity")) or DEFAULT_SEVERITY)
    )
    line = candidate.get("line")
    confidence = candidate.get("confidence")
    return Finding(
        severity=resolved,
        text=str(candidate.get("text") or ""),
        file=str(candidate.get("file") or ""),
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        category=str(candidate.get("category") or ""),
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else None
        ),
        evidence=str(candidate.get("evidence") or ""),
        fix=str(candidate.get("fix") or ""),
    )


def _build_union(succeeded: Sequence[_PassResult]) -> list[dict[str, Any]]:
    """The calibrator's candidate list: every successful pass's comments,
    coerced through the ONE trust boundary (:func:`finding_from_dict` — the
    same coercion the posting path applies) and tagged with the dimension that
    found them. Candidate ``id`` == list index (the contract's join key)."""
    union: list[dict[str, Any]] = []
    for result in succeeded:
        for raw in result.review.get("comments") or []:
            if not isinstance(raw, Mapping):
                continue
            finding = finding_from_dict(raw)
            union.append(
                {
                    "id": len(union),
                    "dimension": result.dimension.name,
                    "file": finding.file,
                    "line": finding.line,
                    "severity": finding.severity.value,
                    "category": finding.category or result.dimension.name,
                    "confidence": finding.confidence,
                    "text": finding.text,
                    "evidence": finding.evidence,
                    "fix": finding.fix,
                }
            )
    return union


def _merge_coverage(succeeded: Sequence[_PassResult]) -> dict[str, list]:
    """Union the passes' coverage attestations into ONE summary attestation.

    ``reviewed`` entries dedupe preserving first-seen order; ``skipped``
    entries dedupe by ``(file, reason)``. Malformed pass coverage (the
    schema-unenforced agy path) is skipped defensively, exactly like the
    posting path's coverage renderer.
    """
    reviewed: list[str] = []
    skipped: list[dict[str, str]] = []
    seen_reviewed: set[str] = set()
    seen_skipped: set[tuple[str, str]] = set()
    for result in succeeded:
        summary = result.review.get("summary")
        coverage = summary.get("coverage") if isinstance(summary, Mapping) else None
        if not isinstance(coverage, Mapping):
            continue
        raw_reviewed = coverage.get("reviewed")
        for entry in raw_reviewed if isinstance(raw_reviewed, list) else []:
            text = str(entry)
            if text not in seen_reviewed:
                seen_reviewed.add(text)
                reviewed.append(text)
        raw_skipped = coverage.get("skipped")
        for entry in raw_skipped if isinstance(raw_skipped, list) else []:
            if not isinstance(entry, Mapping):
                continue
            file = str(entry.get("file", "?"))
            reason = str(entry.get("reason", ""))
            if (file, reason) not in seen_skipped:
                seen_skipped.add((file, reason))
                skipped.append({"file": file, "reason": reason})
    return {"reviewed": reviewed, "skipped": skipped}


def _attestation(
    dims: Sequence[Dimension],
    failed: Sequence[_PassResult],
    *,
    union_size: int,
    entries: Sequence[JudgedFinding],
    posted: int,
    calibrated: bool,
) -> str:
    """The fan-out attestation paragraph for the posted summary: what ran, what
    it found, and how the union routed to the posted set — so a human reading
    the PR sees the coverage claim (and any degradation) without opening the
    record.

    ``calibrated`` selects the routing phrasing: the DEFAULT off path posts the
    mechanically-deduped union (only nit-suppressed and duplicate route out — no
    drop/out-of-scope, which only the LLM judge produces); the on path posts
    "after calibration" with the full routed-out breakdown. Either way the
    routed-out counts plus ``posted`` plus the merged-away ``duplicate`` count
    sum to ``union_size``: every candidate is accounted for, so the arithmetic a
    human checks always balances. An EMPTY union had nothing to route (both the
    dedup and the dormant calibrator were skipped), so its line never claims a
    routing that never ran.
    """
    names = ", ".join(d.name for d in dims)
    prelude = f"Review fan-out: {len(dims)} dimension pass(es) ({names}) -> "
    duplicates = sum(1 for judged in entries if judged.duplicate_of is not None)
    nit_suppressed = sum(
        1
        for judged in entries
        if judged.disposition is Disposition.NIT_SUPPRESSED
        and judged.duplicate_of is None
    )
    if union_size == 0:
        # Nothing was routed — a dedup/judge over nothing does nothing — so
        # attest the clean pass without a misleading routing that never ran.
        lines = [f"{prelude}no candidate findings."]
    elif not calibrated:
        lines = [
            f"{prelude}{union_size} candidate finding(s) -> {posted} posted as the "
            f"deduped union ({nit_suppressed} nit-suppressed, {duplicates} "
            f"duplicate); calibrator off."
        ]
    else:
        dropped = sum(
            1
            for judged in entries
            if judged.disposition is Disposition.DROP_UNVERIFIED
            and judged.duplicate_of is None
        )
        out_of_scope = sum(
            1
            for judged in entries
            if judged.disposition is Disposition.OUT_OF_SCOPE
            and judged.duplicate_of is None
        )
        lines = [
            f"{prelude}{union_size} candidate finding(s) -> {posted} posted after "
            f"calibration ({dropped} dropped-unverified, {out_of_scope} "
            f"out-of-scope, {nit_suppressed} nit-suppressed, {duplicates} duplicate)."
        ]
    if failed:
        failures = ", ".join(
            f"{r.dimension.name} ({r.run.get('outcome', 'failed')})" for r in failed
        )
        lines.append(
            f"DEGRADED COVERAGE: pass(es) failed and did not contribute: {failures}."
        )
    return "\n".join(lines)


def _comment_dict(finding: Finding) -> dict[str, Any]:
    """One posted finding back in REVIEW_SCHEMA comment shape — what the
    posting path and the round record both re-coerce through
    :func:`finding_from_dict`, so the routed result rides the EXISTING
    pipeline unchanged (the invisibility constraint)."""
    return {
        "file": finding.file,
        "line": finding.line,
        "text": finding.text,
        "severity": finding.severity.value,
        "category": finding.category,
        "confidence": finding.confidence,
        "evidence": finding.evidence,
        "fix": finding.fix,
    }


def _derive_status(posted: object, *, degraded: bool) -> str:
    """The posted review's status, derived from what posts (severity is the
    routing key, ADR-0044): any major-or-worse → ``REQUEST_CHANGES``; anything
    posted (or degraded coverage) → ``COMMENT``; a clean, fully-covered round →
    ``APPROVED``."""
    findings = list(posted)
    if any(f.severity.blocks_merge for f in findings):
        return "REQUEST_CHANGES"
    if findings or degraded:
        return "COMMENT"
    return "APPROVED"
