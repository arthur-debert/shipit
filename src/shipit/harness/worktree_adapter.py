"""Branch resolution for the *demoted* WorktreeCreate adapter (ADR-0017).

The in-CC ``Agent(isolation:"worktree")`` spawn fires Claude Code's
``WorktreeCreate`` hook. Instead of letting the harness mint a native
``.claude/worktrees`` worktree, the hook calls ``shipit tree create`` and returns
a **dissociated Tree clone** as the subagent's cwd — closing the #139 enforcement
gap *by construction* (the in-CC path can no longer reach a native worktree).

This module is the PURE half of that adapter: given the **session-stable epic
marker** and the spawn's agent id, it resolves the holding branch the throwaway
Tree is cut on — ``<epic>/agent-<id>``, or a safe epic-less ``agent-<id>`` when no
(or a malformed) marker is set. No I/O; the boundary
(:mod:`shipit.verbs.hook.worktreecreate`) reads the env + payload and runs the
create.

Why a *session-stable* marker and not a per-spawn one: the hook fires with no
per-spawn intent — it cannot know the work stream or role, only the epic — so the
branch it can build is deliberately coarse (``<epic>/agent-<id>``) and the Tree is
**branch-deferred** (the spawned agent self-branches to its real working branch).
Anything that needs a real branch-pinned Run, a non-Claude backend, or a
PR-reported result goes through ``shipit spawn subagent`` instead (ADR-0017
Considered options).
"""

from __future__ import annotations

import re

#: The session-stable epic marker — an env var the coordinator sets ONCE per
#: session so every in-CC ``Agent(isolation:"worktree")`` spawn lands its
#: throwaway Tree under the right epic namespace. Session-stable (not per-spawn)
#: because the hook fires with no per-spawn intent; the epic is the most it can
#: know (ADR-0017).
EPIC_MARKER_ENV = "SHIPIT_EPIC"

#: The branch stem each spawn's id hangs off: ``<epic>/agent-<id>`` (or bare
#: ``agent-<id>``). It mirrors Claude Code's own native ``agent-<hash>`` worktree
#: naming, so a Tree reads as the same kind of throwaway — just relocated into a
#: real dissociated clone.
_AGENT_STEM = "agent-"

#: Characters an agent id is normalized on before it becomes a branch component:
#: every run of a path/ref separator, dot, colon, or whitespace collapses to a
#: single ``-`` (mirrors ``tree.layout.sanitize_slug``) so the id is safe in a ref.
_ID_SEP = re.compile(r"[\s/.:]+")

#: A usable epic marker is a single alphanumeric token (naming.lex §3 ``THEME+NN``,
#: kept verbatim). Anything else — empty, whitespace, or carrying separators / ``..``
#: that would mangle the ref — is treated as *no marker* and falls back safely, so a
#: garbage marker can never produce a broken branch like ``/agent-x``.
_EPIC_TOKEN = re.compile(r"[A-Za-z0-9]+")


def normalize_agent_id(raw: str) -> str:
    """Normalize a spawn's raw agent id into a safe branch component.

    Strips a leading ``agent-`` (Claude Code's native worktree id already carries
    it, and the branch re-adds exactly one stem), lowercases, collapses every run
    of separator characters to a single ``-``, and trims stray ``-``. Returns
    ``""`` for an id that normalizes to nothing — the boundary substitutes a
    generated id in that case so a spawn is never blocked on a missing id.
    """
    cleaned = raw.strip()
    if cleaned.lower().startswith(_AGENT_STEM):
        cleaned = cleaned[len(_AGENT_STEM) :]
    return _ID_SEP.sub("-", cleaned.lower()).strip("-")


def resolve_branch(epic: str | None, agent_id: str) -> str:
    """Resolve the holding branch the throwaway Tree is cut on (pure).

    ``<epic>/agent-<id>`` when the session-stable epic marker is a usable
    alphanumeric token; a safe, epic-less ``agent-<id>`` when the marker is
    missing OR malformed — so such a spawn still lands in a real Tree (never a
    native worktree), just under a generic holding namespace rather than an
    epic's. ``agent_id`` must already be a non-empty, normalized ref component
    (see :func:`normalize_agent_id`); the caller guarantees that.
    """
    leaf = f"{_AGENT_STEM}{agent_id}"
    epic = (epic or "").strip()
    if epic and _EPIC_TOKEN.fullmatch(epic):
        return f"{epic}/{leaf}"
    return leaf
