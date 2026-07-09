"""roundrecord — the **Review-round record**: pure build + the generate-time write.

The persisted product of one reviewer's review (RVW02-WS03; CONTEXT.md
"Review-round record"): the judged **Findings** with their severities and
**dispositions**, the coverage attestation, and the range reviewed — written
verb-witnessed at GENERATE time (a tee off the review path, never a pipeline
change: the posting path is unchanged and a no-post replay writes the same
record) to the same harness-owned, repo-keyed, append-only, never-committed
JSONL store family as the eval record
(:mod:`shipit.harness.eval.store`, :data:`~shipit.harness.eval.store.REVIEW_ROUNDS_KIND`).

The boundary, stated once: an **eval record** says how a run *behaved*; a
review-round record says what the review *concluded*. They meet in
``shipit eval report``, which joins round records to eval records by run id —
each record carries the run ids + **Variant** hashes of its contributing runs
(``round.runs``: the WS04 dimension fan-out fills it with one entry per
**Dimension pass** plus the **Calibrator** run; the single-pass offline replay
contributes none) and its own review-instructions **Variant**
(``round.variant``), the experiment-arm handle a review-prompt A/B groups by.

Dispositions are the Opportunity-harvest seam: the record ALWAYS carries every
judged finding WITH its disposition — routed-out (dropped) findings included,
never just the posted subset. The PR path passes the Calibrator's real routing
in (``record_round(findings=…)``, RVW02-WS04); a caller with no calibrator
(the single-pass offline replay) falls back to :func:`dispositioned`, which
maps every finding to ``post`` — the honest default for a pipeline where the
whole output reaches the record's ``review``.

Pure core / thin boundary: :func:`build` (and :func:`dispositioned`) are pure —
a record is a function of its arguments, unit-testable from fixtures;
:func:`record_round` is the I/O boundary that stamps the timestamp, hashes the
instructions variant, and appends to the store. It RAISES on failure — the
review-path tee (:func:`shipit.review.service.generate_review`) wraps it
fail-open (a record miss must never degrade a review), while the offline replay
(:mod:`shipit.review.replay`) lets it propagate (the record IS replay's product).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..finding import Disposition, Finding
from ..harness.eval.store import REVIEW_ROUNDS_KIND, append_record
from ..harness.eval.variant import label_from_env, variant_of
from ..identity import repo_from_slug
from .instructions import load_instructions
from .schema import finding_from_dict

#: Bump when the record's field set changes, so an aggregator can read mixed stores
#: (the same convention as :data:`shipit.harness.eval.record.SCHEMA_VERSION`).
SCHEMA_VERSION = 1


def dispositioned(review: Mapping[str, Any]) -> list[tuple[Finding, Disposition]]:
    """Every finding of a review dict, paired with its disposition. PURE.

    Maps each ``comments[]`` entry through the ONE trust boundary
    (:func:`shipit.review.schema.finding_from_dict`) — the SAME coercion the
    posting path applies, so the record can never disagree with what was posted.
    This is the SINGLE-PASS default (the offline replay): with no calibrator
    routing anything out, the whole output reaches the PR/record, so every
    finding is ``post``. The PR path's fan-out (RVW02-WS04) supplies the
    Calibrator's real routing (``drop-unverified`` / ``nit-suppressed`` /
    ``out-of-scope``) through the same ``(Finding, Disposition)`` shape via
    ``record_round(findings=…)`` instead.
    """
    comments = review.get("comments") or []
    return [
        (finding_from_dict(raw), Disposition.POST)
        for raw in comments
        if isinstance(raw, Mapping)
    ]


def build(
    *,
    review: Mapping[str, Any],
    findings: Sequence[tuple[Finding, Disposition]],
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
    timestamp: str,
) -> dict[str, Any]:
    """Assemble the review-round record — one JSONL line per review round. PURE.

    ``review`` supplies the review's own summary layer (status + the coverage
    attestation, read defensively — the agy path is schema-unenforced);
    ``findings`` is the FULL judged set with dispositions (:func:`dispositioned`
    or, post-WS04, the calibrator's routing) — dropped findings ride along with
    their disposition, never erased (the Opportunity-harvest seam). ``pr`` is
    ``None`` for an offline range replay (no PR was touched); ``base_sha`` /
    ``head_sha`` are the range reviewed. ``variant`` is the review-instructions
    content-hash (+ optional A/B label) — the experiment-arm handle; ``runs``
    carries the run ids + variant hashes of every contributing run (empty for
    today's single-pass producer; WS04's dimension passes + Calibrator fill it).
    ``duration_ms`` / ``total_tokens`` are the round's cost (``None`` when the
    backend reports none — the CLI backends report no token totals).
    """
    summary = review.get("summary") or {}
    if not isinstance(summary, Mapping):
        summary = {}
    coverage = summary.get("coverage")
    return {
        "round.schema_version": SCHEMA_VERSION,
        "round.timestamp": timestamp,
        "round.repo": repo,
        "round.pr": pr,
        "round.range": {"base": base_sha, "head": head_sha},
        "round.reviewer": reviewer,
        "round.status": summary.get("status"),
        "round.coverage": coverage if isinstance(coverage, Mapping) else None,
        "round.findings": [
            _finding_record(finding, disposition) for finding, disposition in findings
        ],
        "round.invocation": {
            "model": model,
            "timeout": timeout,
            "instructions_path": instructions_path,
        },
        "round.variant": dict(variant) if variant is not None else None,
        "round.runs": [dict(run) for run in runs],
        "round.usage": {"duration_ms": duration_ms, "total_tokens": total_tokens},
    }


def _finding_record(finding: Finding, disposition: Disposition) -> dict[str, Any]:
    """One judged finding as record data: the domain fields + its disposition.

    The severity/disposition enums serialize as their wire values (the SAME
    tokens the machine marker and the domain vocabulary use), so the store is
    greppable and the report can filter dispositions without an enum table.
    """
    return {
        "file": finding.file,
        "line": finding.line,
        "severity": finding.severity.value,
        "category": finding.category,
        "confidence": finding.confidence,
        "text": finding.text,
        "evidence": finding.evidence,
        "fix": finding.fix,
        "disposition": disposition.value,
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
    findings: Sequence[tuple[Finding, Disposition]] | None = None,
    runs: Sequence[Mapping[str, Any]] = (),
    duration_ms: int | None = None,
    base_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Build one round record for ``review`` and append it to the repo's store.

    The I/O boundary around :func:`build`: stamps the UTC timestamp, resolves
    the store key from ``repo_slug`` (the canonical ``owner/name`` — the same
    :class:`~shipit.identity.Repo` identity the eval store keys on, ADR-0024),
    and content-hashes the review INSTRUCTIONS as the round's **Variant**
    (:func:`~shipit.harness.eval.variant.variant_of` — the same ``sha256:``
    scheme as the role-prompt variant; the instructions are the prompt a
    review A/B edits, so identical instructions pool across PRs and an edited
    prompt separates arms) with any :data:`~shipit.harness.eval.variant.VARIANT_LABEL_ENV`
    label. Returns the store path the record landed in.

    ``findings`` is the FULL judged set with the Calibrator's real dispositions
    (the RVW02-WS04 fan-out passes it; routed-out findings included, never
    erased); ``None`` — the single-pass replay — falls back to
    :func:`dispositioned` (everything ``post``). ``runs`` carries the
    contributing runs' entries (run ids + per-run variant hashes: every
    dimension pass + the calibrator) onto ``round.runs``.

    RAISES on failure (a malformed slug, an unreadable instructions file, an
    unwritable store): the caller owns the failure posture — the review-path tee
    wraps this fail-open, the offline replay propagates (the record is its
    product). ``base_dir`` overrides the store family root (tests); ``env``
    injects the label read.
    """
    repo = repo_from_slug(repo_slug)
    variant = variant_of(
        load_instructions(instructions_path), label=label_from_env(env)
    )
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
        timestamp=_now_iso(),
    )
    return append_record(record, repo, base_dir, kind=REVIEW_ROUNDS_KIND)


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string (the record's ``round.timestamp``)."""
    return _dt.datetime.now(_dt.UTC).isoformat()
