"""The ``shipit eval`` command group — the run-evaluation surface (PRD har02).

A NESTED click group (mirrors ``verbs/pr/`` and ``verbs/hook/``): ``shipit eval
<verb>`` reads the local JSONL eval store the harness terminal-hooks write. This
package is the extension point — each verb is its own module exposing a ``cmd``
click command (or a ``group``), registered below by an append-only line.

WS04 landed the first verb, ``report`` (the aggregator). RVW03-WS06 adds the
Review Lab pair (ADR-0048): ``score`` — the deterministic Ground-truth scorer,
reading the review-rounds store against the in-repo fixture — and ``bank``,
the Adjudication write-path that grows that fixture. The hook side that
*writes* the eval store lives under :mod:`shipit.verbs.hook` and
:mod:`shipit.harness.eval`; ``bank`` is the one verb here that writes anything,
and what it writes is the committed fixture file, never a store.
"""

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


# --- verb registration ---------------------------------------------------------
# Verbs import in one ruff-sorted block (I001 canonically merges same-package
# imports, so a literal one-line-per-verb block is not stable); each new verb
# still appends exactly one `add_command` line below — that list is append-only.
from . import bank, report, score  # noqa: E402  (HAR02-WS04; RVW03-WS06)

eval_group.add_command(report.cmd)
eval_group.add_command(score.cmd)
eval_group.add_command(bank.group)
