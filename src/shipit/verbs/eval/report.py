"""``shipit eval report`` — aggregate the local JSONL eval store (HAR02-WS04).

A THIN wrapper over DuckDB/SQL (PRD har02 module #6): the harness terminal-hooks
append one **eval record** per run to a never-committed JSONL store keyed by repo
(:mod:`shipit.harness.eval.store`); this verb reads that store *directly* with
DuckDB's ``read_json_auto`` and rolls it up three ways —

- **by role** (``gen_ai.agent.name``): how implementer runs are doing vs shepherd
  vs coordinator (PRD user story 6);
- **by variant** (``eval.variant``): which version of a role prompt / policy
  produced which results, so an A/B is separable (user stories 7-8);
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

from ... import gh
from ...harness.eval import store

#: The eval-record fields the aggregator groups and measures over (the WS01 record
#: shape). Quoted in SQL because the JSON keys carry dots (OTel ``gen_ai.*`` /
#: harness-local ``eval.*`` names), which DuckDB reads as literal column names.
_ROLE_FIELD = '"gen_ai.agent.name"'
_VARIANT_FIELD = '"eval.variant"'
_TIMESTAMP_FIELD = '"eval.timestamp"'
_TOOL_CALLS_FIELD = '"eval.tool_call_count"'

#: Substituted for a NULL group key so a row with no variant (WS01 writes ``None``)
#: still aggregates under a stable, printable bucket instead of vanishing.
_NO_VARIANT = "(none)"
_UNKNOWN_ROLE = "(unknown)"


@dataclass(frozen=True)
class GroupRow:
    """One aggregated bucket: a group ``key``, its run ``count``, and the mean
    tool-call count across those runs."""

    key: str
    runs: int
    avg_tool_calls: float


@dataclass(frozen=True)
class EvalReport:
    """The full aggregation: the total run count plus the three roll-ups."""

    total_runs: int
    by_role: list[GroupRow]
    by_variant: list[GroupRow]
    by_day: list[GroupRow]


def _group_query(key_expr: str) -> str:
    """A GROUP-BY query rolling the store up by ``key_expr``.

    The single ``?`` parameter is the store path; the FROM clause reads the JSONL
    directly. Ordering is deterministic (most runs first, then key) so the rendered
    table — and the tests — are stable.
    """
    return f"""
        SELECT
            {key_expr} AS key,
            COUNT(*) AS runs,
            AVG(CAST({_TOOL_CALLS_FIELD} AS DOUBLE)) AS avg_tool_calls
        FROM read_json_auto(?, format='newline_delimited')
        GROUP BY 1
        ORDER BY runs DESC, key
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
        return EvalReport(total_runs=0, by_role=[], by_variant=[], by_day=[])

    import duckdb  # lazy: only the eval verb needs the query engine.

    con = duckdb.connect(":memory:")
    try:
        role_key = f"COALESCE(CAST({_ROLE_FIELD} AS VARCHAR), '{_UNKNOWN_ROLE}')"
        variant_key = f"COALESCE(CAST({_VARIANT_FIELD} AS VARCHAR), '{_NO_VARIANT}')"
        # The day bucket is the ISO timestamp's date prefix — taken as the leading
        # 10 chars so it never depends on DuckDB parsing the timezone offset.
        day_key = f"SUBSTR(CAST({_TIMESTAMP_FIELD} AS VARCHAR), 1, 10)"

        by_role = _run_group(con, role_key, str(path))
        by_variant = _run_group(con, variant_key, str(path))
        by_day = _run_group(con, day_key, str(path))
        total = sum(row.runs for row in by_role)
        return EvalReport(
            total_runs=total,
            by_role=by_role,
            by_variant=by_variant,
            by_day=by_day,
        )
    finally:
        con.close()


def _run_group(con: object, key_expr: str, path: str) -> list[GroupRow]:
    """Execute the group query for ``key_expr`` and map its rows to ``GroupRow``."""
    rows = con.execute(_group_query(key_expr), [path]).fetchall()  # type: ignore[attr-defined]
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
        *_render_section("Trend (by day):", "day", report.by_day),
    ]
    return "\n".join(sections)


def _repo_root(start: str) -> str:
    """The git working-tree root for ``start`` (the store's repo key), else ``start``.

    Mirrors the hook's resolution so the verb reads exactly the store the hook
    wrote: keyed by the repo's filesystem root, not its ``owner/repo`` slug.
    """
    try:
        root = gh._git(["rev-parse", "--show-toplevel"], cwd=start).strip()
    except gh.GhError:
        return start
    return root or start


def run(
    repo_root: str | None = None,
    *,
    base_dir: str | Path | None = None,
    out: TextIO | None = None,
) -> int:
    """Aggregate the local eval store for a repo and print the report. Returns 0.

    ``repo_root`` defaults to the current checkout (resolved to its git toplevel);
    ``base_dir`` overrides the store root (injected by tests, mirroring
    :func:`shipit.harness.eval.store.store_path`). The store path is computed by
    the store module — the single source of truth — so reader and writer can never
    disagree about where records live.
    """
    out = out or sys.stdout
    root = _repo_root(repo_root if repo_root is not None else ".")
    path = store.store_path(root, base_dir if base_dir is None else Path(base_dir))
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
