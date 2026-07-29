"""``shipit eval report`` — roll the local JSONL eval store up by role, variant, invocation, day, and review Variant."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import click

from ... import execrun, identity
from ...harness.eval import store

logger = logging.getLogger("shipit.harness")

_ROLE_FIELD = '"gen_ai.agent.name"'
_VARIANT_FIELD = '"eval.variant"'
_INVOCATION_FIELD = '"eval.invocation"'
_TIMESTAMP_FIELD = '"eval.timestamp"'
_TOOL_CALLS_FIELD = '"eval.tool_call_count"'

_NO_VARIANT = "(none)"
_NO_INVOCATION = "(none)"
_UNKNOWN_ROLE = "(unknown)"

_VARIANT_HASH = f"{_VARIANT_FIELD}.content_hash"
_VARIANT_LABEL = f"{_VARIANT_FIELD}.label"
_VARIANT_KEY = f"""CASE
        WHEN {_VARIANT_FIELD} IS NULL OR {_VARIANT_HASH} IS NULL THEN '{_NO_VARIANT}'
        WHEN {_VARIANT_LABEL} IS NULL THEN CAST({_VARIANT_HASH} AS VARCHAR)
        ELSE CAST({_VARIANT_HASH} AS VARCHAR) || ' [' || CAST({_VARIANT_LABEL} AS VARCHAR) || ']'
    END"""

_INV_OBSERVED = f"{_INVOCATION_FIELD}.observed"
_INV_BACKEND = f"{_INV_OBSERVED}.backend"
_INV_MODEL = f"{_INV_OBSERVED}.model"
_INV_REASONING = f"{_INV_OBSERVED}.reasoning_level"
_INVOCATION_KEY = f"""CASE
        WHEN {_INVOCATION_FIELD} IS NULL OR {_INV_OBSERVED} IS NULL THEN '{_NO_INVOCATION}'
        ELSE
            COALESCE(CAST({_INV_BACKEND} AS VARCHAR), '?') || '/' ||
            COALESCE(CAST({_INV_MODEL} AS VARCHAR), '?') ||
            CASE
                WHEN {_INV_REASONING} IS NULL THEN ''
                ELSE ' (' || CAST({_INV_REASONING} AS VARCHAR) || ')'
            END
    END"""


@dataclass(frozen=True)
class GroupRow:
    """One aggregated bucket: a group ``key``, its run ``count``, and the mean tool-call count."""

    key: str
    runs: int
    avg_tool_calls: float


@dataclass(frozen=True)
class ReviewRoundRow:
    """One review-axis bucket by Variant; ``avg_round_tokens is None`` means no round reported usage (latency-only), never zero cost."""

    key: str
    rounds: int
    findings: int
    posted: int
    dropped: int
    avg_duration_ms: float
    token_rounds: int
    avg_round_tokens: float | None


@dataclass(frozen=True)
class EvalReport:
    """The full aggregation: the total run count, the roll-ups, and the review axis."""

    total_runs: int
    by_role: list[GroupRow]
    by_variant: list[GroupRow]
    by_invocation: list[GroupRow]
    by_day: list[GroupRow]
    review: list[ReviewRoundRow] = field(default_factory=list)


_EMPTY_REPORT = EvalReport(
    total_runs=0, by_role=[], by_variant=[], by_invocation=[], by_day=[]
)


_ORDER_BY_RUNS = "runs DESC, key"
_ORDER_BY_KEY = "key"


def _group_query(key_expr: str, order_by: str) -> str:
    """A GROUP-BY query rolling the store up by ``key_expr``; the one ``?`` parameter is the store path and ``order_by`` is a fixed fragment, never user input."""
    return f"""
        SELECT
            {key_expr} AS key,
            COUNT(*) AS runs,
            AVG(CAST({_TOOL_CALLS_FIELD} AS DOUBLE)) AS avg_tool_calls
        FROM read_json_auto(?, format='newline_delimited')
        GROUP BY 1
        ORDER BY {order_by}
    """


@contextmanager
def _shared_lock(path: Path) -> Iterator[None]:
    """Hold a shared lock on ``path`` for the wrapped block, so a concurrent appender cannot expose a half-flushed line; ``path`` must exist."""
    with path.open(encoding="utf-8") as fh:
        store.lock_shared(fh)
        yield


def aggregate(
    store_path: str | Path, rounds_path: str | Path | None = None
) -> EvalReport:
    """Roll the JSONL eval store at ``store_path`` up, plus the review axis from ``rounds_path``; a missing or empty store yields an empty roll-up."""
    review = review_axis(rounds_path) if rounds_path is not None else []
    path = Path(store_path)
    if not path.exists() or path.stat().st_size == 0:
        return EvalReport(
            total_runs=0,
            by_role=[],
            by_variant=[],
            by_invocation=[],
            by_day=[],
            review=review,
        )

    import duckdb

    con = duckdb.connect(":memory:")
    try:
        with _shared_lock(path):
            present = _present_columns(con, str(path))
            role_key = f"COALESCE(CAST({_ROLE_FIELD} AS VARCHAR), '{_UNKNOWN_ROLE}')"
            variant_key = _VARIANT_KEY
            invocation_key = (
                _INVOCATION_KEY
                if _column(_INVOCATION_FIELD) in present
                else f"'{_NO_INVOCATION}'"
            )
            day_key = f"SUBSTR(CAST({_TIMESTAMP_FIELD} AS VARCHAR), 1, 10)"

            by_role = _run_group(con, role_key, str(path), _ORDER_BY_RUNS)
            by_variant = _run_group(con, variant_key, str(path), _ORDER_BY_RUNS)
            by_invocation = _run_group(con, invocation_key, str(path), _ORDER_BY_RUNS)
            by_day = _run_group(con, day_key, str(path), _ORDER_BY_KEY)
        total = sum(row.runs for row in by_role)
        return EvalReport(
            total_runs=total,
            by_role=by_role,
            by_variant=by_variant,
            by_invocation=by_invocation,
            by_day=by_day,
            review=review,
        )
    finally:
        con.close()


def review_axis(rounds_path: str | Path | None) -> list[ReviewRoundRow]:
    """Round records grouped by Variant, with each round's own measured duration and token cost."""
    rounds = _read_jsonl(rounds_path)
    if not rounds:
        return []

    buckets: dict[str, dict] = {}
    for round_record in rounds:
        key = _variant_bucket(round_record.get("round.variant"))
        bucket = buckets.setdefault(
            key,
            {
                "rounds": 0,
                "findings": 0,
                "posted": 0,
                "durations": [],
                "tokens": [],
            },
        )
        bucket["rounds"] += 1
        findings = round_record.get("round.findings")
        for finding in findings if isinstance(findings, list) else []:
            if not isinstance(finding, Mapping):
                continue
            bucket["findings"] += 1
            if (
                finding.get("disposition") == "post"
                and finding.get("duplicate_of") is None
            ):
                bucket["posted"] += 1
        usage = round_record.get("round.usage")
        duration = usage.get("duration_ms") if isinstance(usage, Mapping) else None
        if isinstance(duration, (int, float)):
            bucket["durations"].append(float(duration))
        tokens = usage.get("total_tokens") if isinstance(usage, Mapping) else None
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            bucket["tokens"].append(float(tokens))

    rows = [
        ReviewRoundRow(
            key=key,
            rounds=bucket["rounds"],
            findings=bucket["findings"],
            posted=bucket["posted"],
            dropped=bucket["findings"] - bucket["posted"],
            avg_duration_ms=_mean(bucket["durations"]) or 0.0,
            token_rounds=len(bucket["tokens"]),
            avg_round_tokens=_mean(bucket["tokens"]),
        )
        for key, bucket in buckets.items()
    ]
    rows.sort(key=lambda row: (-row.rounds, row.key))
    return rows


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _variant_bucket(variant: object) -> str:
    """A round-record variant → its report bucket key, matching the DuckDB roll-up's variant key."""
    if not isinstance(variant, Mapping) or not variant.get("content_hash"):
        return _NO_VARIANT
    content_hash = str(variant["content_hash"])
    label = variant.get("label")
    return f"{content_hash} [{label}]" if label is not None else content_hash


