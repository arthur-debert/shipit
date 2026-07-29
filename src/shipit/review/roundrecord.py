"""The Review-round record: what one review CONCLUDED and what it cost.

:func:`build` is pure; :func:`record_round` is the I/O boundary and raises on
failure, so each caller owns its own failure posture.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..finding import Disposition, JudgedFinding
from ..harness.eval.store import REVIEW_ROUNDS_KIND, append_record, read_records
from ..harness.eval.variant import label_from_env, variant_of
from ..identity import repo_from_slug
from .dimensions import fanout_variant_text
from .instructions import load_instructions
from .schema import finding_from_dict

#: Bump when the field set changes, so an aggregator can read mixed stores.
SCHEMA_VERSION = 4


def dispositioned(
    review: Mapping[str, Any], *, run_id: str | None = None
) -> list[JudgedFinding]:
    """Every finding of a review dict, all ``post`` and canonical — the
    single-pass default, for a caller with no calibrator routing to supply."""
    comments = review.get("comments") or []
    return [
        JudgedFinding(finding_from_dict(raw), Disposition.POST, run_id=run_id)
        for raw in comments
        if isinstance(raw, Mapping)
    ]


def build(
    *,
    review: Mapping[str, Any],
    findings: Sequence[JudgedFinding],
    repo: str,
    pr: int | None,
    base_sha: str,
    head_sha: str,
    reviewer: str,
    model: str,
    timeout: str,
    instructions_path: str | None,
    variant: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]] = (),
    duration_ms: int | None = None,
    total_tokens: int | None = None,
    round_id: str | None = None,
    artifacts_dir: str | None = None,
    cell: Mapping[str, Any] | None = None,
    timestamp: str,
) -> dict[str, Any]:
    """One JSONL line for one review round. ``findings`` is the FULL judged set,
    routed-out findings included; ``pr`` is ``None`` for an offline range replay,
    and ``total_tokens`` ``None`` means no contributing run reported any."""
    summary = review.get("summary") or {}
    if not isinstance(summary, Mapping):
        summary = {}
    coverage = summary.get("coverage")
    return {
        "round.schema_version": SCHEMA_VERSION,
        "round.timestamp": timestamp,
        "round.id": round_id,
        "round.artifacts": artifacts_dir,
        "round.repo": repo,
        "round.pr": pr,
        "round.range": {"base": base_sha, "head": head_sha},
        "round.reviewer": reviewer,
        "round.status": summary.get("status"),
        "round.coverage": coverage if isinstance(coverage, Mapping) else None,
        "round.findings": [_finding_record(judged) for judged in findings],
        "round.invocation": {
            "model": model,
            "timeout": timeout,
            "instructions_path": instructions_path,
        },
        "round.variant": dict(variant) if variant is not None else None,
        "round.cell": dict(cell) if cell is not None else None,
        "round.runs": [dict(run) for run in runs],
        "round.usage": {"duration_ms": duration_ms, "total_tokens": total_tokens},
    }


def _finding_record(judged: JudgedFinding) -> dict[str, Any]:
    """One judged finding as record data. A duplicate carries its twin's ``post``
    disposition but never reached the PR, so "posted" reads as ``disposition ==
    post AND duplicate_of is None``, never the disposition alone."""
    finding = judged.finding
    return {
        "file": finding.file,
        "line": finding.line,
        "severity": finding.severity.value,
        "category": finding.category,
        "confidence": finding.confidence,
        "text": finding.text,
        "evidence": finding.evidence,
        "fix": finding.fix,
        "disposition": judged.disposition.value,
        "duplicate_of": judged.duplicate_of,
        "run_id": judged.run_id,
    }


def record_round(
    review: Mapping[str, Any],
    *,
    repo_slug: str,
    pr: int | None,
    base_sha: str,
    head_sha: str,
    reviewer: str,
    model: str,
    timeout: str,
    instructions_path: str | None,
    findings: Sequence[JudgedFinding] | None = None,
    runs: Sequence[Mapping[str, Any]] = (),
    duration_ms: int | None = None,
    total_tokens: int | None = None,
    round_id: str | None = None,
    artifacts_dir: str | None = None,
    cell: Mapping[str, Any] | None = None,
    dimension_names: Sequence[str] | None = None,
    dimension_overrides: Mapping[str, Mapping[str, str]] | None = None,
    base_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Build one round record and append it to the repo's store, returning the
    store path. ``dimension_names`` must be the RESOLVED pass set — ``None`` means
    "not a fan-out" — because the focus texts live in code, not the instructions
    file, so arms differing only by dimension set would otherwise pool under one
    variant. ``findings`` ``None`` falls back to :func:`dispositioned`. Raises on
    failure; the caller owns the posture."""
    repo = repo_from_slug(repo_slug)
    variant_text = load_instructions(instructions_path)
    if dimension_names is not None:
        variant_text = fanout_variant_text(
            variant_text, dimension_names, dimension_overrides
        )
    variant = variant_of(variant_text, label=label_from_env(env))
    record = build(
        review=review,
        findings=findings if findings is not None else dispositioned(review),
        repo=repo.slug,
        pr=pr,
        base_sha=base_sha,
        head_sha=head_sha,
        reviewer=reviewer,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        variant=variant.as_record(),
        runs=runs,
        duration_ms=duration_ms,
        total_tokens=total_tokens,
        round_id=round_id,
        artifacts_dir=artifacts_dir,
        cell=cell,
        timestamp=_now_iso(),
    )
    return append_record(record, repo, base_dir, kind=REVIEW_ROUNDS_KIND)


def last_reviewed_head(
    *,
    repo_slug: str,
    pr: int,
    reviewer: str,
    new_head: str,
    base_dir: Path | None = None,
) -> str | None:
    """The head ``reviewer`` most recently reviewed on PR ``pr``, ignoring
    ``new_head`` itself — the incremental round's fix-range base. ``None`` for a
    first round or a lost record, which fall through to a full round."""
    repo = repo_from_slug(repo_slug)
    found: str | None = None
    for record in read_records(repo, base_dir, kind=REVIEW_ROUNDS_KIND):
        if record.get("round.pr") != pr:
            continue
        if record.get("round.reviewer") != reviewer:
            continue
        rng = record.get("round.range")
        head = rng.get("head") if isinstance(rng, Mapping) else None
        if not head or head == new_head:
            continue
        found = head
    return found


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()
