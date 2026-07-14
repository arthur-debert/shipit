"""Role resolver — read the acting agent's **role** off the hook payload.

`resolve_role(hook_input) -> Role` encapsulates the one load-bearing rule
(ADR-0011 / ADR-0012): the Claude Code `PreToolUse` payload carries `agent_type`
**iff** the caller is a subagent, so an empty/absent `agent_type` is normally
the top-level human-facing session — the `coordinator`, the role the guard
governs. A named subagent resolves to its own role. Hosts without a native agent
flag (Codex) can pass a launch-context role in from the hook boundary; absent
native and launch-context role still means coordinator.

`Role` is a CLOSED registry (mirrors `prstate`'s reviewer/toolchain registries):
`coordinator`, `implementer`, `shepherd`, `explorer`, `reviewer`. Per-consumer
custom roles are out of scope (the registry ships fixed). Pure: a function of
the payload plus an optional boundary-supplied role name, no I/O.

This resolver is the deliberately LENIENT hook boundary: an unknown non-empty
native subagent identity stays an unknown worker (never the coordinator),
because the hook must govern whatever identity the host hands it. The STRICT
public/programmatic boundary — where an unknown role or an unsupported
role/launch pairing fails loud before any provisioning — is the Role Profile
registry (:mod:`shipit.harness.roleprofile`, RPE01-WS01); the hook's leniency
never makes an unknown identity spawnable there.
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


def resolve_role(
    hook_input: Mapping[str, Any], *, fallback_role: str | None = None
) -> Role:
    """Resolve the acting role from a parsed `PreToolUse` payload.

    The empty/absent-`agent_type`⇒`coordinator` rule is the load-bearing one:
    the top-level session has no `agent_type`, so it is governed unless a host
    without native agent flags passed a spawned role at the boundary. A named
    subagent whose `agent_type` matches a registry role resolves to that role.

    An *unrecognized* non-empty `agent_type` is still a subagent (NOT the
    coordinator — the only property the WS01 edit guard turns on), so it resolves
    to a generic worker role (`implementer`) rather than falling through to
    `coordinator`; the mismatch is logged. The fallback role follows the same
    normalization, but is consulted only when `agent_type` is absent.
    """
    agent_type = str(hook_input.get("agent_type") or "").strip().lower()
    if not agent_type:
        fallback = str(fallback_role or "").strip().lower()
        if not fallback:
            return Role.COORDINATOR
        return _resolve_role_name(fallback, source="fallback role")
    return _resolve_role_name(agent_type, source="agent_type")


def _resolve_role_name(name: str, *, source: str) -> Role:
    """Normalize a non-empty role name to the closed registry."""
    for role in Role:
        if role.value == name:
            return role
    logger.debug(
        "unrecognized %s %r — treating as a non-coordinator worker",
        source,
        name,
    )
    return Role.IMPLEMENTER
