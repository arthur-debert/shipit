"""curve — the **convergence curve**: a cell's scored trajectory over its sweeps.

The Review Lab's read side for cells (ADR-0049, RVW03-WS07): ``lab report
<cell>`` renders the objective the lab optimizes — a CONVERGENCE CURVE, never
a round-1 score. For each cumulative sweep point ``k`` the curve reports what
sweeps ``1..k`` of the cell achieved together: cumulative major-or-worse
recall against the fixture's confirmed labels, cumulative false positives and
adjudicated precision, token cost, and latency — computed by pooling the
banked, cell-tagged **Review-round records** through the ONE deterministic
scorer (:func:`shipit.review.scorer.score_records`; zero tokens, zero LLM,
free to re-run).

Comparisons happen AT EQUAL BUDGET (ADR-0049): each point carries recall per
million tokens and recall per minute — two separate normalization views — and
the baseline cell's curve renders beside the treatment's, so a configuration
that converges by sweep 2 at half the cost shows up as the win it is. Token
cost reads ``round.usage.total_tokens`` when present (the RVW03-WS04 capture)
and marks the point **latency-only** otherwise — a missing measurement is
announced, never zero-filled. Underpowered tiers keep their marker
(:data:`shipit.review.scorer.UNDERPOWERED_FLOOR` passes through): a 0/3-style
number can never masquerade as signal.

All PURE (:func:`convergence_curve` is a function of cell + fixture + record
dicts; :func:`render_curve_report` a function of curves): the CLI boundary
lives in :mod:`shipit.verbs.lab.report`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..finding import Severity
from .cell import Cell, key_tuple
from .groundtruth import Fixture
from .labrun import plan_points, resolve_pins
from .scorer import UNDERPOWERED_FLOOR, VariantScore, score_records

__all__ = ["CellCurve", "CurvePoint", "convergence_curve", "render_curve_report"]

#: The severity tiers the curve's headline recall counts — the fixture's
#: major-or-worse focus (ADR-0048: ≥25 major-or-worse labels at v1; the
#: merge-block test). Minor/nit labels still score in ``shipit eval score``.
_HEADLINE_TIERS = (Severity.CRITICAL, Severity.MAJOR)


@dataclass(frozen=True)
class CurvePoint:
    """One cumulative sweep point: what sweeps ``1..sweep`` achieved together.

    ``records`` counts the banked rounds pooled into this point. ``expected``
    is how many DECLARED points this sweep has (pins × replicates) and
    ``banked`` how many of them have a record — a sweep is :pyattr:`missing`
    whenever ``banked < expected``, so an interrupted multi-pin run renders the
    gap (and how many points are short) instead of a falsely-complete curve.
    ``tokens`` is the cumulative ``round.usage.total_tokens`` sum over records
    that carry one (``None`` when none do — the latency-only case);
    ``tokens_complete`` says whether EVERY pooled record carried a count, so a
    partial sum renders as the floor it is (``≥``), never as the truth.
    """

    sweep: int
    records: int
    expected: int
    banked: int
    positives: int
    recalled: int
    false_positives: int
    unadjudicated: int
    tokens: int | None
    tokens_complete: bool
    duration_ms: int

    @property
    def missing(self) -> bool:
        """True when a declared point of this sweep has no banked record yet —
        the curve renders the gap and how to fill it, never silently truncates."""
        return self.banked < self.expected

    @property
    def recall(self) -> float | None:
        """Cumulative major-or-worse recall, or ``None`` with no denominator."""
        return self.recalled / self.positives if self.positives else None

    @property
    def precision(self) -> float | None:
        """Adjudicated precision: recalled real labels vs banked-not-real
        matches. Unadjudicated emissions are UNKNOWN to the corpus and sit in
        neither numerator nor denominator — they render beside it instead."""
        adjudicated = self.recalled + self.false_positives
        return self.recalled / adjudicated if adjudicated else None

    @property
    def underpowered(self) -> bool:
        """ADR-0048's power marker on the headline denominator."""
        return self.positives < UNDERPOWERED_FLOOR

    @property
    def minutes(self) -> float:
        return self.duration_ms / 60_000


@dataclass(frozen=True)
class CellCurve:
    """One cell's whole convergence curve + the identity facts the render cites."""

    cell_id: str
    axis: str
    fixture_version: int
    sweep_mode: str
    points: tuple[CurvePoint, ...]


