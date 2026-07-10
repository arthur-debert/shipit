"""``shipit eval report`` — aggregate the local JSONL eval store (HAR02-WS04).

A THIN wrapper over DuckDB/SQL (PRD har02 module #6): the harness terminal-hooks
append one **eval record** per run to a never-committed JSONL store keyed by repo
(:mod:`shipit.harness.eval.store`); this verb reads that store *directly* with
DuckDB's ``read_json_auto`` and rolls it up five ways —

- **by role** (``gen_ai.agent.name``): how implementer runs are doing vs shepherd
  vs coordinator (PRD user story 6);
- **by variant** (``eval.variant``): which version of a role prompt / policy
  produced which results, so an A/B is separable (user stories 7-8);
- **by invocation** (``eval.invocation``): which Backend × Model × ReasoningLevel
  launch config produced which results, so configurations are comparable (ADR-0025);
- **trend over time** (the ``eval.timestamp`` day): metrics run-over-run, so the
  harness's improvement (or regression) is a query, not a guess (user story 11);
- **the review axis** (RVW02-WS03): **review-round records** grouped by their
  review-instructions **Variant** — rounds / findings / posted vs routed-out
  dispositions / cost — JOINED to eval records by run id (``round.runs[].run_id``
  ↔ ``eval.run_id``), so "which prompt variant produced what recall at what
  cost" is one report, not intuition (PRD rvw02, user story 24).

The aggregation is a pure function of the store *paths* (:func:`aggregate`), with
the click command (:func:`cmd`) / :func:`run` as the thin boundary that resolves
both kind stores from the ONE family root and renders the result (ADR-0012
pure-core / thin-boundary). DuckDB is imported lazily so the rest of the CLI
never pays its import cost; the review axis — a small join over the (one line
per round) rounds store — is plain Python over the same JSONL, so nested
finding/disposition lists never depend on DuckDB's struct inference.

No platform, no infra: the query engine reads the on-disk JSONL and exits — the
substrate stays a local file (ADR-0013).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import click

from ... import execrun, identity
from ...harness.eval import store

logger = logging.getLogger("shipit.harness")

#: The eval-record fields the aggregator groups and measures over (the WS01 record
#: shape). Quoted in SQL because the JSON keys carry dots (OTel ``gen_ai.*`` /
#: harness-local ``eval.*`` names), which DuckDB reads as literal column names.
_ROLE_FIELD = '"gen_ai.agent.name"'
_VARIANT_FIELD = '"eval.variant"'
_INVOCATION_FIELD = '"eval.invocation"'
_TIMESTAMP_FIELD = '"eval.timestamp"'
_TOOL_CALLS_FIELD = '"eval.tool_call_count"'

#: Substituted for a NULL group key so a row with no variant (a fail-open run writes
#: ``None``) still aggregates under a stable, printable bucket instead of vanishing.
_NO_VARIANT = "(none)"
_NO_INVOCATION = "(none)"
_UNKNOWN_ROLE = "(unknown)"

#: The variant is persisted as a nested object — ``{"content_hash": …, "label": …}``
#: (:meth:`shipit.harness.eval.variant.Variant.as_record`), which ``read_json_auto``
#: reads as a STRUCT (or, when every row's variant is null, a plain JSON column).
#: Grouping must key on the variant's IDENTITY, not the struct's text repr: the
#: content-hash, refined by the optional A/B label so two arms of the same prompt
#: separate (CONTEXT.md "variant"; PRD user stories 7-8). A null variant — or a
#: null content-hash — buckets under :data:`_NO_VARIANT`; a null label collapses to
#: the bare content-hash. Struct-field access is null-safe across both inferred
#: column types, so a store of only-null variants does not error.
_VARIANT_HASH = f"{_VARIANT_FIELD}.content_hash"
_VARIANT_LABEL = f"{_VARIANT_FIELD}.label"
_VARIANT_KEY = f"""CASE
        WHEN {_VARIANT_FIELD} IS NULL OR {_VARIANT_HASH} IS NULL THEN '{_NO_VARIANT}'
        WHEN {_VARIANT_LABEL} IS NULL THEN CAST({_VARIANT_HASH} AS VARCHAR)
        ELSE CAST({_VARIANT_HASH} AS VARCHAR) || ' [' || CAST({_VARIANT_LABEL} AS VARCHAR) || ']'
    END"""

#: The invocation is persisted as ``{"observed": {backend, model, provider,
#: reasoning_level, permission_mode}, "intended": …}`` (ADR-0025;
#: :func:`shipit.harness.eval.record._invocation_record`). Grouping keys on the
#: OBSERVED launch config — the ``backend/model (reasoning_level)`` tuple the harness
#: compares configurations by — so two Runs of the same role under different backends
#: or reasoning levels separate. A null invocation (an old/fail-open record) buckets
#: under :data:`_NO_INVOCATION`; a null backend/model collapses to ``?``; a null
#: reasoning level drops the suffix. Struct-field access is null-safe across the
#: inferred column types (STRUCT for a populated store, JSON for an all-null one), so a
#: store of only-null invocations does not error. A store predating v3 has NO
#: ``eval.invocation`` column at all (not merely null rows) — DuckDB would fail to bind
#: it, so :func:`aggregate` checks the column's PRESENCE (:func:`_present_columns`) and
#: substitutes the constant ``(none)`` bucket when it is absent (forward-compat within
#: the store's own history — pre-v3 rows show a null invocation dimension).
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
    """One aggregated bucket: a group ``key``, its run ``count``, and the mean
    tool-call count across those runs."""

    key: str
    runs: int
    avg_tool_calls: float


@dataclass(frozen=True)
class ReviewRoundRow:
    """One review-axis bucket: a round-record **Variant** (the experiment-arm
    handle), its round/finding volumes split by disposition, its own recorded
    cost, and the run-id-joined eval-record cost of its contributing runs.

    ``posted`` vs ``dropped`` is the disposition split (``post`` AND canonical
    — a merged-away duplicate shares its twin's ``post`` but never reached the
    PR — vs every routed-out finding + duplicate) — the recall/FP raw material.
    ``joined_runs`` /
    ``avg_run_tokens`` come from the eval store via the ``round.runs[].run_id``
    ↔ ``eval.run_id`` join (zero/None when no contributing run has an eval
    record — today's CLI backends contribute none; WS04's fan-out will).
    """

    key: str
    rounds: int
    findings: int
    posted: int
    dropped: int
    avg_duration_ms: float
    joined_runs: int
    avg_run_tokens: float | None


@dataclass(frozen=True)
class EvalReport:
    """The full aggregation: the total run count plus the roll-ups (role, variant,
    invocation, day) and the review axis (round records by variant, RVW02-WS03)."""

    total_runs: int
    by_role: list[GroupRow]
    by_variant: list[GroupRow]
    by_invocation: list[GroupRow]
    by_day: list[GroupRow]
    review: list[ReviewRoundRow] = field(default_factory=list)


#: The "no runs recorded" report — returned for an empty/missing store AND when the
#: target path has no per-repo store to read (not a checkout / no origin remote).
_EMPTY_REPORT = EvalReport(
    total_runs=0, by_role=[], by_variant=[], by_invocation=[], by_day=[]
)


#: Default roll-up ordering: most runs first, then key — a "top buckets" view for
#: the role/variant groupings. The day trend overrides this (see ``_ORDER_BY_KEY``)
#: because a time series must read chronologically, not by run-count.
_ORDER_BY_RUNS = "runs DESC, key"
#: Chronological ordering for the day roll-up: the key is the ISO date prefix, so
#: ordering by key alone reads oldest→newest regardless of each day's run count.
_ORDER_BY_KEY = "key"


def _group_query(key_expr: str, order_by: str) -> str:
    """A GROUP-BY query rolling the store up by ``key_expr``, ordered by ``order_by``.

    The single ``?`` parameter is the store path; the FROM clause reads the JSONL
    directly. ``order_by`` is a fixed SQL fragment (one of the module ``_ORDER_BY_*``
    constants, never user input) so the rendered table — and the tests — are stable.
    """
    return f"""
        SELECT
            {key_expr} AS key,
            COUNT(*) AS runs,
            AVG(CAST({_TOOL_CALLS_FIELD} AS DOUBLE)) AS avg_tool_calls
        FROM read_json_auto(?, format='newline_delimited')
        GROUP BY 1
        ORDER BY {order_by}
    """


def aggregate(
    store_path: str | Path, rounds_path: str | Path | None = None
) -> EvalReport:
    """Roll the JSONL eval store at ``store_path`` up by role, variant, and day —
    plus, when ``rounds_path`` names the repo's review-rounds store, the review
    axis (:func:`review_axis`: round records by variant, run-id-joined to the
    eval records).

    Pure of any global state: it opens an in-memory DuckDB, reads the files, and
    returns the structured result. A store that does not exist yet (or is empty)
    yields an empty roll-up rather than an error — "no runs recorded" is a valid,
    common state, not a failure — and the two stores are independent: review
    rounds report even when no eval record exists yet (replay against CLI
    backends writes rounds but no eval records).
    """
    review = review_axis(rounds_path, store_path) if rounds_path is not None else []
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

    import duckdb  # lazy: only the eval verb needs the query engine.

    con = duckdb.connect(":memory:")
    try:
        # A store whose rows predate a field's introduction has NO such column at
        # all, so a query referencing it would fail to bind. Consult the inferred
        # schema and fall back to the constant bucket for an absent group column, so
        # the report is tolerant of a mixed-schema store (e.g. pre-v3 records with no
        # `eval.invocation`) instead of raising.
        present = _present_columns(con, str(path))
        role_key = f"COALESCE(CAST({_ROLE_FIELD} AS VARCHAR), '{_UNKNOWN_ROLE}')"
        variant_key = _VARIANT_KEY
        invocation_key = (
            _INVOCATION_KEY
            if _column(_INVOCATION_FIELD) in present
            else f"'{_NO_INVOCATION}'"
        )
        # The day bucket is the ISO timestamp's date prefix — taken as the leading
        # 10 chars so it never depends on DuckDB parsing the timezone offset.
        day_key = f"SUBSTR(CAST({_TIMESTAMP_FIELD} AS VARCHAR), 1, 10)"

        by_role = _run_group(con, role_key, str(path), _ORDER_BY_RUNS)
        by_variant = _run_group(con, variant_key, str(path), _ORDER_BY_RUNS)
        by_invocation = _run_group(con, invocation_key, str(path), _ORDER_BY_RUNS)
        # The day trend orders chronologically by the date key, not by run count,
        # so a busy older day cannot jump ahead of a quieter newer one.
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


def review_axis(
    rounds_path: str | Path | None, eval_path: str | Path
) -> list[ReviewRoundRow]:
    """The review axis: round records grouped by **Variant**, run-id-joined to
    eval records (RVW02-WS03; PRD rvw02 user story 24).

    Plain Python over the two JSONL stores — the rounds store is one line per
    review round (small by construction), and the nested findings/dispositions
    lists stay out of DuckDB's struct inference. Per variant bucket: rounds,
    findings split ``posted`` (disposition ``post`` AND canonical — a merged-away
    duplicate shares its twin's ``post`` but never reached the PR) vs ``dropped``
    (every routed-out finding + duplicate — the recall/FP raw material), the round's own mean
    duration, and the joined eval-record cost — each ``round.runs[].run_id``
    resolved against ``eval.run_id``, averaging the joined runs' total tokens.
    A missing store, a malformed line, or a round with no joinable run degrades
    per-item (skip / None), never errors: the report reads whatever history the
    stores hold. Buckets order most-rounds first, then key — the same "top
    buckets" ordering as the DuckDB roll-ups.
    """
    rounds = _read_jsonl(rounds_path)
    if not rounds:
        return []
    tokens_by_run: dict[str, object] = {}
    for record in _read_jsonl(eval_path):
        run_id = record.get("eval.run_id")
        if run_id:
            tokens_by_run[str(run_id)] = record.get("eval.usage.total_tokens")

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
                "joined": 0,
                "tokens": [],
            },
        )
        bucket["rounds"] += 1
        findings = round_record.get("round.findings")
        for finding in findings if isinstance(findings, list) else []:
            if not isinstance(finding, Mapping):
                continue
            bucket["findings"] += 1
            # "posted" is disposition==post AND canonical: a merged-away
            # duplicate carries its twin's post disposition but never reached the
            # PR, so counting it would double the posted-vs-dropped split
            # (RVW02-WS04 fan-out dedup edge; duplicate_of absent on pre-WS04
            # single-pass records → all such findings are canonical).
            if (
                finding.get("disposition") == "post"
                and finding.get("duplicate_of") is None
            ):
                bucket["posted"] += 1
        usage = round_record.get("round.usage")
        duration = usage.get("duration_ms") if isinstance(usage, Mapping) else None
        if isinstance(duration, (int, float)):
            bucket["durations"].append(float(duration))
        runs = round_record.get("round.runs")
        for run in runs if isinstance(runs, list) else []:
            if not isinstance(run, Mapping):
                continue
            run_id = str(run.get("run_id") or "")
            if run_id and run_id in tokens_by_run:
                bucket["joined"] += 1
                tokens = tokens_by_run[run_id]
                if isinstance(tokens, (int, float)):
                    bucket["tokens"].append(float(tokens))

    rows = [
        ReviewRoundRow(
            key=key,
            rounds=bucket["rounds"],
            findings=bucket["findings"],
            posted=bucket["posted"],
            dropped=bucket["findings"] - bucket["posted"],
            avg_duration_ms=_mean(bucket["durations"]) or 0.0,
            joined_runs=bucket["joined"],
            avg_run_tokens=_mean(bucket["tokens"]),
        )
        for key, bucket in buckets.items()
    ]
    rows.sort(key=lambda row: (-row.rounds, row.key))
    return rows


def _mean(values: list[float]) -> float | None:
    """The arithmetic mean of ``values``, or ``None`` for an empty list."""
    return sum(values) / len(values) if values else None


def _variant_bucket(variant: object) -> str:
    """A round-record variant → its report bucket key — the SAME rendering the
    DuckDB eval roll-up's variant key produces (``hash``, ``hash [label]``, or
    :data:`_NO_VARIANT`), so the two variant axes read alike."""
    if not isinstance(variant, Mapping) or not variant.get("content_hash"):
        return _NO_VARIANT
    content_hash = str(variant["content_hash"])
    label = variant.get("label")
    return f"{content_hash} [{label}]" if label is not None else content_hash


def _read_jsonl(path: str | Path | None) -> list[dict]:
    """Every parseable JSON OBJECT line of ``path`` — missing/empty store → ``[]``.

    Tolerant by design (the stores are local, append-only telemetry): a
    malformed or non-object line is skipped, never an error, so one bad write
    cannot take the whole report down — but it is skipped LOUDLY (RVW03-WS03),
    a warning naming the file and 1-based line number, mirroring
    :func:`shipit.harness.eval.store.read_records`: a corrupted round must
    never silently read as "this arm found nothing". The file is STREAMED
    line-by-line (not ``read_text().splitlines()``) so an unbounded append-only
    store does not allocate the whole file plus a split-line list at once.
    """
    if path is None:
        return []
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict] = []
    with target.open(encoding="utf-8") as fh:
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
    """The bare column name for a quoted SQL field literal (``"eval.invocation"`` →
    ``eval.invocation``) — how it appears in the inferred schema (:func:`_present_columns`)."""
    return field.strip('"')


def _present_columns(con: object, path: str) -> set[str]:
    """The top-level column names DuckDB infers for the store at ``path``.

    A store whose rows all predate a field (a pre-v3 record with no
    ``eval.invocation``) yields NO such column, so a query naming it fails to bind.
    The aggregator consults this to fall back to a constant bucket for an absent group
    column, keeping the report tolerant of the store's own schema history (NOT compat
    with the orphaned path-keyed stores — those stay orphaned)."""
    rows = con.execute(  # type: ignore[attr-defined]
        "DESCRIBE SELECT * FROM read_json_auto(?, format='newline_delimited')",
        [path],
    ).fetchall()
    return {str(row[0]) for row in rows}


def _run_group(con: object, key_expr: str, path: str, order_by: str) -> list[GroupRow]:
    """Execute the group query for ``key_expr`` and map its rows to ``GroupRow``."""
    rows = con.execute(_group_query(key_expr, order_by), [path]).fetchall()  # type: ignore[attr-defined]
    return [
        GroupRow(key=str(key), runs=int(runs), avg_tool_calls=float(avg or 0.0))
        for key, runs, avg in rows
    ]


def _render_section(title: str, key_header: str, rows: list[GroupRow]) -> list[str]:
    """Render one roll-up as an aligned text table (a list of lines)."""
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
    """Render the review axis as an aligned text table (a list of lines).

    The disposition split (posted vs dropped) and the joined eval-run cost are
    the load-bearing columns — what a review-prompt A/B actually compares. A
    variant with no joinable runs prints ``-`` for tokens rather than a fake 0.
    """
    lines = ["Review rounds (by variant):"]
    if not rows:
        lines.append("  (no review rounds)")
        return lines
    key_width = max(len("variant"), *(len(r.key) for r in rows))
    lines.append(
        f"  {'variant':<{key_width}}  {'rounds':>6}  {'findings':>8}  "
        f"{'posted':>6}  {'dropped':>7}  {'avg ms':>8}  {'runs':>4}  {'tokens':>8}"
    )
    for r in rows:
        tokens = f"{r.avg_run_tokens:.0f}" if r.avg_run_tokens is not None else "-"
        lines.append(
            f"  {r.key:<{key_width}}  {r.rounds:>6}  {r.findings:>8}  "
            f"{r.posted:>6}  {r.dropped:>7}  {r.avg_duration_ms:>8.0f}  "
            f"{r.joined_runs:>4}  {tokens:>8}"
        )
    return lines


def format_report(report: EvalReport) -> str:
    """Render ``report`` as readable, plain-text sections.

    Kept separate from :func:`aggregate` so the structured result is what tests
    assert on (external behaviour), and the formatting stays trivially eyeballable.
    The empty-store message fires only when BOTH stores are empty: review rounds
    exist without eval records (a replay against a CLI backend), and must render.
    """
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
    """The :class:`shipit.identity.Repo` identity for the checkout at ``start``.

    Mirrors the hook's resolution so the verb reads exactly the store the hook
    wrote: keyed by the repo's origin ``owner/name`` identity (ADR-0024), not its
    filesystem path. Derived LOCALLY from the origin remote (offline / Tree-safe).

    ``start`` may name a *file* inside the repo, but the git boundary needs a
    directory, so a file path is normalized to its parent first. Raises
    :class:`shipit.execrun.ExecError` (no checkout / no origin) or :class:`ValueError`
    (unparseable remote) — the caller degrades those to an empty report.
    """
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
    """Aggregate the local eval store for a repo and print the report. Returns 0.

    ``repo_root`` is a path inside the repo whose stores to read; it defaults to
    the current checkout, resolved to its origin ``owner/name`` identity (the
    store key, ADR-0024). ``base_dir`` overrides the store FAMILY root (injected
    by tests, mirroring :func:`shipit.harness.eval.store.store_path`) — one root
    resolves BOTH kind stores (eval + review-rounds), which is what makes the
    review-axis join a same-override read. The store paths are computed by the
    store module — the single source of truth — so reader and writer can never
    disagree about where records live. A path that is not a checkout (or has no
    origin) has no per-repo store, so it prints the empty report rather than
    erroring.
    """
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
    """Aggregate the local objective-eval store: by role, by variant, and over time.

    REPO_ROOT is a path inside the repo whose store to read; omitted, it defaults
    to the current directory. The store is the never-committed JSONL the harness
    terminal-hooks append to — this verb only reads it.
    """
    raise SystemExit(run(repo_root))
