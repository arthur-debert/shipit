"""The ``shipit lab`` command group — the Review Lab's experiment surface.

See docs/adr/0049-convergence-curve-objective-one-axis-cells.md.
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


from . import report, run  # noqa: E402

register_help_command(lab_group, package=__package__, resource="lab_help.txt")
lab_group.add_command(run.cmd)
lab_group.add_command(report.cmd)
