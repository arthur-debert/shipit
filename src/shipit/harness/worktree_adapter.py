"""``harness/worktree_adapter`` — pure resolution for the WorktreeCreate hook.

Tells the coordinator launch from a helper spawn and resolves the helper's
``<epic>/agent-<id>`` holding branch. See docs/adr/0027-ephemeral-session-tree.md.
"""

from __future__ import annotations

import re

EPIC_MARKER_ENV = "SHIPIT_EPIC"

_AGENT_STEM = "agent-"

_ID_SEP = re.compile(r"[\s/.:]+")

#: A usable epic marker is one alphanumeric token; anything else falls back safely.
_EPIC_TOKEN = re.compile(r"[A-Za-z0-9]+")


def is_coordinator_launch(payload: dict[str, object]) -> bool:
    """A coordinator launch fires before any prompt, so an absent ``prompt_id`` is it."""
    return not payload.get("prompt_id")


def normalize_agent_id(raw: str) -> str:
    """Strips a leading ``agent-`` stem; ``""`` when the id normalizes to nothing."""
    cleaned = raw.strip()
    if cleaned.lower().startswith(_AGENT_STEM):
        cleaned = cleaned[len(_AGENT_STEM) :]
    return _ID_SEP.sub("-", cleaned.lower()).strip("-")


def resolve_epic(override: str | None, branch: str | None) -> str | None:
    """The epic CANDIDATE: ``override``, else ``branch``'s prefix before the first ``/``."""
    override = (override or "").strip()
    if override:
        return override
    prefix, sep, _ = (branch or "").strip().partition("/")
    if sep and prefix:
        return prefix
    return None


def resolve_branch(epic: str | None, agent_id: str) -> str:
    """``<epic>/agent-<id>``, bare when the epic is missing or malformed."""
    leaf = f"{_AGENT_STEM}{agent_id}"
    epic = (epic or "").strip()
    if epic and _EPIC_TOKEN.fullmatch(epic):
        return f"{epic}/{leaf}"
    return leaf
