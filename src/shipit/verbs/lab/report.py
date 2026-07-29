"""``shipit lab report`` — render one Cell's convergence curve from banked records."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TextIO

import click

from ...harness.eval import store
from ...harness.eval.variant import variant_of
from ...identity import repo_from_slug
from ...review.cell import (
    DEFAULT_CELLS_DIR,
    Cell,
    CellError,
    instructions_variant_text,
    load_baseline_lineage,
    load_cell,
    resolve_cell_path,
)
from ...review.curve import convergence_curve, render_curve_report
from ...review.groundtruth import DEFAULT_FIXTURE_PATH, load_fixture
from ...review.instructions import load_instructions
from ...review.labrun import resolve_pins, safe_instructions_path
from .._errors import cli_errors
from .._help import HelpableCommand


def _variant_hash(cell: Cell) -> str:
    """The content hash of ``cell``'s variant text, computed exactly as the runner does; a missing instructions file raises CellError."""
    try:
        base_text = load_instructions(safe_instructions_path(cell.instructions_path))
    except OSError as exc:
        raise CellError(
            f"cell {cell.id!r}: cannot read instructions "
            f"{cell.instructions_path!r}: {exc}"
        ) from exc
    return variant_of(instructions_variant_text(cell, base_text)).content_hash


def _pin_records(cell: Cell, fixture, base_dir: Path | None) -> list[dict[str, Any]]:
    """Every banked round record of every repo the cell's pins name; a missing store is zero records."""
    pins = resolve_pins(cell, fixture)
    records: list[dict[str, Any]] = []
    for slug in sorted({pin.repo.lower() for pin in pins}):
        records.extend(
            store.read_records(
                repo_from_slug(slug), base_dir, kind=store.REVIEW_ROUNDS_KIND
            )
        )
    return records


@cli_errors
def run(
    cell_ref: str,
    *,
    fixture_path: str | None = None,
    cells_dir: str | None = None,
    base_dir: Path | None = None,
    out: TextIO | None = None,
) -> int:
    """Load + lineage check → pool banked records → print the curve; returns an exit code."""
    out = out or sys.stdout
    cells_root = Path(cells_dir) if cells_dir is not None else DEFAULT_CELLS_DIR
    cell = load_cell(resolve_cell_path(cell_ref, cells_root))
    fixture = load_fixture(
        Path(fixture_path) if fixture_path is not None else DEFAULT_FIXTURE_PATH
    )
    lineage = load_baseline_lineage(cell, fixture, cells_root)
    baseline_curve = None
    if not cell.is_control:
        baseline = lineage[1]
        baseline_curve = convergence_curve(
            baseline,
            fixture,
            _pin_records(baseline, fixture, base_dir),
            variant_hash=_variant_hash(baseline),
        )
    curve = convergence_curve(
        cell,
        fixture,
        _pin_records(cell, fixture, base_dir),
        variant_hash=_variant_hash(cell),
    )
    print(render_curve_report(curve, baseline_curve), file=out, end="")
    return 0


@click.command(
    name="report",
    cls=HelpableCommand,
    help_package=__package__,
    help_resource="lab_report_help.txt",
)
@click.argument("cell_ref", metavar="CELL")
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
def cmd(cell_ref: str, fixture_path: str | None, cells_dir: str | None) -> None:
    """Render CELL's convergence curve from banked review-round records."""
    raise SystemExit(run(cell_ref, fixture_path=fixture_path, cells_dir=cells_dir))