def _read_jsonl(path: str | Path | None) -> list[dict]:
    """Every parseable JSON object line of ``path`` (missing store → ``[]``), streamed under a shared lock; a malformed line is skipped with a warning."""
    if path is None:
        return []
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict] = []
    with target.open(encoding="utf-8") as fh:
        store.lock_shared(fh)
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "malformed record skipped in %s, line %d: not valid JSON",
                    target,
                    lineno,
                    exc_info=True,
                )
                continue
            if not isinstance(parsed, dict):
                logger.warning(
                    "malformed record skipped in %s, line %d: expected a JSON "
                    "object, got %s",
                    target,
                    lineno,
                    type(parsed).__name__,
                )
                continue
            records.append(parsed)
    return records


def _column(field: str) -> str:
    return field.strip('"')


def _present_columns(con: object, path: str) -> set[str]:
    """The top-level column names DuckDB infers for the store at ``path``."""
    rows = con.execute(  # type: ignore[attr-defined]
        "DESCRIBE SELECT * FROM read_json_auto(?, format='newline_delimited')",
        [path],
    ).fetchall()
    return {str(row[0]) for row in rows}


def _run_group(con: object, key_expr: str, path: str, order_by: str) -> list[GroupRow]:
    rows = con.execute(_group_query(key_expr, order_by), [path]).fetchall()  # type: ignore[attr-defined]
    return [
        GroupRow(key=str(key), runs=int(runs), avg_tool_calls=float(avg or 0.0))
        for key, runs, avg in rows
    ]


