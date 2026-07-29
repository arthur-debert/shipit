"""The ``shipit eval`` command group — the run-evaluation surface."""

from __future__ import annotations

import click


@click.group(
    name="eval",
    help=(
        "Run evaluation — aggregate the local objective-eval store.\n\n"
        "`report` runs DuckDB/SQL over the JSONL records the harness writes at "
        "each run's terminal hook, summarising by role, by variant, over "
        "time — and by review-round variant (the review axis). `score` scores "
        "banked review rounds against the in-repo ground-truth fixture "
        "(deterministic, token-free); `bank` records an adjudicated verdict "
        "into that fixture. `--help` is the map."
    ),
)
def eval_group() -> None:
    """Root of the ``eval`` subcommand group; verbs are attached below."""


from . import bank, report, score  # noqa: E402  (HAR02-WS04; RVW03-WS06)

eval_group.add_command(report.cmd)
eval_group.add_command(score.cmd)
eval_group.add_command(bank.group)
