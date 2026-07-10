"""``shipit eval score`` — score banked review-round records against the fixture.

The Review Lab's read-side verb (ADR-0048, RVW03-WS06): loads the in-repo
Ground-truth fixture (:mod:`shipit.review.groundtruth`), reads the local
review-round record stores of every repo the fixture pins (the SAME
harness-owned JSONL family the replay/review paths append to,
:mod:`shipit.harness.eval.store`), and prints the deterministic score report —
recall / false positives / unadjudicated per Variant, underpowered tiers
marked, near-misses and unmatched emissions listed for Adjudication
(:mod:`shipit.review.scorer`). Zero tokens, zero network, CI-runnable: the
whole run is local file reads + pure matching.

Pure core / thin boundary: :func:`run` resolves paths and reads stores;
everything it feeds (:func:`shipit.review.scorer.score_records`) and prints is
pure. A missing store is simply zero records for that repo (a fixture pin
nobody replayed yet); a missing or invalid FIXTURE is a loud error — scoring
against half a fixture would print numbers with an unreproducible denominator.
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
    """Score the fixture's pinned ranges from the local round-record stores.

    ``fixture_path`` defaults to the in-repo location
    (:data:`~shipit.review.groundtruth.DEFAULT_FIXTURE_PATH` under the current
    directory — run from the repo root, or pass the path). ``base_dir``
    overrides the store FAMILY root (tests, mirroring the eval report verb).
    Returns 0 on a rendered report, 1 on a fixture error (loud, never a
    silently-empty score).
    """
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
        try:
            repo = repo_from_slug(slug)
        except ValueError:
            continue  # parse_fixture guarantees owner/name shape; belt-and-suspenders
        records.extend(store.read_records(repo, root, kind=store.REVIEW_ROUNDS_KIND))
    print(render_report(score_records(fixture, records)), file=out, end="")
    return 0


@click.command(name="score")
@click.argument("fixture_path", required=False)
def cmd(fixture_path: str | None) -> None:
    """Score banked review-round records against the Ground-truth fixture.

    FIXTURE_PATH defaults to lab/fixture.toml under the current directory.
    Reads the never-committed review-rounds stores for every repo the fixture
    pins; prints recall / false positives / unadjudicated per variant with an
    adjudication report of near-misses and unmatched emissions. Deterministic
    and token-free — safe to re-run forever (ADR-0048).
    """
    raise SystemExit(run(fixture_path))
