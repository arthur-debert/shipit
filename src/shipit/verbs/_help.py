"""Helpers for long-form human help surfaces.

Click's ``--help`` remains the terse syntax/options map. This module serves
standalone UTF-8 text bundled beside command modules for the longer,
task-oriented help pages exposed as git-style ``help`` subcommands.
"""

from __future__ import annotations

from importlib import resources

import click


def load_help_text(package: str, resource: str) -> str:
    """Return one package-relative help text file as UTF-8 text."""
    try:
        return resources.files(package).joinpath(resource).read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, ModuleNotFoundError) as exc:
        raise click.ClickException(
            f"bundled help resource {package}:{resource} is unavailable; "
            "reinstall shipit or file a bug"
        ) from exc


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


def enable_leaf_help(
    command: click.Command, *, package: str, resource: str
) -> click.Command:
    """Teach one leaf command to reserve leading ``help`` for long-form help."""
    if isinstance(command, click.Group):
        raise TypeError("enable_leaf_help only accepts leaf commands")
    if not isinstance(command, HelpableCommand):
        command.__class__ = HelpableCommand
    command.help_package = package  # type: ignore[attr-defined]
    command.help_resource = resource  # type: ignore[attr-defined]
    return command


def register_long_help(
    root: click.Group, specs: dict[tuple[str, ...], tuple[str, str]]
) -> None:
    """Wire long-form help resources onto public commands named by path.

    ``()`` names the root group. Hidden commands are intentionally refused so
    internal entrypoints cannot accidentally join the human help surface.
    """
    for path, (package, resource) in specs.items():
        command = _resolve_command(root, path)
        if getattr(command, "hidden", False):
            dotted = " ".join(path) or root.name or "root"
            raise RuntimeError(
                f"cannot register human help for hidden command {dotted}"
            )
        if isinstance(command, click.Group):
            register_help_command(command, package=package, resource=resource)
        else:
            enable_leaf_help(command, package=package, resource=resource)


def _resolve_command(root: click.Group, path: tuple[str, ...]) -> click.Command:
    command: click.Command = root
    for part in path:
        if not isinstance(command, click.Group):
            joined = " ".join(path)
            raise RuntimeError(f"long help path crosses leaf command: {joined}")
        try:
            command = command.commands[part]
        except KeyError as exc:
            joined = " ".join(path)
            raise RuntimeError(f"long help path does not exist: {joined}") from exc
    return command


class HelpableCommand(click.Command):
    """A leaf command that reserves leading ``help`` for long-form help.

    ``shipit lab run CELL`` is intentionally still a leaf command, not a group.
    This shim intercepts a first positional ``help`` before Click treats it as
    CELL, while leaving every other CELL value untouched.
    """

    def __init__(self, *args, help_package: str, help_resource: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.help_package = help_package
        self.help_resource = help_resource

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if (
            not ctx.resilient_parsing
            and self._first_positional_arg(ctx, args) == "help"
        ):
            click.echo(load_help_text(self.help_package, self.help_resource), nl=False)
            ctx.exit()
        return super().parse_args(ctx, args)

    def _first_positional_arg(self, ctx: click.Context, args: list[str]) -> str | None:
        parser = self.make_parser(ctx)
        try:
            opts, leftover, _ = parser.parse_args(args=list(args))
        except click.ClickException:
            return None
        for param in self.get_params(ctx):
            if isinstance(param, click.Argument):
                value = opts.get(param.name)
                if isinstance(value, (tuple, list)):
                    first = value[0] if value else None
                    return first if isinstance(first, str) else None
                return value if isinstance(value, str) else None
        if leftover and leftover[0] == "help":
            return "help"
        return None
