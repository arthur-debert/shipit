"""``shipit eval report`` — aggregate the local JSONL eval store (HAR02-WS04).

A THIN wrapper over DuckDB/SQL (PRD har02 module #6): the harness terminal-hooks
append one **eval record** per run to a never-committed JSONL store keyed by repo
(:mod:`shipit.harness.eval.store`); this verb reads that store *directly* with
DuckDB's ``read_json_auto`` and rolls it up four ways —

- **by role** (``gen_ai.agent.name``): how implementer runs are doing vs shepherd
  vs coordinator (PRD user story 6);
- **by variant** (``eval.variant``): which version of a role prompt / policy
  produced which results, so an A/B is separable (user stories 7-8);
- **by invocation** (``eval.invocation``): which Backend × Model × ReasoningLevel
  launch config produced which results, so configurations are comparable (ADR-0025);
- **trend over time** (the ``eval.timestamp`` day): metrics run-over-run, so the
  harness's improvement (or regression) is a query, not a guess (user story 11).

The aggregation is a pure function of a store *path* (:func:`aggregate`), with the
click command (:func:`cmd`) / :func:`run` as the thin boundary that resolves the
store path and renders the result (ADR-0012 pure-core / thin-boundary). DuckDB is
imported lazily so the rest of the CLI never pays its import cost.

No platform, no infra: the query engine reads the on-disk JSONL and exits — the
substrate stays a local file (ADR-0013).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import click

from ... import gh, identity
from ...harness.eval import store

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
class EvalReport:
    """The full aggregation: the total run count plus the roll-ups (role, variant,
    invocation, day)."""

    total_runs: int
    by_role: list[GroupRow]
    by_variant: list[GroupRow]
    by_invocation: list[GroupRow]
    by_day: list[GroupRow]


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


def aggregate(store_path: str | Path) -> EvalReport:
    """Roll the JSONL eval store at ``store_path`` up by role, variant, and day.

    Pure of any global state: it opens an in-memory DuckDB, reads the file, and
    returns the structured result. A store that does not exist yet (or is empty)
    yields an empty report rather than an error — "no runs recorded" is a valid,
    common state, not a failure.
    """
    path = Path(store_path)
    if not path.exists() or path.stat().st_size == 0:
        return _EMPTY_REPORT

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
        )
    finally:
        con.close()


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


def format_report(report: EvalReport) -> str:
    """Render ``report`` as readable, plain-text sections.

    Kept separate from :func:`aggregate` so the structured result is what tests
    assert on (external behaviour), and the formatting stays trivially eyeballable.
    """
    if report.total_runs == 0:
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
    ]
    return "\n".join(sections)


def _resolve_repo(start: str) -> identity.Repo:
    """The :class:`shipit.identity.Repo` identity for the checkout at ``start``.

    Mirrors the hook's resolution so the verb reads exactly the store the hook
    wrote: keyed by the repo's origin ``owner/name`` identity (ADR-0024), not its
    filesystem path. Derived LOCALLY from the origin remote (offline / Tree-safe).

    ``start`` may name a *file* inside the repo, but the git boundary needs a
    directory, so a file path is normalized to its parent first. Raises
    :class:`shipit.gh.GhError` (no checkout / no origin) or :class:`ValueError`
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

    ``repo_root`` is a path inside the repo whose store to read; it defaults to the
    current checkout, resolved to its origin ``owner/name`` identity (the store
    key, ADR-0024). ``base_dir`` overrides the store root (injected by tests,
    mirroring :func:`shipit.harness.eval.store.store_path`). The store path is
    computed by the store module — the single source of truth — so reader and
    writer can never disagree about where records live. A path that is not a
    checkout (or has no origin) has no per-repo store, so it prints the empty
    report rather than erroring.
    """
    out = out or sys.stdout
    try:
        repo = _resolve_repo(repo_root if repo_root is not None else ".")
    except (gh.GhError, ValueError):
        print(format_report(_EMPTY_REPORT), file=out)
        return 0
    path = store.store_path(repo, base_dir if base_dir is None else Path(base_dir))
    report = aggregate(path)
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
