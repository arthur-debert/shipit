"""``shipit lab run`` — execute one experiment Cell over the replay driver.

The thin CLI around :func:`shipit.review.labrun.run_cell` (ADR-0049,
RVW03-WS07): resolve the cell reference to its committed file, load + validate
it (:mod:`shipit.review.cell` — the mandatory baseline/axis fairness
declaration fails HERE, before any token burns), load the Ground-truth
fixture, enforce the fair-pair check against the named baseline cell, and run
every (pin × replicate × sweep) point foreground on the subscription-billed
CLI backends. Idempotent by the full key: banked points are reused, never
re-paid — ``--force`` is the one explicit re-execute path.

Errors route through the one :func:`~.._errors.cli_errors` shell: an
untrustworthy cell or fixture file, an unfair pair, a missing checkout or
un-fetched pinned commit, and a backend failure all surface as one uniform
``error: …`` stderr line + exit 1.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...review.cell import (
    DEFAULT_CELLS_DIR,
    CellError,
    check_fair_pair,
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
    """Load + validate → fair-pair check → execute the sweep plan. Exit code.

    ``cell_ref`` is a path to a cell file, or a cell id under ``cells_dir``
    (default ``lab/cells/``). A treatment cell's BASELINE file must load from
    the same cells directory and pass :func:`check_fair_pair` — an unfair
    comparison refuses to run, exactly as it should fail at PR review of the
    cell file. ``base_dir``/``launcher`` are the store/launch injection seams
    (tests), as on the replay driver.
    """
    cells_root = Path(cells_dir) if cells_dir is not None else DEFAULT_CELLS_DIR
    cell = load_cell(resolve_cell_path(cell_ref, cells_root))
    fixture = load_fixture(
        Path(fixture_path) if fixture_path is not None else DEFAULT_FIXTURE_PATH
    )
    if not cell.is_control:
        baseline_path = cells_root / f"{cell.baseline}.toml"
        if not baseline_path.is_file():
            raise CellError(
                f"cell {cell.id!r} names baseline {cell.baseline!r} but "
                f"{baseline_path} does not exist — the baseline cell is part of "
                "the reviewed pair; commit it first"
            )
        check_fair_pair(cell, load_cell(baseline_path), fixture)
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
