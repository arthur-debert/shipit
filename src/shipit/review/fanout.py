"""Review round orchestration: run the passes, union their findings, route the post.

See docs/adr/0052-round1-default-single-pass.md.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import events
from ..agent import backend as agent_backend
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
from . import artifacts as artifacts_mod
from . import producer
from .calibrator import (
    CalibratedFinding,
    CalibratorConfig,
    run_calibrator,
)
from .diff import RangeView
from .dimensions import Dimension, known_dimension_names, resolve_dimensions
from .match import Claim, same_claim
from .schema import finding_from_dict
from .usage import UNREPORTED

logger = logging.getLogger("shipit.review")


@dataclass(frozen=True)
class FanoutOutcome:
    review: dict
    findings: tuple[JudgedFinding, ...]
    runs: tuple[dict[str, Any], ...]
    total_tokens: int | None = None
    round_id: str = ""
    artifacts_dir: str | None = None


@dataclass(frozen=True)
class _PassResult:
    dimension: Dimension
    run: dict[str, Any]
    review: dict | None


DEFAULT_INCREMENTAL_REASONING = "low"

#: Not a registry dimension: it only labels an incremental round's pass.
_INCREMENTAL_DIMENSION = Dimension(
    name="incremental",
    title="Incremental fix-range",
    focus="the fix range only, with mandatory dependency-neighborhood context",
)

#: Not a registry dimension either, so a ``dimensions`` list cannot name it.
_SINGLE_PASS_DIMENSION = Dimension(
    name="single",
    title="Single full-scope pass",
    focus="the full diff, unscoped — one monolithic full-scope pass",
)


def run_fanout_review(
    backend: Backend,
    target,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    semantic_dedup: bool = False,
    nit_cap: int | None = None,
    invocation_overrides: Mapping[str, Mapping[str, str]] | None = None,
    incremental: bool = False,
    incremental_reasoning: str = DEFAULT_INCREMENTAL_REASONING,
    dry_run: bool = False,
    launcher: launch.Runner | None = None,
    artifacts_base_dir: Path | None = None,
    review_tree_naming: Mapping[str, str] | None = None,
) -> FanoutOutcome:
    """Review ``target``, a PR review view or a ``RangeView`` for offline
    replay: empty ``dimensions`` runs one unscoped pass, a non-empty list the
    named fan-out, ``incremental`` one fix-range pass with ``nit_cap`` 0."""
    range_view = target if isinstance(target, RangeView) else None
    if range_view is not None and incremental:
        raise ValueError(
            "run_fanout_review: incremental and a RangeView target are mutually "
            "exclusive — rounds are keyed to a live PR head, and multi-round "
            "fix-range replay is out of the Review Lab's scope"
        )
    if range_view is not None and dry_run:
        raise ValueError(
            "run_fanout_review: dry_run is not supported for a RangeView target "
            "— the dry-run contract prints a would-run Tree launch, and an "
            "offline replay has no Tree"
        )
    if semantic_dedup and calibrator is not None:
        raise ValueError(
            "run_fanout_review: semantic_dedup and a calibrator are mutually "
            "exclusive — semantic_dedup is the mechanical (judge-off) path's "
            "near-duplicate collapse (#750), and the calibrator does its own "
            "dedup; both at once would post an arm no config declares"
        )
    if invocation_overrides and incremental:
        raise ValueError(
            "run_fanout_review: invocation_overrides and incremental are "
            "mutually exclusive — an incremental round runs one fix-range "
            "pass, not dimension passes"
        )
    if invocation_overrides and not dimensions:
        raise ValueError(
            "run_fanout_review: invocation_overrides require an explicit "
            "`dimensions` fan-out — the round-1 default is one monolithic "
            "pass (ADR-0052), which has no dimension passes to override"
        )
    incremental_range: tuple[str, str] | None = None
    single = False
    if incremental:
        incremental_range = (str(target.base_sha), str(target.head_sha))
        dims = (_INCREMENTAL_DIMENSION,)
        effective_nit_cap = 0
    elif not dimensions:
        single = True
        dims = (_SINGLE_PASS_DIMENSION,)
        effective_nit_cap = nit_cap
    else:
        try:
            dims = resolve_dimensions(dimensions)
        except KeyError as exc:
            raise ValueError(
                f"unknown review dimension {exc.args[0]!r} — known dimensions: "
                f"{', '.join(known_dimension_names())}"
            ) from None
        effective_nit_cap = nit_cap
    if invocation_overrides:
        unknown = sorted(set(invocation_overrides) - {d.name for d in dims})
        if unknown:
            raise ValueError(
                f"invocation_overrides name dimension(s) outside this round's "
                f"pass set: {', '.join(unknown)} (passes: "
                f"{', '.join(d.name for d in dims)})"
            )
    agent = backend.funnel_agent or backend.name
    scoped = not incremental and not single
    pass_word = "incremental" if incremental else ("single" if single else "dimension")
    pr_number = None if range_view is not None else target.number
    pr_extra = {"pr": pr_number} if pr_number is not None else {}
    where = (
        f"range {range_view.base_sha}..{range_view.head_sha}"
        if range_view is not None
        else f"pr#{target.number}"
    )

    if dry_run:
        for dim in dims:
            producer.run_tree_review(
                backend,
                target,
                model=model,
                timeout=timeout,
                instructions_path=instructions_path,
                dry_run=True,
                dimension=dim if scoped else None,
                incremental_range=incremental_range,
            )
        if calibrator is None:
            collapse = (
                " with the semantic near-duplicate collapse (#750)"
                if semantic_dedup
                else ""
            )
            print(
                "(dry-run: calibrator OFF — would post the mechanically-deduped "
                f"union{collapse} using each pass's own severity)"
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

    round_backends = [backend]
    if calibrator is not None:
        round_backends.append(agent_backend.by_name(calibrator.backend))
    producer.preflight_round(round_backends)

    workdir = (
        range_view.workdir
        if range_view is not None
        else producer.provision_review_tree(target, backend, naming=review_tree_naming)
    )
    label = label_from_env()
    round_id = uuid.uuid4().hex
    repo_slug = (
        range_view.repo.slug
        if range_view is not None
        else getattr(target, "repo", None)
    )
    round_dir = artifacts_mod.round_root(
        repo_slug, round_id, base_dir=artifacts_base_dir
    )

    def _one_pass(dim: Dimension) -> _PassResult:
        override = (invocation_overrides or {}).get(dim.name, {})
        pass_model = override.get("model", model)
        pass_timeout = override.get("timeout", timeout)
        if range_view is not None:
            task = producer.range_pass_task_text(
                backend,
                range_view,
                instructions_path=instructions_path,
                dimension=dim if scoped else None,
            )
        else:
            task_kwargs = {
                "instructions_path": instructions_path,
                "dimension": dim if scoped else None,
                "incremental_range": incremental_range,
                "diff": target.diff,
            }
            task = producer.pass_task_text(backend, target.number, **task_kwargs)
        run_id = uuid.uuid4().hex
        kind = f"{pass_word}-pass"
        bundle = artifacts_mod.RunArtifacts.under(round_dir, run_id)
        run: dict[str, Any] = {
            "run_id": run_id,
            "kind": kind,
            "dimension": dim.name,
            "backend": agent,
            "model": pass_model,
            "variant": variant_of(task, label=label).as_record(),
            "artifacts": str(bundle.dir) if bundle.dir is not None else None,
            "usage": UNREPORTED.as_record(),
        }
        if incremental:
            run["range"] = {"base": incremental_range[0], "head": incremental_range[1]}
        bundle.record(
            run_id=run_id,
            round_id=round_id,
            kind=kind,
            dimension=dim.name,
            backend=agent,
            model=pass_model,
            variant=run["variant"],
            pr=pr_number,
        )
        correlation = {
            **pr_extra,
            "reviewer": agent,
            "run_id": run_id,
            "round_id": round_id,
            "dimension": dim.name,
        }
        events.emit(
            logger,
            "review.pass.launched",
            "%s pass %s launched for %s (agent=%s)",
            pass_word,
            dim.name,
            where,
            agent,
            extra=correlation,
        )
        start = time.monotonic()
        try:
            if range_view is not None:
                captured = producer.run_range_review(
                    backend,
                    range_view,
                    model=pass_model,
                    timeout=pass_timeout,
                    instructions_path=instructions_path,
                    launcher=launcher,
                    dimension=dim if scoped else None,
                    run_id=run_id,
                    artifacts=bundle,
                )
            else:
                captured = producer.run_tree_review(
                    backend,
                    target,
                    model=pass_model,
                    timeout=pass_timeout,
                    instructions_path=instructions_path,
                    launcher=launcher,
                    dimension=dim if scoped else None,
                    tree_path=workdir,
                    incremental_range=incremental_range,
                    reasoning=incremental_reasoning if incremental else None,
                    run_id=run_id,
                    artifacts=bundle,
                )
        except Exception as exc:  # noqa: BLE001 - a pass failure degrades, never kills
            run["duration_ms"] = int((time.monotonic() - start) * 1000)
            run["outcome"] = (
                "timed_out" if getattr(exc, "timed_out", False) else "failed"
            )
            run["detail"] = str(exc)[:500]
            bundle.record(
                outcome=run["outcome"],
                duration_ms=run["duration_ms"],
                error=str(exc),
            )
            logger.warning(
                "%s pass %s failed for %s (agent=%s) — coverage degrades, "
                "the round continues",
                pass_word,
                dim.name,
                where,
                agent,
                exc_info=True,
                extra=correlation,
            )
            events.emit(
                logger,
                "review.pass.settled",
                "%s pass %s settled %s for %s in %dms",
                pass_word,
                dim.name,
                run["outcome"],
                where,
                run["duration_ms"],
                extra={
                    **correlation,
                    "outcome": run["outcome"],
                    "duration_ms": run["duration_ms"],
                },
            )
            return _PassResult(dimension=dim, run=run, review=None)
        review = captured.review
        run["duration_ms"] = int((time.monotonic() - start) * 1000)
        run["outcome"] = "success"
        run["findings"] = len(review.get("comments") or [])
        run["usage"] = captured.usage.as_record()
        if captured.reasoning is not None:
            run["reasoning"] = captured.reasoning
        bundle.record(
            outcome="success",
            duration_ms=run["duration_ms"],
            findings=run["findings"],
        )
        events.emit(
            logger,
            "review.pass.settled",
            "%s pass %s settled success for %s in %dms (%d finding(s))",
            pass_word,
            dim.name,
            where,
            run["duration_ms"],
            run["findings"],
            extra={
                **correlation,
                "outcome": "success",
                "duration_ms": run["duration_ms"],
                "findings": run["findings"],
            },
        )
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
        if incremental:
            kind = "the incremental pass failed"
        elif single:
            kind = "the single review pass failed"
        else:
            kind = f"all {len(dims)} dimension passes failed"
        raise RuntimeError(f"{kind} for {where} (agent={agent}) — {details}")

    union = _build_union(succeeded)
    coverage = _merge_coverage(succeeded)
    artifacts_dir = str(round_dir) if round_dir is not None else None

    calibrated = calibrator is not None
    if not union:
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
        return FanoutOutcome(
            review=review,
            findings=(),
            runs=tuple(runs),
            total_tokens=_round_total(runs),
            round_id=round_id,
            artifacts_dir=artifacts_dir,
        )

    if calibrator is None:
        entries = dedup_union(union, semantic=semantic_dedup)
        feedback = ""
    else:
        calibrator_bundle = artifacts_mod.RunArtifacts.under(round_dir, "calibrator")
        calibrator_bundle.record(
            round_id=round_id,
            kind="calibrator",
            backend=calibrator.backend,
            model=calibrator.model,
            reasoning=calibrator.reasoning,
            pr=pr_number,
        )
        calibrator_run: dict[str, Any] = {
            "kind": "calibrator",
            "backend": calibrator.backend,
            "model": calibrator.model,
            "artifacts": (
                str(calibrator_bundle.dir)
                if calibrator_bundle.dir is not None
                else None
            ),
        }
        calibrator_correlation = {
            **pr_extra,
            "reviewer": agent,
            "run_id": "calibrator",
            "round_id": round_id,
            "dimension": "calibrator",
        }
        events.emit(
            logger,
            "review.pass.launched",
            "calibrator (%s) launched over %d candidate(s) for %s",
            calibrator.backend,
            len(union),
            where,
            extra=calibrator_correlation,
        )
        start = time.monotonic()
        try:
            judged_run = run_calibrator(
                calibrator,
                union,
                pr_number=pr_number,
                commit_range=(
                    (str(range_view.base_sha), str(range_view.head_sha))
                    if range_view is not None
                    else None
                ),
                cwd=workdir,
                launcher=launcher,
                artifacts=calibrator_bundle,
                correlation=calibrator_correlation,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            calibrator_bundle.record(
                outcome=("timed_out" if getattr(exc, "timed_out", False) else "failed"),
                duration_ms=duration_ms,
                error=str(exc),
            )
            events.emit(
                logger,
                "review.pass.settled",
                "calibrator (%s) settled failed for %s in %dms",
                calibrator.backend,
                where,
                duration_ms,
                extra={
                    **calibrator_correlation,
                    "outcome": "failed",
                    "duration_ms": duration_ms,
                },
            )
            raise
        result = judged_run.result
        duration_ms = int((time.monotonic() - start) * 1000)
        calibrator_run.update(
            {
                "run_id": judged_run.run_id,
                "duration_ms": duration_ms,
                "outcome": "success",
                "judged": len(result.entries),
                "variant": variant_of(judged_run.task, label=label).as_record(),
                "usage": judged_run.usage.as_record(),
            }
        )
        if judged_run.reasoning is not None:
            calibrator_run["reasoning"] = judged_run.reasoning
        calibrator_bundle.record(
            run_id=judged_run.run_id,
            outcome="success",
            duration_ms=duration_ms,
            judged=len(result.entries),
            variant=calibrator_run["variant"],
        )
        events.emit(
            logger,
            "review.pass.settled",
            "calibrator (%s) settled success for %s in %dms (%d judged)",
            calibrator.backend,
            where,
            duration_ms,
            len(result.entries),
            extra={
                **calibrator_correlation,
                "outcome": "success",
                "duration_ms": duration_ms,
            },
        )
        runs.append(calibrator_run)
        entries = result.entries
        feedback = result.overall_feedback.strip()

    routed = route_calibrated(entries, nit_cap=effective_nit_cap)
    findings = tuple(
        JudgedFinding(
            entry.finding, d, entry.duplicate_of, run_id=_pass_run_id(union, entry.id)
        )
        for entry, d in routed
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
        semantic=semantic_dedup,
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

    operation = (
        "calibration"
        if calibrated
        else "semantic near-duplicate dedup"
        if semantic_dedup
        else "mechanical dedup"
    )
    events.emit(
        logger,
        "review.calibrated" if calibrated else "review.deduped",
        "%s completed for %s (agent=%s): %d candidate(s) -> %d posted",
        operation,
        where,
        agent,
        len(union),
        posted,
        extra={
            **pr_extra,
            "reviewer": agent,
            "round_id": round_id,
            "candidates": len(union),
            "posted": posted,
        },
    )
    for judged in findings:
        if judged.posted:
            continue
        finding = judged.finding
        disposition_extra = {
            **pr_extra,
            "reviewer": agent,
            "round_id": round_id,
            "severity": finding.severity.value,
            "disposition": judged.disposition.value,
        }
        if judged.run_id is not None:
            disposition_extra["run_id"] = judged.run_id
        events.emit(
            logger,
            "finding.dispositioned",
            "finding routed out on %s: %s (%s) -> %s",
            where,
            finding.file or "(no file)",
            finding.severity.value,
            judged.disposition.value,
            extra=disposition_extra,
        )

    return FanoutOutcome(
        review=review,
        findings=findings,
        runs=tuple(runs),
        total_tokens=_round_total(runs),
        round_id=round_id,
        artifacts_dir=artifacts_dir,
    )


def _round_total(runs: Sequence[Mapping[str, Any]]) -> int | None:
    totals = []
    for run in runs:
        usage = run.get("usage")
        if not isinstance(usage, Mapping):
            continue
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool):
            totals.append(total)
    return sum(totals) if totals else None


def _pass_run_id(union: Sequence[Mapping[str, Any]], entry_id: int) -> str | None:
    if 0 <= entry_id < len(union):
        raw = union[entry_id].get("run_id")
        return str(raw) if raw else None
    return None


def route_calibrated(
    entries: Sequence[CalibratedFinding], *, nit_cap: int | None
) -> tuple[tuple[CalibratedFinding, Disposition], ...]:
    """Every judged finding with its final disposition, severity-first; one posts
    iff that is ``post`` AND it is canonical. Nits over ``nit_cap`` (``None``
    uncapped) flip to ``nit-suppressed``; a duplicate inherits its twin's."""
    ordered = sorted(entries, key=lambda e: e.finding.severity.rank)
    nits_posted = 0
    routed: list[tuple[CalibratedFinding, Disposition]] = []
    final_disposition_for: dict[int, Disposition] = {}
    for entry in ordered:
        disposition = entry.disposition
        if entry.duplicate_of is not None:
            # Both producers append duplicates after their canonical, and the
            # severity sort is stable, so the twin is always seen first.
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
    *,
    semantic: bool = False,
) -> tuple[CalibratedFinding, ...]:
    """Merge candidates by ``(file, line, claim)``, most-severe canonical and the
    rest duplicates carrying its severity. Nothing is dropped, canonicals first."""
    grouped: list[list[Mapping[str, Any]]] = []
    group_by_key: dict[tuple[str, int | None, str], list[Mapping[str, Any]]] = {}
    for candidate in union:
        key = _dedup_key(candidate)
        group = group_by_key.get(key)
        if group is None and semantic:
            # Every member must match: no chaining, so a bridging finding
            # cannot fuse two distinct defects at one line.
            group = next(
                (
                    members
                    for members in grouped
                    if all(_near_duplicate(candidate, member) for member in members)
                ),
                None,
            )
        if group is None:
            group = []
            grouped.append(group)
        group.append(candidate)
        group_by_key.setdefault(key, group)

    entries: list[CalibratedFinding] = []
    for members in grouped:
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


