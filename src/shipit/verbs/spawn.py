"""``shipit spawn`` — the click glue for shipit-owned subagent spawning.

See docs/adr/0017-shipit-owned-subagent-spawning.md.
"""

from __future__ import annotations

import json

import click

from ..harness import prompts
from ..harness.role import Role
from ..spawn import subagent
from ..spawn.subagent import SUPPORTED_BACKENDS
from ._errors import cli_errors
from ._params import shape_options
from ._render import emit


@click.group(
    name="spawn",
    help=(
        "Spawn backend-agent Runs shipit owns end to end.\n\n"
        "`subagent` creates a write Tree and launches a headless claude child "
        "rooted in it (ADR-0019), so the Run's work happens in the Tree, never the "
        "parent checkout. `brief` prints a role's brief template — the "
        "task-specific slots the coordinator fills before spawning (RVW02). "
        "`--help` is the map."
    ),
)
def spawn() -> None:
    """Root of the ``spawn`` subcommand group; verbs are attached below."""


@spawn.command(name="subagent")
@click.option(
    "--repo",
    required=True,
    help=(
        "Target repo (e.g. shipit). The skeleton spawns from the ambient checkout "
        "and uses this to guard against running in the wrong one; multi-repo "
        "selection is a later WS."
    ),
)
@shape_options
@click.option(
    "--role",
    required=True,
    help=(
        "The Run's role, validated against the fixed Role Profile registry "
        "(RPE01-WS01) before any Tree work: an unknown role, or one whose profile "
        "does not support a detached launch (coordinator, explorer), is refused "
        "loudly. The accepted role rides `claude --agent <role>` (ADR-0019 §2) "
        "so the guard allows the Run's own edits; it needs a committed "
        ".claude/agents/<role>.md def in the Tree. `reviewer` gets a shared "
        "READ-ONLY Tree and posts through the review service (ADR-0018), while "
        "`shepherd` attaches to an existing writable PR head via --pr."
    ),
)
@click.option(
    "--pr",
    "pr",
    type=int,
    default=None,
    help=(
        "Shepherd shape: existing pull request number to attach to. Valid only "
        "with --role shepherd; the branch/base are resolved from the PR instead "
        "of cutting a new issue or work-stream branch."
    ),
)
@click.option(
    "--backend",
    type=click.Choice(SUPPORTED_BACKENDS),
    default="claude",
    show_default=True,
    help=(
        "The agent backend to launch (derived from the adapter registry). `claude` "
        "(ADR-0019) and `antigravity` — the `agy` CLI, write Runs (ADR-0020) — are "
        "wired; `codex` lands alongside."
    ),
)
def subagent_cmd(
    repo: str,
    epic: str | None,
    ws: int | None,
    issue: int | None,
    role: str,
    pr: int | None,
    session: str,
    backend: str,
) -> None:
    """Create a write Tree and launch a backend-agent Run that reports via a draft PR."""
    raise SystemExit(
        run(
            repo=repo,
            epic=epic,
            ws=ws,
            issue=issue,
            role=role,
            pr=pr,
            session=session,
            backend=backend,
        )
    )


@spawn.command(name="brief")
@click.argument(
    "role",
    type=click.Choice([role.value for role in prompts.BRIEF_ROLES]),
)
def brief_cmd(role: str) -> None:
    """Print ROLE's brief template — the task-specific half the coordinator fills."""
    click.echo(prompts.load_brief_template(Role(role)))


def format_spawned(result: subagent.SpawnResult) -> str:
    """The byte-stable renderer for the agent-parsed ``SPAWNED`` block."""
    return "SPAWNED\n" + json.dumps(result.to_dict(), indent=2)


@cli_errors
def run(
    *,
    repo: str,
    role: str,
    epic: str | None = None,
    ws: int | None = None,
    issue: int | None = None,
    pr: int | None = None,
    session: str = "work",
    backend: str = "claude",
    bounds: subagent.Boundaries | None = None,
) -> int:
    """Run the spawn pipeline and render SPAWNED; returns an exit code."""
    spec = subagent.SubagentSpec(
        repo=repo,
        role=role,
        epic=epic,
        ws=ws,
        issue=issue,
        pr=pr,
        session=session,
        backend=backend,
    )
    result = subagent.spawn_subagent(spec, bounds)
    emit(result, format_spawned)
    return 0
