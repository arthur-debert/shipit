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
review-round record says what the review *concluded* — and, since RVW03-WS04,
what it COST. Each contributing run's entry (``round.runs``: the dimension
fan-out fills it with one entry per **Dimension pass** plus the **Calibrator**
run — offline exactly as live, RVW03-WS01; the single-pass offline replay
contributes its one range pass, RVW03-WS02) carries its run id, **Variant**
hash, per-run token ``usage`` measured from the CLI's own output at launch-result
level (explicitly-unknown for a CLI that reports none — NEVER via the broken
transcript/run_id join), and the ReasoningLevel actually applied to argv.
``round.usage.total_tokens`` sums the reported per-run usage; ``shipit eval
report``'s review axis reads that token cost straight from the record. The
record also carries its own review-instructions **Variant**
(``round.variant``), the experiment-arm handle a review-prompt A/B groups by —
for a dimension fan-out round the hash folds the resolved dimension set's
prompt material (names, titles, focus texts, per-dimension overrides) in with
the instructions (#713), so arms differing only by dimension set stamp
different variants.
Since RVW03-WS02 the record also carries ``round.id`` / ``round.artifacts``
(the round's per-run artifact-bundle location,
:mod:`shipit.review.artifacts`) and each finding's originating ``run_id``, so
a posted finding traces back to the pass → prompt → raw output that emitted it.
Since RVW03-WS07 a ``lab run`` round additionally carries ``round.cell`` — the
experiment **Cell** tag (cell id + the full idempotency key,
:func:`shipit.review.cell.run_key`) that makes banked cell results
reusable-by-key and curve-reportable (``None`` on every non-cell round).

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

from ..finding import Disposition, JudgedFinding
from ..harness.eval.store import REVIEW_ROUNDS_KIND, append_record, read_records
from ..harness.eval.variant import label_from_env, variant_of
from ..identity import repo_from_slug
from .dimensions import fanout_variant_text
from .instructions import load_instructions
from .schema import finding_from_dict

#: Bump when the record's field set changes, so an aggregator can read mixed stores
#: (the same convention as :data:`shipit.harness.eval.record.SCHEMA_VERSION`).
#: 2 added ``round.findings[].duplicate_of`` (the fan-out dedup edge, RVW02-WS04).
#: 3 added, RVW03-WS02, ``round.id`` / ``round.artifacts`` (the per-round
#: artifact-bundle location) and ``round.findings[].run_id`` (the finding↔pass
#: correlation); and, RVW03-WS04, per-run ``round.runs[].usage`` (token usage
#: measured from the CLI's own output at launch-result level — no transcript
#: join) with a real ``round.usage.total_tokens`` summed from it, plus
#: ``round.runs[].reasoning`` as a stamp of the argv ACTUALLY used (absent = no
#: level applied), never an echoed config value.
#: 4 added ``round.cell`` — the experiment Cell tag a ``lab run`` stamps
#: (cell id + full idempotency key, :func:`shipit.review.cell.run_key`;
#: ``None`` for every non-cell round), RVW03-WS07.
SCHEMA_VERSION = 4


def dispositioned(
    review: Mapping[str, Any], *, run_id: str | None = None
) -> list[JudgedFinding]:
    """Every finding of a review dict, paired with its disposition. PURE.

    Maps each ``comments[]`` entry through the ONE trust boundary
    (:func:`shipit.review.schema.finding_from_dict`) — the SAME coercion the
    posting path applies, so the record can never disagree with what was posted.
    This is the SINGLE-PASS default (the offline replay): with no calibrator
    routing anything out, the whole output reaches the PR/record, so every
    finding is ``post`` and canonical (no dedup, no ``duplicate_of``). The PR
    path's fan-out (RVW02-WS04) supplies the Calibrator's real routing
    (``drop-unverified`` / ``nit-suppressed`` / ``out-of-scope``, plus the dedup
    edge) as :class:`JudgedFinding`\\ s via ``record_round(findings=…)`` instead.

    ``run_id`` (RVW03-WS02) is the single pass's run id, stamped on every
    finding — in a one-pass pipeline every finding originates from that one
    run; ``None`` (a caller with no per-run identity) leaves the correlation
    absent, exactly as before.
    """
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
    """Assemble the review-round record — one JSONL line per review round. PURE.

    ``review`` supplies the review's own summary layer (status + the coverage
    attestation, read defensively — the agy path is schema-unenforced);
    ``findings`` is the FULL judged set with dispositions (:func:`dispositioned`
    or, post-WS04, the calibrator's routing) — dropped findings ride along with
    their disposition, never erased (the Opportunity-harvest seam), each
    carrying the ``run_id`` of its originating pass (RVW03-WS02). ``pr`` is
    ``None`` for an offline range replay (no PR was touched); ``base_sha`` /
    ``head_sha`` are the range reviewed. ``variant`` is the review-instructions
    content-hash (+ optional A/B label) — the experiment-arm handle; ``runs``
    carries every contributing run's entry (run id, variant hash, per-run
    ``usage``, applied ``reasoning`` — the dimension passes + Calibrator fill
    it; the single-pass replay contributes its one range pass). ``duration_ms`` /
    ``total_tokens`` are the round's cost: ``total_tokens`` is the sum of the
    runs' CLI-REPORTED usage (RVW03-WS04, measured at launch-result level) and
    ``None`` only when no contributing run reported any — the explicit
    latency-only marker, never a fabricated zero. ``round_id`` / ``artifacts_dir``
    (RVW03-WS02) are the round's identity and the directory its per-run artifact
    bundles live under — ``round.id`` / ``round.artifacts``, what makes a round's
    bundles discoverable from its record (``None`` for a pipeline with no bundles).
    ``cell`` (RVW03-WS07) is the experiment Cell tag a ``lab run`` stamps — the
    cell id + the full idempotency key (:func:`shipit.review.cell.run_key`), what
    makes a banked record reusable-by-key and curve-reportable; ``None`` (every
    non-cell round) leaves ``round.cell`` null.
    """
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
    """One judged finding as record data: the domain fields + its routing.

    The severity/disposition enums serialize as their wire values (the SAME
    tokens the machine marker and the domain vocabulary use), so the store is
    greppable and the report can filter dispositions without an enum table.
    ``duplicate_of`` is the fan-out dedup edge (``None`` for a canonical): a
    merged-away duplicate carries its twin's ``post`` disposition but never
    reached the PR, so the report reads ``disposition == post AND duplicate_of is
    None`` as "posted" — never the raw disposition alone (RVW02-WS04).
    ``run_id`` is the originating pass (RVW03-WS02) — the ``round.runs`` entry,
    and per-run artifact bundle, this finding traces back to."""
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
    """Build one round record for ``review`` and append it to the repo's store.

    The I/O boundary around :func:`build`: stamps the UTC timestamp, resolves
    the store key from ``repo_slug`` (the canonical ``owner/name`` — the same
    :class:`~shipit.identity.Repo` identity the eval store keys on, ADR-0024),
    and content-hashes the review INSTRUCTIONS as the round's **Variant**
    (:func:`~shipit.harness.eval.variant.variant_of` — the same ``sha256:``
    scheme as the role-prompt variant; the instructions are the prompt a
    review A/B edits, so identical instructions pool across PRs and an edited
    prompt separates arms) with any :data:`~shipit.harness.eval.variant.VARIANT_LABEL_ENV`
    label. For a dimension fan-out round, ``dimension_names`` — the RESOLVED
    pass set the round actually ran (never ``None``-means-default: the caller
    resolves; ``None`` here means "not a dimension fan-out", the single-pass
    and incremental rounds) — folds the dimensions' prompt material (names,
    titles, focus texts, plus any per-dimension ``dimension_overrides``) into
    the hashed text (:func:`~shipit.review.dimensions.fanout_variant_text`,
    #713): the focus texts live in code, not the instructions file, so two
    arms differing only by dimension set would otherwise stamp one variant and
    pool in ``eval score``. Returns the store path the record landed in.

    ``findings`` is the FULL judged set with the Calibrator's real dispositions
    (the RVW02-WS04 fan-out passes it; routed-out findings included, never
    erased); ``None`` — the single-pass replay — falls back to
    :func:`dispositioned` (everything ``post``). ``runs`` carries the
    contributing runs' entries (run ids, per-run variant hashes, artifact bundle
    paths, per-run ``usage``, applied ``reasoning``: every dimension pass + the
    calibrator) onto ``round.runs``; ``total_tokens`` the round's CLI-measured
    token total (RVW03-WS04 — ``None`` = no run reported usage, the latency-only
    marker). ``round_id`` / ``artifacts_dir`` (RVW03-WS02) land as ``round.id`` /
    ``round.artifacts`` — the round's identity and its bundles' location, so the
    artifact trail is discoverable from the record. ``cell`` (RVW03-WS07) lands
    as ``round.cell`` — the ``lab run`` experiment tag (cell id + the full
    idempotency key); ``None`` for every non-cell round.

    RAISES on failure (a malformed slug, an unreadable instructions file, an
    unwritable store): the caller owns the failure posture — the review-path tee
    wraps this fail-open, the offline replay propagates (the record is its
    product). ``base_dir`` overrides the store family root (tests); ``env``
    injects the label read.
    """
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
    """The head SHA ``reviewer`` most recently reviewed on PR ``pr`` — the
    incremental round's fix-range BASE (RVW02-WS06, ADR-0045), or ``None``.

    Reads the repo's review-round store (:func:`~shipit.harness.eval.store.read_records`,
    the SAME origin-keyed store the tee writes to at generate time) and returns
    the ``round.range.head`` of the most-recent record that:

      * belongs to THIS PR (``round.pr == pr``) and THIS reviewer
        (``round.reviewer == reviewer``) — one reviewer's own review history, so
        a co-reviewer's rounds never mis-scope this one's fix range; and
      * reviewed a DIFFERENT head than the one now being reviewed
        (``head != new_head``) — a re-review of the SAME head (an idempotent
        re-request) is not a prior round to diff against.

    Records are append-ordered chronological, so the LAST matching record is the
    most recent; its head is the last commit this reviewer saw. ``None`` when the
    reviewer has no prior differing-head record for this PR — a first round, or a
    round whose predecessor's record write failed (the tee is fail-open): both
    correctly fall through to a full round (fail toward over-reviewing). The
    replay path's records (``round.pr is None``) never match a real PR number, so
    an offline replay can never be mistaken for a prior PR round.

    ``base_dir`` overrides the store family root (tests), as on the writers.
    """
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
    """The current UTC time as an ISO-8601 string (the record's ``round.timestamp``)."""
    return _dt.datetime.now(_dt.UTC).isoformat()
