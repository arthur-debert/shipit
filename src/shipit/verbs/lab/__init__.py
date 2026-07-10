"""The ``shipit lab`` command group — the Review Lab's experiment surface.

A NESTED click group (mirrors ``verbs/eval/`` and ``verbs/pr/``): ``shipit lab
<verb>`` drives the measured-review-experimentation layer of ADR-0049 /
``docs/spec/review-lab.md``. ``run`` resolves one declarative in-repo **Cell**
file onto the sanctioned offline replay driver — foreground, idempotent by
key, banked results reused, never re-paid; ``report`` renders the cell's
**convergence curve** (cumulative recall / precision / cost / latency per
sweep point, equal-budget against its baseline cell) from the banked records —
deterministic and token-free.

This package is the extension point — each verb is its own module exposing a
``cmd`` click command, registered below by an append-only line (the same
convention as ``verbs/eval/``).
"""

from __future__ import annotations

import click

from .._help import register_help_command


@click.group(
    name="lab",
    help=(
        "Review Lab — run and report declarative review experiments "
        "(ADR-0049).\n\n"
        "Use `run` to execute a cell and `report` to render its banked "
        "convergence curve. `shipit lab help` is the long-form map."
    ),
)
def lab_group() -> None:
    """Root of the ``lab`` subcommand group; verbs are attached below."""


# --- verb registration ---------------------------------------------------------
# Verbs import in one ruff-sorted block (the verbs/eval/ convention); each new
# verb appends exactly one `add_command` line below — that list is append-only.
from . import report, run  # noqa: E402  (RVW03-WS07)

register_help_command(lab_group, package=__package__, resource="lab_help.txt")
lab_group.add_command(run.cmd)
lab_group.add_command(report.cmd)
