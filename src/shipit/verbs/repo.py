"""`shipit repo new` — click glue and renderer over the repo-creation domain."""

from __future__ import annotations

from pathlib import Path

import click

from ..repocreate import CreationResult, create_repo
from ._errors import cli_errors


@click.group(name="repo")
def repo() -> None:
    """Repository-level operations."""


@repo.command(name="new")
@click.option(
    "--stack",
    "stacks",
    multiple=True,
    metavar="STACK",
    help="Toolchain to scaffold (repeatable). v1 supports: rust.",
)
@click.option(
    "--standout-wizard",
    is_flag=True,
    help=(
        "Scaffold the Rust application with the interactive `standout new-project` "
        "wizard instead of the minimal hello-world workspace. Requires --stack rust."
    ),
)
@click.argument("name")
@click.argument("parent", required=False, type=click.Path(path_type=Path))
def new_cmd(
    stacks: tuple[str, ...],
    standout_wizard: bool,
    name: str,
    parent: Path | None,
) -> None:
    """Create a new local Repo NAME under PARENT (default: current directory)."""
    raise SystemExit(
        run_new(
            stacks=tuple(stacks),
            name=name,
            parent=parent,
            standout_wizard=standout_wizard,
        )
    )


@cli_errors
def run_new(
    *,
    stacks: tuple[str, ...],
    name: str,
    parent: Path | None,
    standout_wizard: bool = False,
) -> int:
    """Create the repo and render the result; returns an exit code."""
    result = create_repo(
        name, parent or Path.cwd(), stacks, standout_wizard=standout_wizard
    )
    click.echo(format_result(result))
    return 0


def format_result(result: CreationResult) -> str:
    return (
        f"repo new: created {result.destination} "
        f"(stacks: {', '.join(result.stacks)})\n"
        f"  initial commit {result.initial_commit[:12]} on main"
    )
