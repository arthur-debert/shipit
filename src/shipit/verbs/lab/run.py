"""``shipit lab run`` — execute one experiment Cell over the replay driver."""

from __future__ import annotations

from pathlib import Path

import click

from ...review.cell import (
    DEFAULT_CELLS_DIR,
    load_baseline_lineage,
    load_cell,
    resolve_cell_path,
)
from ...review.groundtruth import DEFAULT_FIXTURE_PATH, load_fixture
from ...review.labrun import run_cell
from .._errors import cli_errors
from .._help import HelpableCommand


@cli_errors
def run(
    cell_ref: str,
    *,
    checkouts: tuple[str, ...] = (),
    prs: tuple[str, ...] = (),
    force: bool = False,
    fixture_path: str | None = None,
    cells_dir: str | None = None,
    base_dir: Path | None = None,
    launcher=None,
) -> int:
    """Check lineage, then execute the sweep plan; returns an exit code."""
    cells_root = Path(cells_dir) if cells_dir is not None else DEFAULT_CELLS_DIR
    cell = load_cell(resolve_cell_path(cell_ref, cells_root))
    fixture = load_fixture(
        Path(fixture_path) if fixture_path is not None else DEFAULT_FIXTURE_PATH
    )
    load_baseline_lineage(cell, fixture, cells_root)
    run_cell(
        cell,
        fixture,
        checkouts=checkouts,
        pr_subset=prs,
        force=force,
        base_dir=base_dir,
        launcher=launcher,
    )
    return 0


@click.command(
    name="run",
    cls=HelpableCommand,
    help_package=__package__,
    help_resource="lab_run_help.txt",
)
@click.argument("cell_ref", metavar="CELL")
@click.option(
    "--checkout",
    "checkouts",
    multiple=True,
    help=(
        "Path to a local clone of a fixture-pinned repo (repeatable; the "
        "current directory is always a candidate). Replay is offline — each "
        "clone must already have the pinned commits fetched."
    ),
)
@click.option(
    "--pr",
    "prs",
    multiple=True,
    help="Narrow this session to the named fixture pin id(s) (repeatable).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-execute banked points instead of reusing them (the explicit "
    "re-run path; a re-run record supersedes its predecessor in the report).",
)
@click.option(
    "--fixture",
    "fixture_path",
    default=None,
    help="Ground-truth fixture path (default: lab/fixture.toml).",
)
@click.option(
    "--cells-dir",
    "cells_dir",
    default=None,
    help="Cells directory for id references and baselines (default: lab/cells).",
)
def cmd(
    cell_ref: str,
    checkouts: tuple[str, ...],
    prs: tuple[str, ...],
    force: bool,
    fixture_path: str | None,
    cells_dir: str | None,
) -> None:
    """Run experiment cell CELL over the offline replay driver."""
    raise SystemExit(
        run(
            cell_ref,
            checkouts=checkouts,
            prs=prs,
            force=force,
            fixture_path=fixture_path,
            cells_dir=cells_dir,
        )
    )
