"""Role resolver — read the acting agent's **role** off the hook payload.

`resolve_role(hook_input) -> Role` encapsulates the one load-bearing rule
(ADR-0011 / ADR-0012): the Claude Code `PreToolUse` payload carries `agent_type`
**iff** the caller is a subagent, so an empty/absent `agent_type` is the
top-level human-facing session — the `coordinator`, the role the guard governs.
A named subagent resolves to its own role.

`Role` is a CLOSED registry (mirrors `prstate`'s reviewer/toolchain registries):
`coordinator`, `implementer`, `shepherd`, `explorer`, `reviewer`. Per-consumer
custom roles are out of scope (the registry ships fixed). Pure: a function of the
payload only, no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

logger = logging.getLogger("shipit.hook")


class Role(StrEnum):
    """The closed set of agent roles the harness governs (ADR-0011)."""

    COORDINATOR = "coordinator"
    IMPLEMENTER = "implementer"
    SHEPHERD = "shepherd"
    EXPLORER = "explorer"
    REVIEWER = "reviewer"


def resolve_role(hook_input: Mapping[str, Any]) -> Role:
    """Resolve the acting role from a parsed `PreToolUse` payload.

    The empty/absent-`agent_type`⇒`coordinator` rule is the load-bearing one:
    the top-level session has no `agent_type`, so it is always governed. A named
    subagent whose `agent_type` matches a registry role resolves to that role.

    An *unrecognized* non-empty `agent_type` is still a subagent (NOT the
    coordinator — the only property the WS01 edit guard turns on), so it resolves
    to a generic worker role (`implementer`) rather than falling through to
    `coordinator`; the mismatch is logged. WS03 tightens this once the agent-defs
    are generated with the registry's exact names.
    """
    agent_type = str(hook_input.get("agent_type") or "").strip().lower()
    if not agent_type:
        return Role.COORDINATOR
    for role in Role:
        if role.value == agent_type:
            return role
    logger.debug(
        "unrecognized agent_type %r — treating as a non-coordinator worker",
        agent_type,
    )
    return Role.IMPLEMENTER
