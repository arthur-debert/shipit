"""`shipit repo new` — ADR-0030 glue + renderer over the creation domain.

The verb is a thin click parser and renderer over the deep repository-creation
module (:mod:`shipit.repocreate`, ``docs/spec/repo-new.md`` §Design Decisions):
it collects the repeated ``--stack`` selections, the project ``name``, and the
optional ``parent`` (defaulting to the current directory), calls
:func:`shipit.repocreate.create_repo`, and renders the typed
:class:`~shipit.repocreate.CreationResult`. It coordinates no validation,
staging, install, verification, or commit itself — those live in the module.

``repo`` is a new top-level command group and ``new`` its creation verb
(ADR-0056). Creation failures reach one uniform ``error: …`` + exit 1 through the
shared :func:`~._errors.cli_errors` shell (nothing here re-implements that
mapping): a domain-level refusal — a bad request, a conflicting profile
contribution, a failed staged Check — raises
:class:`~shipit.repocreate.CreationError`, while an underlying Git failure (e.g.
a commit that cannot sign) propagates as an :class:`~shipit.execrun.ExecError`;
the shell's known-error set carries both.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..repocreate import CreationResult, create_repo
from ._errors import cli_errors


@click.group(name="repo")
def repo() -> None:
    """Repository-level operations.

    ``repo new`` creates a new local shipit-managed Repo with a complete,
    verified development baseline.
    """


@repo.command(name="new")
@click.option(
    "--stack",
    "stacks",
    multiple=True,
    metavar="STACK",
    help="Toolchain to scaffold (repeatable). v1 supports: rust.",
)
@click.argument("name")
@click.argument("parent", required=False, type=click.Path(path_type=Path))
def new_cmd(stacks: tuple[str, ...], name: str, parent: Path | None) -> None:
    """Create a new local Repo NAME under PARENT (default: current directory).

    The destination is always ``PARENT/NAME``; it must be absent or an empty
    directory. Creates a complete, verified, initially-committed Repo — the
    project scaffold, shipit's managed baseline, a resolved pixi lockfile, and
    passing lint/test/build Checks — publishing it only once every step
    succeeds.
    """
    raise SystemExit(run_new(stacks=tuple(stacks), name=name, parent=parent))


@cli_errors
def run_new(*, stacks: tuple[str, ...], name: str, parent: Path | None) -> int:
    """Create → render. Returns an exit code (refusals map to 1 via the shell)."""
    result = create_repo(name, parent or Path.cwd(), stacks)
    click.echo(format_result(result))
    return 0


def format_result(result: CreationResult) -> str:
    """The pure text renderer over the typed result.

    Reports the published destination and the root commit (the WS01 acceptance
    line: "reports the destination and initial commit").
    """
    return (
        f"repo new: created {result.destination} "
        f"(stacks: {', '.join(result.stacks)})\n"
        f"  initial commit {result.initial_commit[:12]} on main"
    )
