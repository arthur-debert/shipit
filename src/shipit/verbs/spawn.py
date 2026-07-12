"""``shipit spawn`` — shipit-owned subagent spawning, as ADR-0030 click glue.

A NESTED click group mirroring ``shipit tree``: ``shipit spawn <verb>`` is the
surface for launching backend-agent **Runs** that shipit owns end to end
(ADR-0017 / ADR-0019). The whole pipeline — shape validation → identity →
umbrella check → Tree → launch → post-condition audit — lives in the domain
(:func:`shipit.spawn.subagent.spawn_subagent`, spec → typed result); this
module is the three ADR-0030 pieces and nothing else:

- **params** — the shared Tree-shape option stack (:func:`.._params.shape_options`)
  plus the verb's own ``--repo``/``--role``/``--backend``; click validates only
  the explicit primitives — WHICH shape the combination selects (and the
  role-dependent ``--issue`` requirement) is the pipeline's own shape stage, a
  runtime refusal, so a valid reviewer spawn (no ``--issue``) is never rejected
  at parse.
- **domain call** — one :class:`~shipit.spawn.subagent.SubagentSpec` handed to
  the pipeline, which returns the frozen
  :class:`~shipit.spawn.subagent.SpawnResult` or raises the
  :class:`~shipit.spawn.subagent.SpawnError` domain refusal.
- **render** — the pure :func:`format_spawned` (the byte-stable, agent-parsed
  ``SPAWNED`` block) through the shared :func:`~.._render.emit`; the exit code
  derives from the result, with every refusal mapped by the one
  :func:`~.._errors.cli_errors` shell (``error: …`` + exit 1) instead of the
  old per-verb print+log+rc helper.
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
        "does not support a detached launch (coordinator, shepherd, explorer), is "
        "refused loud. The accepted role rides `claude --agent <role>` (ADR-0019 "
        "§2) so the guard allows the Run's own edits; it needs a committed "
        ".claude/agents/<role>.md def in the Tree. `reviewer` gets a shared "
        "READ-ONLY Tree and posts a review through the PR (ADR-0018), not a "
        "write Tree."
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
    session: str,
    backend: str,
) -> None:
    """Create a write Tree and launch a backend-agent Run that reports via a draft PR.

    Resolve the ambient repo identity, create a write Tree by reusing
    ``shipit tree create``, then launch a headless backend child whose ``cwd`` IS
    that Tree (ADR-0019). Two write shapes, dispatched on whether ``--epic``/``--ws``
    are given:

    \b
    - **epic/work stream** (``--epic E --ws N``): the Tree is cut from the epic-grouped
      umbrella base (``origin/E/umbrella``) so the Run's draft PR targets the EPIC
      branch (``E/umbrella``), matching the coordinator-driven epic topology (#176).
    - **standalone issue** (``--issue N`` with NO ``--epic``/``--ws``): the Tree is cut
      from ``origin/main`` on branch ``issues/<id>/<session>`` (session default
      ``work``), so the Run's draft PR targets ``main``.

    Either way the Run implements ``--issue`` (REQUIRED for a write role — it rides
    the task prompt and the draft PR links it: ``closes #<issue>`` on the standalone
    shape so the merge auto-closes the issue, ``for #<issue>`` on the epic shape,
    non-closing because the umbrella PR closes the epic's issues (#649); a
    `reviewer` Run implements no issue) and opens a draft PR from the Tree's branch;
    ``spawn``
    resolves that PR back from the branch and reports the Run↔PR linkage so the
    coordinator drives it with ``shipit pr status``.

    Fail-closed: if the epic umbrella branch is absent on the remote, or a
    Tree-creation error occurs, the spawn exits 1 loudly — never a silent fallback
    to ``origin/main`` or to a native ``git worktree``. A child that exits nonzero,
    that exits 0 without having opened a PR on the Tree's branch, or that opened a PR
    which is not an OPEN, DRAFT PR targeting the intended base, is also a clean exit-1.
    """
    raise SystemExit(
        run(
            repo=repo,
            epic=epic,
            ws=ws,
            issue=issue,
            role=role,
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
    """Print ROLE's brief template — the task-specific half the coordinator fills.

    The bundled BRIEF TEMPLATE (RVW02) for a spawn/cold brief: the general half
    of an agent's prompt is its role prompt (the generated agent-def); this is
    the layer that varies per task. Expand it before every implementer spawn and
    every shepherd cold brief: replace EVERY ``{{slot}}`` — the issue ref, the
    exact verify commands, the epic's governing docs, the decision boundaries —
    and hand the expanded skeleton over as the brief. The slots are mandatory;
    the roles flag a missing slot rather than guess around it.
    """
    click.echo(prompts.load_brief_template(Role(role)))


def format_spawned(result: subagent.SpawnResult) -> str:
    """The pure renderer for the agent-parsed ``SPAWNED`` block — byte-stable.

    A ``SPAWNED`` sentinel line plus the Run's coordinates as indented JSON
    (the result's own ``to_dict()``, so the surface is exactly the typed
    result's declared field set). A WRITE Run's payload carries the Run↔PR
    linkage (``pr``/``pr_state``/``pr_is_draft``) the coordinator acts on; a
    reviewer Run reports through the existing PR and opens none, so its block
    renders WITHOUT the linkage keys (``to_dict`` drops them, absent-not-null).
    Frozen agent-facing output (the PRD's sentinel contract) — do not restyle.
    """
    return "SPAWNED\n" + json.dumps(result.to_dict(), indent=2)


@cli_errors
def run(
    *,
    repo: str,
    role: str,
    epic: str | None = None,
    ws: int | None = None,
    issue: int | None = None,
    session: str = "work",
    backend: str = "claude",
    bounds: subagent.Boundaries | None = None,
) -> int:
    """Build the spec → run the pipeline → render SPAWNED. Returns an exit code.

    ``bounds`` injects the pipeline's effectful edges
    (:class:`~shipit.spawn.subagent.Boundaries`) for direct (test) callers;
    ``None`` is production. Returns 0 on a completed spawn; every pipeline
    refusal (:class:`~shipit.spawn.subagent.SpawnError` — an unknown role or a
    role/launch pair the Role Profile registry refuses, bad shape, wrong
    checkout, failed Tree fail-closed, failed launch, failed handshake audit)
    propagates to the :func:`~shipit.verbs._errors.cli_errors` shell: one clean
    ``error: …`` stderr line + exit 1, never a traceback, never a SPAWNED block.
    """
    spec = subagent.SubagentSpec(
        repo=repo,
        role=role,
        epic=epic,
        ws=ws,
        issue=issue,
        session=session,
        backend=backend,
    )
    result = subagent.spawn_subagent(spec, bounds)
    emit(result, format_spawned)
    return 0
