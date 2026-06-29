"""The ``shipit eval`` command group — the run-evaluation surface (PRD har02).

A NESTED click group (mirrors ``verbs/pr/`` and ``verbs/hook/``): ``shipit eval
<verb>`` reads the local JSONL eval store the harness terminal-hooks write. This
package is the extension point — each verb is its own module exposing a ``cmd``
click command, registered below by an append-only line.

WS04 lands the first verb, ``report`` (the aggregator). The hook side that
*writes* the store lives under :mod:`shipit.verbs.hook` and
:mod:`shipit.harness.eval`; this group only *reads* it.
"""

from __future__ import annotations

import click


@click.group(
    name="eval",
    help=(
        "Run evaluation — aggregate the local objective-eval store.\n\n"
        "`report` runs DuckDB/SQL over the JSONL records the harness writes at "
        "each run's terminal hook, summarising by role, by variant, and over "
        "time. `--help` is the map."
    ),
)
def eval_group() -> None:
    """Root of the ``eval`` subcommand group; verbs are attached below."""


# --- verb registration (append-only; one import + one add_command per verb) ---
from . import report  # noqa: E402  (HAR02-WS04)

eval_group.add_command(report.cmd)
