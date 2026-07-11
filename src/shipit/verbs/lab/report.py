"""``shipit lab report`` — render one Cell's convergence curve from banked records.

The Review Lab's cell read-side (ADR-0049, RVW03-WS07): loads the cell + its
baseline cell (fair-pair enforced, same as ``lab run``), the Ground-truth
fixture, and the local review-round record stores of every repo the cell pins,
then prints the deterministic convergence-curve report
(:mod:`shipit.review.curve`): cumulative major-or-worse recall, false
positives / adjudicated precision, token cost (latency-only when no record
carries a count), and latency per sweep point — the treatment and its baseline
side by side, read at equal budget. Zero tokens, zero network, free to re-run
(the ADR-0048 property, one level up).
"""

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
    check_fair_pair,
    instructions_variant_text,
    load_cell,
    resolve_cell_path,
)
from ...review.curve import convergence_curve, render_curve_report
from ...review.groundtruth import DEFAULT_FIXTURE_PATH, load_fixture
from ...review.instructions import load_instructions
from ...review.labrun import resolve_pins, safe_instructions_path
from .._errors import cli_errors


def _variant_hash(cell: Cell) -> str:
    """The content hash of ``cell``'s variant text — the BASE instructions,
    folded with a fan-out cell's resolved dimension set + per-dimension
    overrides (:func:`~shipit.review.cell.instructions_variant_text`, #713) —
    the variant half of the run key, computed EXACTLY as the runner does
    (:mod:`shipit.review.labrun`) so the report selects the same records the
    runs banked. The path is symlink-checked identically to the runner, and a
    missing/unreadable instructions file is a loud :class:`CellError`, never a
    silently-empty curve.
    """
    try:
        base_text = load_instructions(safe_instructions_path(cell.instructions_path))
    except OSError as exc:
        raise CellError(
            f"cell {cell.id!r}: cannot read instructions "
            f"{cell.instructions_path!r}: {exc}"
        ) from exc
    return variant_of(instructions_variant_text(cell, base_text)).content_hash


def _pin_records(cell: Cell, fixture, base_dir: Path | None) -> list[dict[str, Any]]:
    """Every banked round record of every repo the cell's pins name — the same
    origin-keyed stores the replay driver appended to (a missing store is
    simply zero records: a pin nobody ran yet renders as missing points)."""
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
    """Load + fair-pair check → pool banked records → print the curve. Exit code.

    The baseline cell loads from the same cells directory and renders beside
    the treatment (a control cell renders alone — it IS the baseline);
    :func:`resolve_pins` re-validates the fixture version pin, so a report
    against a drifted fixture refuses instead of printing incomparable
    numbers. ``base_dir`` overrides the store family root (tests).
    """
    out = out or sys.stdout
    cells_root = Path(cells_dir) if cells_dir is not None else DEFAULT_CELLS_DIR
    cell = load_cell(resolve_cell_path(cell_ref, cells_root))
    fixture = load_fixture(
        Path(fixture_path) if fixture_path is not None else DEFAULT_FIXTURE_PATH
    )
    baseline_curve = None
    if not cell.is_control:
        baseline_path = cells_root / f"{cell.baseline}.toml"
        if not baseline_path.is_file():
            raise CellError(
                f"cell {cell.id!r} names baseline {cell.baseline!r} but "
                f"{baseline_path} does not exist — commit the control cell of "
                "the pair first"
            )
        baseline = load_cell(baseline_path)
        check_fair_pair(cell, baseline, fixture)
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


@click.command(name="report")
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
    """Render CELL's convergence curve from banked review-round records.

    CELL is a cell id under lab/cells/ or a path to a cell file. Prints
    cumulative major-or-worse recall, false positives / precision, token cost,
    and latency per sweep point — with the baseline cell's curve beside it for
    the equal-budget comparison. Deterministic and token-free: scoring banked
    records is free to re-run forever (ADR-0048/0049).
    """
    raise SystemExit(run(cell_ref, fixture_path=fixture_path, cells_dir=cells_dir))
