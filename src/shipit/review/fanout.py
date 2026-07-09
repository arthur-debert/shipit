"""fanout — round-1 dimension fan-out + calibration (RVW02-WS04, ADR-0045).

The orchestration between the review producer and the posting service: a
local-agent reviewer's detached review run no longer makes one monolithic
"find everything" pass — it fans out into parallel **Dimension passes**
(:mod:`shipit.review.dimensions`) on the reviewer's own backend against ONE
shared read-only Tree, unions the results, and hands the union to the
**Calibrator** (:mod:`shipit.review.calibrator`) — the single fixed table-level
judge that dedups, adversarially verifies, normalizes severity, and assigns
every judged finding a **Disposition**.

What this module owns:

  * the pass fan-out (provision the Tree once, launch the configured
    dimension set in parallel through :func:`shipit.review.producer.run_tree_review`,
    tolerate per-pass failures — a pass failure degrades coverage, it never
    kills the round unless EVERY pass failed);
  * the union (each successful pass's comments, coerced through the ONE trust
    boundary :func:`shipit.review.schema.finding_from_dict`, tagged with the
    dimension that found them) and the merged coverage attestation;
  * the deterministic post-calibration routing (:func:`route_calibrated`):
    duplicates never post, round-1 nits post under the TABLE-LEVEL nit cap
    (over-cap nits flip to ``nit-suppressed``, recorded; ``0`` floors the
    posted review at minor), the posted status derives from what posts
    (major-or-worse → ``REQUEST_CHANGES``); and
  * the round's contributing-run trail: one entry per pass + one for the
    calibrator, each with a run id and the **Variant** hash of the exact
    prompt that ran — what the review-round record's ``round.runs`` carries
    and ``shipit eval report`` joins on (WS03).

The fan-out is INVISIBLE below the reviewer boundary (ADR-0045): the service
posts ONE review through the reviewer's own bot exactly as before; the funnel,
reconcile, and prstate machinery are untouched. An EMPTY union skips the
calibrator entirely (a judge that never originates has nothing to do with
nothing) and posts the attested clean review.
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
from ..finding import Disposition, Finding, Severity
from ..harness.eval.variant import label_from_env, variant_of
from ..spawn import launch
from . import producer
from .calibrator import (
    DEFAULT_CALIBRATOR,
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

    ``review`` is the calibrated REVIEW_SCHEMA-shaped dict the posting path
    consumes unchanged (the fan-out's invisibility below the reviewer
    boundary); ``findings`` is the FULL judged set with dispositions —
    routed-out findings included, never erased (the round record's
    Opportunity-harvest seam); ``runs`` the contributing-run entries (every
    pass + the calibrator, run ids + variant hashes) for ``round.runs``.
    """

    review: dict
    findings: tuple[tuple[Finding, Disposition], ...]
    runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _PassResult:
    """One dimension pass's outcome: its run entry + the review it captured
    (``None`` when the pass failed — the run entry carries the why)."""

    dimension: Dimension
    run: dict[str, Any]
    review: dict | None


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
    dry_run: bool = False,
    launcher: launch.Runner | None = None,
) -> FanoutOutcome:
    """Fan ``backend``'s review of ``ctx`` out into dimension passes, calibrate
    the union, and return the calibrated :class:`FanoutOutcome`.

    ``dimensions`` names the reviewer's configured pass set (the per-reviewer
    Roster option; ``None``/empty → the shipped default set); ``calibrator``
    the table-level judge config (``None`` → :data:`~shipit.review.calibrator.DEFAULT_CALIBRATOR`);
    ``nit_cap`` the table-level round-1 nit budget (``None`` → uncapped, ``0``
    → floor at minor). ``model`` / ``timeout`` / ``instructions_path`` are the
    reviewer's own run options and apply to every pass, exactly as they applied
    to the monolithic run.

    Failure posture: a SINGLE pass failure is tolerated — its run entry records
    the outcome, the posted summary attests the degraded coverage; ALL passes
    failing raises ``RuntimeError`` (the service maps it to the ``failed``
    funnel outcome). A calibrator failure (unavailable / timed out /
    unparseable / contract-violating output) PROPAGATES — an uncalibrated
    union is never posted (severities off the common ruler would be posted
    unverified); the round degrades non-blocking exactly like a failed
    monolithic review (ADR-0006).

    With ``dry_run=True``: prints each pass's would-run argv (one per
    dimension, no clone, no model bill) plus a calibrator note, and returns an
    empty outcome — the same honest dry-run contract as the producer's.
    """
    try:
        dims = resolve_dimensions(dimensions)
    except KeyError as exc:
        raise ValueError(
            f"unknown review dimension {exc.args[0]!r} — known dimensions: "
            f"{', '.join(known_dimension_names())}"
        ) from None
    config = calibrator if calibrator is not None else DEFAULT_CALIBRATOR
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
                dimension=dim,
            )
        print(
            f"(dry-run: would calibrate the union with {config.backend} "
            f"[model={config.model or 'default'}, reasoning={config.reasoning}])"
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
            backend, ctx.number, instructions_path=instructions_path, dimension=dim
        )
        run: dict[str, Any] = {
            "run_id": uuid.uuid4().hex,
            "kind": "dimension-pass",
            "dimension": dim.name,
            "backend": agent,
            "model": model,
            "variant": variant_of(task, label=label).as_record(),
        }
        start = time.monotonic()
        try:
            review = producer.run_tree_review(
                backend,
                ctx,
                model=model,
                timeout=timeout,
                instructions_path=instructions_path,
                launcher=launcher,
                dimension=dim,
                tree_path=tree_path,
            )
        except Exception as exc:  # noqa: BLE001 - a pass failure degrades, never kills
            run["duration_ms"] = int((time.monotonic() - start) * 1000)
            run["outcome"] = (
                "timed_out" if getattr(exc, "timed_out", False) else "failed"
            )
            run["detail"] = str(exc)[:500]
            logger.warning(
                "dimension pass %s failed for pr#%s (agent=%s) — coverage degrades, "
                "the round continues",
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
        raise RuntimeError(
            f"all {len(dims)} dimension passes failed for pr#{ctx.number} "
            f"(agent={agent}) — {details}"
        )

    union = _build_union(succeeded)
    coverage = _merge_coverage(succeeded)

    if not union:
        # Nothing to judge: the calibrator NEVER originates, so launching it
        # over an empty union buys nothing. Post the attested clean review.
        review = {
            "summary": {
                "status": "COMMENT" if failed else "APPROVED",
                "overall_feedback": _attestation(
                    dims, failed, union_size=0, entries=(), posted=0
                ),
                "coverage": coverage,
            },
            "comments": [],
        }
        return FanoutOutcome(review=review, findings=(), runs=tuple(runs))

    calibrator_run: dict[str, Any] = {
        "kind": "calibrator",
        "backend": config.backend,
        "model": config.model,
        "reasoning": config.reasoning,
    }
    start = time.monotonic()
    result, run_id, task = run_calibrator(
        config,
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

    routed = route_calibrated(result.entries, nit_cap=nit_cap)
    findings = tuple((entry.finding, d) for entry, d in routed)
    posted_entries = [
        entry
        for entry, d in routed
        if d is Disposition.POST and entry.duplicate_of is None
    ]
    comments = [_comment_dict(entry.finding) for entry in posted_entries]
    posted = len(comments)
    status = _derive_status(
        (entry.finding for entry in posted_entries), degraded=bool(failed)
    )
    feedback = result.overall_feedback.strip()
    attestation = _attestation(
        dims, failed, union_size=len(union), entries=findings, posted=posted
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
        "review.calibrated",
        "calibration completed for pr#%s (agent=%s): %d candidate(s) -> %d posted",
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
    for finding, disposition in findings:
        if disposition is Disposition.POST:
            continue
        events.emit(
            logger,
            "finding.dispositioned",
            "finding routed out on pr#%s: %s (%s) -> %s",
            ctx.number,
            finding.file or "(no file)",
            finding.severity.value,
            disposition.value,
            extra={
                "pr": ctx.number,
                "reviewer": agent,
                "severity": finding.severity.value,
                "disposition": disposition.value,
            },
        )

    return FanoutOutcome(review=review, findings=findings, runs=tuple(runs))


def route_calibrated(
    entries: Sequence[CalibratedFinding], *, nit_cap: int | None
) -> tuple[tuple[CalibratedFinding, Disposition], ...]:
    """The deterministic post-calibration routing. PURE.

    Two policies the CODE enforces rather than the judge (deterministic, so
    they are testable and prompt-drift-proof):

      * DUPLICATES NEVER POST — an entry merged into a canonical twin
        (``duplicate_of`` set) shares the twin's judged disposition (it IS the
        same underlying finding, and its substance reaches the PR through the
        twin) but is never emitted as a second posted comment; and
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
    for entry in ordered:
        disposition = entry.disposition
        if (
            disposition is Disposition.POST
            and entry.duplicate_of is None
            and entry.finding.severity is Severity.NIT
            and nit_cap is not None
        ):
            if nits_posted >= nit_cap:
                disposition = Disposition.NIT_SUPPRESSED
            else:
                nits_posted += 1
        routed.append((entry, disposition))
    return tuple(routed)


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
    entries: Sequence[tuple[Finding, Disposition]],
    posted: int,
) -> str:
    """The fan-out attestation paragraph for the posted summary: what ran, what
    it found, and how calibration routed it — so a human reading the PR sees
    the coverage claim (and any degradation) without opening the record."""
    names = ", ".join(d.name for d in dims)
    routed_out = {
        disposition: sum(1 for _, d in entries if d is disposition)
        for disposition in (
            Disposition.DROP_UNVERIFIED,
            Disposition.NIT_SUPPRESSED,
            Disposition.OUT_OF_SCOPE,
        )
    }
    lines = [
        f"Review fan-out: {len(dims)} dimension pass(es) ({names}) -> "
        f"{union_size} candidate finding(s) -> {posted} posted after "
        f"calibration ({routed_out[Disposition.DROP_UNVERIFIED]} "
        f"dropped-unverified, {routed_out[Disposition.OUT_OF_SCOPE]} "
        f"out-of-scope, {routed_out[Disposition.NIT_SUPPRESSED]} "
        "nit-suppressed)."
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
    :func:`finding_from_dict`, so the calibrated result rides the EXISTING
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