def _cell_tag(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    tag = record.get("round.cell")
    return tag if isinstance(tag, Mapping) else None


def _dedupe_by_key(
    records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Last-record-wins per FULL idempotency key: a ``--force`` re-run
    supersedes the record it re-ran; both never score together."""
    by_key: dict[tuple, Mapping[str, Any]] = {}
    for record in records:
        tag = _cell_tag(record)
        assert tag is not None  # filtered by the caller
        by_key[key_tuple(tag)] = record
    return list(by_key.values())


def _pooled_score(
    fixture: Fixture, records: Sequence[Mapping[str, Any]]
) -> VariantScore | None:
    """Score ``records`` as ONE pooled arm through the one deterministic
    scorer. A cell's sweeps are one experiment (and an informed sweep's
    composed instructions hash to per-sweep variants), so the records pool
    under a synthetic uniform variant before scoring — the matching, posted
    read, and denominators stay the scorer's, byte-for-byte."""
    pooled = [
        {**record, "round.variant": {"content_hash": "cell-pool", "label": None}}
        for record in records
    ]
    report = score_records(fixture, pooled)
    if not report.variants:
        return None
    [variant_score] = report.variants
    return variant_score


def _usage_int(record: Mapping[str, Any], key: str) -> int | None:
    usage = record.get("round.usage")
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def convergence_curve(
    cell: Cell,
    fixture: Fixture,
    records: Sequence[Mapping[str, Any]],
    *,
    variant_hash: str,
) -> CellCurve:
    """``cell``'s convergence curve from the banked records. PURE, deterministic.

    Filters to the records matching THIS run's full expected key set — the
    ADR-0049 idempotency keys (cell, fixture version, pin, variant, replicate,
    sweep) of ``cell``'s own plan at ``variant_hash`` (the content hash of the
    cell's BASE instructions, computed by the boundary that reads them). Nothing
    else pools in: a record from a DIFFERENT instructions variant of the same
    cell (edit the prompt, the hash changes), a pin outside the cell's declared
    subset, or a foreign cell id / fixture version is excluded, so the curve
    scores only the current arm and never overstates recall/FP/cost by mixing
    superseded prompts or stray pins. ``--force`` re-runs are then superseded by
    key (last wins), and each cumulative prefix ``sweeps 1..k`` scores as one
    pooled arm. A sweep whose declared points are not all banked yields a
    ``missing`` point carrying the prior sweeps' cumulative numbers — the gap
    (and how many points are short) renders, the curve never silently shortens.
    """
    expected_keys = [
        point.key
        for point in plan_points(
            cell, resolve_pins(cell, fixture), variant_hash=variant_hash
        )
    ]
    # Index the expected keys once and match records by O(1) tuple membership —
    # `lab report` loads EVERY banked record of each pinned repo, so an
    # any(record_matches_key ...) scan per record would be O(records × keys).
    expected_tuples = {key_tuple(key) for key in expected_keys}
    tagged = [
        record
        for record in records
        if (tag := _cell_tag(record)) is not None and key_tuple(tag) in expected_tuples
    ]
    deduped = _dedupe_by_key(tagged)
    banked_tuples = {key_tuple(_cell_tag(record)) for record in deduped}
    points = []
    for sweep in range(1, cell.sweeps + 1):
        keys_this_sweep = [key for key in expected_keys if key["sweep"] == sweep]
        banked_this_sweep = sum(
            key_tuple(key) in banked_tuples for key in keys_this_sweep
        )
        subset = [
            record
            for record in deduped
            if isinstance(sweep_of := _cell_tag(record).get("sweep"), int)
            and sweep_of <= sweep
        ]
        score = _pooled_score(fixture, subset)
        if score is None:
            positives = recalled = fps = unadj = 0
        else:
            headline = [t for t in score.tiers if t.severity in _HEADLINE_TIERS]
            positives = sum(t.positives for t in headline)
            recalled = sum(t.recalled for t in headline)
            fps = len(score.false_positives)
            unadj = len(score.unadjudicated) + len(score.near_misses)
        token_counts = [
            count
            for record in subset
            if (count := _usage_int(record, "total_tokens")) is not None
        ]
        points.append(
            CurvePoint(
                sweep=sweep,
                records=len(subset),
                expected=len(keys_this_sweep),
                banked=banked_this_sweep,
                positives=positives,
                recalled=recalled,
                false_positives=fps,
                unadjudicated=unadj,
                tokens=sum(token_counts) if token_counts else None,
                tokens_complete=bool(subset) and len(token_counts) == len(subset),
                duration_ms=sum(
                    _usage_int(record, "duration_ms") or 0 for record in subset
                ),
            )
        )
    return CellCurve(
        cell_id=cell.id,
        axis=cell.axis,
        fixture_version=cell.fixture_version,
        sweep_mode=cell.sweep_mode,
        points=tuple(points),
    )


# --- rendering (text; the CLI's output layer) ---------------------------------

#: Control characters that must never reach the terminal verbatim — the same
#: CWE-150 guard as the scorer's render: interpolated identity strings ride
#: through record stores, so they are sanitized, not trusted.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    return _CONTROL_CHARS.sub("·", text)


def _fmt_tokens(point: CurvePoint) -> str:
    if point.tokens is None:
        return "n/a (latency-only)"
    rendered = f"{point.tokens / 1_000_000:.2f}Mtok"
    # A partial sum is a floor, not the truth — say so (RVW03-WS04 lands the
    # capture; mixed stores are the transition's normal).
    return rendered if point.tokens_complete else f"≥{rendered} (partial)"


def _fmt_recall(point: CurvePoint) -> str:
    if point.recall is None:
        return "-/- (no scoreable labels)"
    marker = "  [UNDERPOWERED]" if point.underpowered else ""
    return f"{point.recalled}/{point.positives} ({point.recall:.0%}){marker}"


def _fmt_precision(point: CurvePoint) -> str:
    if point.precision is None:
        return "n/a"
    return f"{point.precision:.0%}"


def _per_budget(point: CurvePoint) -> tuple[str, str]:
    """The two equal-budget normalization views (ADR-0049): recall per million
    tokens and recall per minute — ``n/a`` whenever either side is missing."""
    recall = point.recall
    per_mtok = "n/a"
    if recall is not None and point.tokens is not None and point.tokens > 0:
        per_mtok = f"{recall / (point.tokens / 1_000_000):.1%}/Mtok"
    per_minute = "n/a"
    if recall is not None and point.duration_ms > 0:
        per_minute = f"{recall / point.minutes:.1%}/min"
    return per_mtok, per_minute


def _curve_lines(curve: CellCurve, *, title: str) -> list[str]:
    lines = [title]
    for point in curve.points:
        per_mtok, per_minute = _per_budget(point)
        head = (
            f"  sweep {point.sweep}: recall {_fmt_recall(point)}  "
            f"FP {point.false_positives}  precision {_fmt_precision(point)}  "
            f"unadjudicated {point.unadjudicated}"
        )
        cost = (
            f"    cost: {_fmt_tokens(point)}, {point.minutes:.1f} min  —  "
            f"equal-budget: {per_mtok}, {per_minute}  "
            f"({point.records} record(s))"
        )
        lines.append(head)
        lines.append(cost)
        if point.missing:
            lines.append(
                f"    [missing] sweep {point.sweep}: "
                f"{point.expected - point.banked} of {point.expected} declared "
                "point(s) unbanked — cumulative numbers above carry the prior "
                "sweeps; `shipit lab run` fills the point"
            )
    if not curve.points:
        lines.append("  (cell declares zero sweeps — nothing to render)")
    return lines


def render_curve_report(curve: CellCurve, baseline: CellCurve | None = None) -> str:
    """The convergence-curve report as text. Deterministic.

    The treatment's curve, then (unless the cell IS the control) the baseline
    cell's curve rendered with the SAME machinery, so the equal-budget
    comparison — recall per Mtok and per minute at every cumulative sweep
    point — reads off two adjacent lines instead of being computed in the
    reader's head.
    """
    lines = [
        f"convergence curve — cell {_sanitize(curve.cell_id)} "
        f"(axis: {_sanitize(curve.axis)}; {curve.sweep_mode} sweeps) — "
        f"fixture v{curve.fixture_version}",
        "recall counts major-or-worse confirmed labels of the cell's pins; "
        "comparisons read at EQUAL BUDGET (per-Mtok / per-minute views).",
        "",
    ]
    lines += _curve_lines(curve, title=f"cell {_sanitize(curve.cell_id)}:")
    if baseline is not None:
        lines.append("")
        lines += _curve_lines(
            baseline,
            title=f"baseline {_sanitize(baseline.cell_id)} (control):",
        )
    return "\n".join(lines) + "\n"