#: Zero: every pass reviewed the same head, so there is no drift to absorb.
SEMANTIC_DEDUP_LINE_SLACK = 0


def _near_duplicate(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    file = str(a.get("file") or "")
    if not file:
        return False
    a_line = _candidate_line(a)
    b_line = _candidate_line(b)
    if a_line != b_line:
        return False
    return same_claim(
        Claim(file=file, line=a_line, text=str(a.get("text") or "")),
        Claim(
            file=str(b.get("file") or ""),
            line=b_line,
            text=str(b.get("text") or ""),
        ),
        line_slack=SEMANTIC_DEDUP_LINE_SLACK,
    )


def _candidate_line(candidate: Mapping[str, Any]) -> int | None:
    line = candidate.get("line")
    return line if isinstance(line, int) and not isinstance(line, bool) else None


def _dedup_key(candidate: Mapping[str, Any]) -> tuple[str, int | None, str]:
    claim = " ".join(str(candidate.get("text") or "").split()).casefold()
    return (str(candidate.get("file") or ""), _candidate_line(candidate), claim)


def _candidate_id(candidate: Mapping[str, Any]) -> int:
    raw = candidate.get("id")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def _finding_from_candidate(
    candidate: Mapping[str, Any], *, severity: Severity | None = None
) -> Finding:
    resolved = (
        severity
        if severity is not None
        else (parse_severity(candidate.get("severity")) or DEFAULT_SEVERITY)
    )
    confidence = candidate.get("confidence")
    return Finding(
        severity=resolved,
        text=str(candidate.get("text") or ""),
        file=str(candidate.get("file") or ""),
        line=_candidate_line(candidate),
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
                    "run_id": result.run.get("run_id"),
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
    semantic: bool = False,
) -> str:
    if len(dims) == 1 and dims[0] is _SINGLE_PASS_DIMENSION:
        # Keyed off object identity, so a registry dimension named "single"
        # cannot trigger this wording.
        prelude = "Review: one full-scope pass -> "
    else:
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
        lines = [f"{prelude}no candidate findings."]
    elif not calibrated:
        union_word = "semantically-deduped union" if semantic else "deduped union"
        lines = [
            f"{prelude}{union_size} candidate finding(s) -> {posted} posted as the "
            f"{union_word} ({nit_suppressed} nit-suppressed, {duplicates} "
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
    findings = list(posted)
    if any(f.severity.blocks_merge for f in findings):
        return "REQUEST_CHANGES"
    if findings or degraded:
        return "COMMENT"
    return "APPROVED"
