"""``shipit eval score`` — score banked review-round records against the Ground-truth fixture.

See docs/adr/0048-ground-truth-fixture-deterministic-scorer.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TextIO

import click

from ...harness.eval import store
from ...identity import repo_from_slug
from ...review.groundtruth import DEFAULT_FIXTURE_PATH, FixtureError, load_fixture
from ...review.scorer import render_report, score_records


def run(
    fixture_path: str | Path | None = None,
    *,
    base_dir: str | Path | None = None,
    out: TextIO | None = None,
) -> int:
    """Score the fixture's pinned ranges from the local round-record stores; returns 0 on a rendered report, 1 on a fixture error."""
    out = out or sys.stdout
    path = Path(fixture_path) if fixture_path is not None else DEFAULT_FIXTURE_PATH
    try:
        fixture = load_fixture(path)
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    root = base_dir if base_dir is None else Path(base_dir)
    records: list[dict[str, Any]] = []
    for slug in sorted({pin.repo for pin in fixture.prs}):
        repo = repo_from_slug(slug)
        records.extend(store.read_records(repo, root, kind=store.REVIEW_ROUNDS_KIND))
    print(render_report(score_records(fixture, records)), file=out, end="")
    return 0


@click.command(name="score")
@click.argument("fixture_path", required=False)
def cmd(fixture_path: str | None) -> None:
    """Score banked review-round records against the Ground-truth fixture."""
    raise SystemExit(run(fixture_path))
