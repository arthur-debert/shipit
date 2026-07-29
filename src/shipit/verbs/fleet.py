"""``shipit fleet`` — fleet-wide verification over the declared portfolio."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import click

from .. import buildid, config, fleetsweep, identity
from ..fleetsweep import SweepError
from ._errors import cli_errors
from ._params import json_option
from ._render import emit

logger = logging.getLogger("shipit.fleet")


@click.group(
    name="fleet",
    help=(
        "Fleet-wide verification over the declared [project.portfolio].\n\n"
        "`sweep` runs every applicable tool verb in a fresh Tree per portfolio "
        "repo under the candidate shipit build and emits the per-tool x "
        "per-repo matrix report. `--help` is the map."
    ),
)
def fleet() -> None:
    """Root of the ``fleet`` subcommand group; verbs are attached below."""


@fleet.command(name="sweep")
@click.option(
    "--repo",
    "repos",
    multiple=True,
    metavar="OWNER/NAME",
    help=(
        "Portfolio repo to sweep (repeatable) — a re-run selector after a "
        "shipit-side fix. Default: every [project.portfolio] entry."
    ),
)
@click.option(
    "--tool",
    "tools",
    multiple=True,
    type=click.Choice(list(fleetsweep.SWEEP_TOOLS)),
    help="Tool verb to run (repeatable). Default: every swept tool.",
)
@click.option(
    "--source-root",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Root of the local portfolio checkouts the [project.portfolio] `path` "
        f"entries index into. Default {fleetsweep.DEFAULT_SOURCE_ROOT}."
    ),
)
@click.option(
    "--shipit-exec",
    "shipit_exec",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "The candidate shipit build every Tree invocation runs (the sanctioned "
        "SHIPIT_EXEC override, ADR-0033). Default: the running build."
    ),
)
@click.option(
    "--keep-trees",
    is_flag=True,
    help=(
        "Leave the sweep Trees on disk for post-mortem (the tree gc ladder "
        "reclaims them later). Default: each Tree is removed after its row."
    ),
)
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Where to write the JSON report artifact. Default: "
        f"{fleetsweep.REPORT_PATH} on a full sweep; a --repo/--tool-filtered "
        "re-run renders to the terminal only unless --out is given, so a "
        "partial matrix never overwrites the committed exit-gate evidence."
    ),
)
@json_option
def sweep_cmd(
    repos: tuple[str, ...],
    tools: tuple[str, ...],
    source_root: Path | None,
    shipit_exec: Path | None,
    keep_trees: bool,
    out: Path | None,
    as_json: bool,
) -> None:
    """Run every shipped tool verb against every portfolio repo it applies to."""
    raise SystemExit(
        run_sweep(
            repos=repos,
            tools=tools,
            source_root=source_root,
            shipit_exec=shipit_exec,
            keep_trees=keep_trees,
            out=out,
            as_json=as_json,
        )
    )


@cli_errors
def run_sweep(
    *,
    repos: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    source_root: Path | None = None,
    shipit_exec: Path | None = None,
    keep_trees: bool = False,
    out: Path | None = None,
    as_json: bool = False,
    sweep_fn: Callable[..., fleetsweep.SweepReport] | None = None,
) -> int:
    """Read the portfolio → orchestrate the sweep → render + persist the report; returns 0 when no cell is red, 1 otherwise."""
    cfg = config.load(Path(config.CONFIG_NAME))
    entries = fleetsweep.load_portfolio(cfg)
    if repos:
        try:
            wanted = {identity.repo_from_slug(slug).slug for slug in repos}
        except ValueError as exc:
            raise SweepError(f"invalid --repo selector: {exc}") from exc
        by_slug = {identity.repo_from_slug(entry.repo).slug: entry for entry in entries}
        unknown = sorted(wanted - by_slug.keys())
        if unknown:
            raise SweepError(
                f"not in [project.portfolio]: {', '.join(unknown)} — the sweep "
                "iterates exactly the declared portfolio (ADR-0033)"
            )
        entries = tuple(entry for slug, entry in by_slug.items() if slug in wanted)
    candidate = fleetsweep.resolve_candidate(shipit_exec)
    sha = buildid.build_sha()
    sweep_fn = sweep_fn or fleetsweep.sweep
    report = sweep_fn(
        entries,
        candidate=candidate,
        candidate_build=sha.value if sha is not None else None,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source_root=source_root or fleetsweep.DEFAULT_SOURCE_ROOT,
        tools=tuple(tools) or fleetsweep.SWEEP_TOOLS,
        keep_trees=keep_trees,
    )
    emit(report, format_sweep, as_json=as_json)
    filtered = bool(repos or tools)
    target = out if out is not None else (None if filtered else fleetsweep.REPORT_PATH)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        if not as_json:
            print(f"report written to {target}")
        logger.info(
            "fleet sweep report written",
            extra={"report_path": str(target), "red_cells": report.red_cells},
        )
    return report.verdict()


_STATUS_LABELS = {
    fleetsweep.STATUS_PASS: "pass",
    fleetsweep.STATUS_FAIL: "FAIL",
    fleetsweep.STATUS_NOT_APPLICABLE: "n/a",
    fleetsweep.STATUS_EXPECTED_FAIL: "xfail",
}


def format_sweep(report: fleetsweep.SweepReport) -> str:
    """The pure text renderer: the matrix table, the per-repo adoption-ready lines, and the fleet verdict."""
    if not report.repos:
        return "fleet sweep: no portfolio repos selected."
    headers = ["REPO", *(tool.upper() for tool in report.tools)]
    rows = []
    for row in report.repos:
        by_tool = {cell.tool: cell for cell in row.cells}
        rows.append(
            [
                row.entry.repo,
                *(
                    _STATUS_LABELS.get(by_tool[tool].status, by_tool[tool].status)
                    if tool in by_tool
                    else "-"
                    for tool in report.tools
                ),
            ]
        )
    all_rows = [headers, *rows]
    widths = [max(len(r[col]) for r in all_rows) for col in range(len(headers))]
    table = "\n".join(
        "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip()
        for row in all_rows
    )
    summaries = "\n".join(row.summary() for row in report.repos)
    ready = sum(1 for row in report.repos if row.adoption_ready)
    build = report.candidate_build or "unknown"
    verdict = (
        "matrix green — TOL01 exit gate holds"
        if report.all_green
        else f"{report.red_cells} red cell(s) — fix in shipit, re-run"
    )
    footer = (
        f"fleet sweep: {len(report.repos)} repo(s), {ready} adoption-ready, "
        f"{verdict} (candidate build {build})"
    )
    return f"{table}\n\n{summaries}\n\n{footer}"
