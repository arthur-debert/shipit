"""``harness/role`` — read the acting agent's role off the hook payload.

The LENIENT boundary; the strict one is :mod:`shipit.harness.roleprofile`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

logger = logging.getLogger("shipit.hook")


class Role(StrEnum):
    """The closed set of agent roles the harness governs."""

    COORDINATOR = "coordinator"
    IMPLEMENTER = "implementer"
    SHEPHERD = "shepherd"
    EXPLORER = "explorer"
    REVIEWER = "reviewer"


def resolve_role(
    hook_input: Mapping[str, Any], *, fallback_role: str | None = None
) -> Role:
    """A payload carries `agent_type` iff the caller is a subagent, so an absent
    one is the coordinator. `fallback_role` applies only when it is absent.
    """
    agent_type = str(hook_input.get("agent_type") or "").strip().lower()
    if not agent_type:
        fallback = str(fallback_role or "").strip().lower()
        if not fallback:
            return Role.COORDINATOR
        return _resolve_role_name(fallback, source="fallback role")
    return _resolve_role_name(agent_type, source="agent_type")


def _resolve_role_name(name: str, *, source: str) -> Role:
    for role in Role:
        if role.value == name:
            return role
    logger.debug(
        "unrecognized %s %r — treating as a non-coordinator worker",
        source,
        name,
    )
    return Role.IMPLEMENTER
