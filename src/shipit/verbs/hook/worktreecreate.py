"""`shipit hook worktreecreate` — the demoted WorktreeCreate adapter (ADR-0017).

Claude Code fires the `WorktreeCreate` hook when an in-session
`Agent(isolation:"worktree")` spawn needs an isolated checkout. Left to itself the
harness mints a native `.claude/worktrees/agent-<hash>` worktree — exactly the
thing ADR-0014 forbids and the #139 enforcement gap. This boundary intercepts that
spawn and instead provisions a **Tree** (a dissociated clone) via `shipit tree
create`, printing the Tree's path so Claude Code adopts it as the subagent's cwd.
So even the throwaway in-CC path lands in a real Tree, closing #139 *by
construction* (the supported route can no longer reach a native worktree).

THIN by design (mirrors `hook pretooluse`): read the `WorktreeCreate` payload on
stdin → resolve the holding branch (`harness.worktree_adapter`) from the
session-stable epic marker + the spawn's id → create the Tree
(`tree.create`) → write its absolute path to stdout. The branch is **deferred**
(`<epic>/agent-<id>`, base `origin/main`): a coarse holding branch the spawned
agent self-branches off, because the hook cannot know the per-spawn work
stream/role (ADR-0017).

**Fail-CLOSED — the OPPOSITE of `hook pretooluse`.** Claude Code adopts the path a
zero-exit hook prints and aborts the spawn on a non-zero exit. A silent fallback
to a native worktree would re-open #139, so ANY failure here (bad payload, not in a
checkout, a git/gh/provision error) prints a diagnostic to **stderr**, writes
NOTHING to stdout, and exits non-zero — the spawn fails loud rather than escaping
to a native worktree.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from typing import TextIO

import click

from ... import gh
from ...harness import worktree_adapter
from ...tree.create import create_from_source, new_agent_hash
from ...tree.layout import TreeSpec

logger = logging.getLogger("shipit.hook")

#: Bytes of randomness behind a synthesized agent id when the payload carries none
#: → 8 hex chars, enough to keep concurrent marker-less spawns from colliding on a
#: holding branch.
_ID_BYTES = 4


@click.command(name="worktreecreate")
def cmd() -> None:
    """Provision a Tree for an `Agent(isolation:"worktree")` spawn; print its path.

    Reads the `WorktreeCreate` payload as JSON on stdin and writes the new Tree's
    absolute path to stdout (which Claude Code adopts as the subagent cwd). Exits
    non-zero on any failure — fail-CLOSED, so a failed spawn never falls back to a
    native worktree.
    """
    raise SystemExit(run())


def run(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Parse stdin → resolve branch → create Tree → print path. Fail-CLOSED.

    Returns 0 after printing the Tree path on success; returns 1 (printing a
    diagnostic to stderr and NOTHING to stdout) on any error, so Claude Code aborts
    the spawn rather than minting a native worktree.
    """
    out = stdout if stdout is not None else sys.stdout
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"WorktreeCreate payload is not an object: {payload!r}")
        branch = _resolve_branch(payload)
        path = _create_tree(branch)
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED: any error aborts the spawn.
        logger.debug("worktreecreate hook failed (aborting spawn)", exc_info=True)
        print(f"shipit hook worktreecreate: {exc}", file=sys.stderr)
        return 1
    out.write(path + "\n")
    return 0


def _resolve_branch(payload: dict[str, object]) -> str:
    """The holding branch for this spawn: `<epic>/agent-<id>` (epic from the marker).

    The id comes from the payload's `worktree_name` (Claude Code's own throwaway
    name), normalized to a safe ref component; if it normalizes to nothing a random
    id is synthesized so the spawn is never blocked on a missing name. The epic
    comes from the session-stable marker env var, falling back safely to an
    epic-less branch when unset or malformed.
    """
    raw_id = str(payload.get("worktree_name") or "")
    agent_id = worktree_adapter.normalize_agent_id(raw_id) or secrets.token_hex(
        _ID_BYTES
    )
    epic = os.environ.get(worktree_adapter.EPIC_MARKER_ENV)
    return worktree_adapter.resolve_branch(epic, agent_id)


def _create_tree(branch: str) -> str:
    """Provision the Tree on `branch` from the ambient checkout; return its path.

    Resolves repo identity (local root + `org/repo`) at the gh/git boundary,
    validates the slug is a well-formed `org/repo` before trusting it, hands a
    freeform-`branch` :class:`TreeSpec` to the orchestrator, and returns the
    dissociated clone's path. Raises on any failure — a missing checkout OR a
    malformed slug — so :func:`run` fails closed; there is no native-worktree
    fallback.
    """
    root = gh.repo_root()
    if not root:
        raise RuntimeError("not inside a git checkout — cannot provision a Tree")
    org_repo = gh.current_repo()
    org, sep, repo = org_repo.partition("/")
    if not (org and sep and repo):
        raise RuntimeError(
            f"malformed repo slug {org_repo!r} (expected 'org/repo') — "
            "cannot provision a Tree"
        )
    spec = TreeSpec(
        org=org,
        repo=repo,
        agent_hash=new_agent_hash(),
        branch=branch,
    )
    tree = create_from_source(spec, source_repo=root)
    return tree.path