def _render_section(title: str, key_header: str, rows: list[GroupRow]) -> list[str]:
    lines = [title]
    if not rows:
        lines.append("  (no runs)")
        return lines
    key_width = max(len(key_header), *(len(r.key) for r in rows))
    lines.append(f"  {key_header:<{key_width}}  {'runs':>5}  {'avg tool calls':>14}")
    for r in rows:
        lines.append(f"  {r.key:<{key_width}}  {r.runs:>5}  {r.avg_tool_calls:>14.2f}")
    return lines


def _render_review_section(rows: list[ReviewRoundRow]) -> list[str]:
    """Render the review axis as an aligned text table (a list of lines)."""
    lines = ["Review rounds (by variant):"]
    if not rows:
        lines.append("  (no review rounds)")
        return lines
    key_width = max(len("variant"), *(len(r.key) for r in rows))
    lines.append(
        f"  {'variant':<{key_width}}  {'rounds':>6}  {'findings':>8}  "
        f"{'posted':>6}  {'dropped':>7}  {'avg ms':>8}  {'avg tokens':>12}"
    )
    for r in rows:
        tokens = (
            f"{r.avg_round_tokens:.0f}"
            if r.avg_round_tokens is not None
            else "latency-only"
        )
        lines.append(
            f"  {r.key:<{key_width}}  {r.rounds:>6}  {r.findings:>8}  "
            f"{r.posted:>6}  {r.dropped:>7}  {r.avg_duration_ms:>8.0f}  "
            f"{tokens:>12}"
        )
    return lines


def format_report(report: EvalReport) -> str:
    """Render ``report`` as readable, plain-text sections."""
    if report.total_runs == 0 and not report.review:
        return "No eval records yet — the store is empty."
    sections = [
        f"Eval report — {report.total_runs} run(s)",
        "",
        *_render_section("By role:", "role", report.by_role),
        "",
        *_render_section("By variant:", "variant", report.by_variant),
        "",
        *_render_section("By invocation:", "invocation", report.by_invocation),
        "",
        *_render_section("Trend (by day):", "day", report.by_day),
        "",
        *_render_review_section(report.review),
    ]
    return "\n".join(sections)


def _resolve_repo(start: str) -> identity.Repo:
    """The Repo identity for the checkout at ``start``; raises ExecError (no checkout/origin) or ValueError (unparseable remote)."""
    cwd = Path(start)
    if cwd.is_file():
        cwd = cwd.parent
    return identity.resolve_repo(str(cwd))


def run(
    repo_root: str | None = None,
    *,
    base_dir: str | Path | None = None,
    out: TextIO | None = None,
) -> int:
    """Aggregate the local eval store for a repo and print the report. Returns 0."""
    out = out or sys.stdout
    try:
        repo = _resolve_repo(repo_root if repo_root is not None else ".")
    except (execrun.ExecError, ValueError):
        print(format_report(_EMPTY_REPORT), file=out)
        return 0
    root = base_dir if base_dir is None else Path(base_dir)
    path = store.store_path(repo, root)
    rounds_path = store.store_path(repo, root, kind=store.REVIEW_ROUNDS_KIND)
    report = aggregate(path, rounds_path)
    print(format_report(report), file=out)
    return 0


@click.command(name="report")
@click.argument("repo_root", required=False)
def cmd(repo_root: str | None) -> None:
    """Aggregate the local objective-eval store: by role, by variant, and over time."""
    raise SystemExit(run(repo_root))
