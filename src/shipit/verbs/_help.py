"""Helpers for long-form human help surfaces.

Click's ``--help`` remains the terse syntax/options map. This module serves
standalone UTF-8 text bundled beside command modules for the longer,
task-oriented help pages exposed as git-style ``help`` subcommands. The first
implemented slice is ``shipit lab help`` plus the lab leaf commands.
"""

from __future__ import annotations

from importlib import resources

import click


def load_help_text(package: str, resource: str) -> str:
    """Return one package-relative help text file as UTF-8 text."""
    return resources.files(package).joinpath(resource).read_text(encoding="utf-8")


def help_command(name: str = "help", *, package: str, resource: str) -> click.Command:
    """Build a Click command that prints a bundled long-form help page."""

    @click.command(name=name)
    def cmd() -> None:
        """Print the long-form help guide."""
        click.echo(load_help_text(package, resource), nl=False)

    return cmd


def register_help_command(group: click.Group, *, package: str, resource: str) -> None:
    """Attach a ``help`` subcommand to ``group``."""
    group.add_command(help_command(package=package, resource=resource))


class HelpableCommand(click.Command):
    """A leaf command that reserves leading ``help`` for long-form help.

    ``shipit lab run CELL`` is intentionally still a leaf command, not a group.
    This shim intercepts leading ``help`` before Click treats it as CELL, while
    leaving every other CELL value untouched.
    """

    def __init__(self, *args, help_package: str, help_resource: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.help_package = help_package
        self.help_resource = help_resource

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not ctx.resilient_parsing and args and args[0] == "help":
            click.echo(load_help_text(self.help_package, self.help_resource), nl=False)
            ctx.exit()
        return super().parse_args(ctx, args)
