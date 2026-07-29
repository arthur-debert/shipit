"""The convergence curve: a cell's scored trajectory over its sweeps, read at
equal budget against its baseline.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..finding import Severity
from .cell import CONTROL_AXIS, Cell, key_tuple
from .groundtruth import Fixture
from .labrun import plan_points, resolve_pins
from .scorer import UNDERPOWERED_FLOOR, VariantScore, score_records

__all__ = ["CellCurve", "CurvePoint", "convergence_curve", "render_curve_report"]

_HEADLINE_TIERS = (Severity.CRITICAL, Severity.MAJOR)


@dataclass(frozen=True)
class CurvePoint:
    """``tokens_complete`` false means the sum is a floor, not the truth."""

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
        return self.banked < self.expected

    @property
    def recall(self) -> float | None:
        return self.recalled / self.positives if self.positives else None

    @property
    def precision(self) -> float | None:
        adjudicated = self.recalled + self.false_positives
        return self.recalled / adjudicated if adjudicated else None

    @property
    def underpowered(self) -> bool:
        return self.positives < UNDERPOWERED_FLOOR

    @property
    def minutes(self) -> float:
        return self.duration_ms / 60_000


@dataclass(frozen=True)
class CellCurve:
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
    by_key: dict[tuple, Mapping[str, Any]] = {}
    for record in records:
        tag = _cell_tag(record)
        assert tag is not None  # filtered by the caller
        if (kt := key_tuple(tag)) is not None:  # skip a corrupt key
            by_key[kt] = record
    return list(by_key.values())


def _pooled_score(
    fixture: Fixture, records: Sequence[Mapping[str, Any]]
) -> VariantScore | None:
    """One arm: an informed sweep's prompt hashes to its own variant, so they
    pool under a synthetic uniform one first."""
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
    """Filtered to ``cell``'s own expected key set, so a superseded prompt can
    never overstate recall."""
    expected_keys = [
        point.key
        for point in plan_points(
            cell, resolve_pins(cell, fixture), variant_hash=variant_hash
        )
    ]
    expected_tuples = {key_tuple(key) for key in expected_keys}
    tagged = [
        record
        for record in records
        if (tag := _cell_tag(record)) is not None and key_tuple(tag) in expected_tuples
    ]
    deduped = _dedupe_by_key(tagged)
    banked_tuples = {
        kt for record in deduped if (kt := key_tuple(_cell_tag(record))) is not None
    }
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


#: Interpolated identity strings ride through record stores.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    return _CONTROL_CHARS.sub("·", text)


def _fmt_tokens(point: CurvePoint) -> str:
    if point.tokens is None:
        return "n/a (latency-only)"
    rendered = f"{point.tokens / 1_000_000:.2f}Mtok"
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
    """The treatment's curve then its baseline's, so the equal-budget comparison
    reads off adjacent lines; only a real control heads as ``(control)``."""
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
        kind = (
            "control"
            if baseline.axis == CONTROL_AXIS
            else f"axis: {_sanitize(baseline.axis)}"
        )
        lines += _curve_lines(
            baseline,
            title=f"baseline {_sanitize(baseline.cell_id)} ({kind}):",
        )
    return "\n".join(lines) + "\n"
