"""``harness/eval/record`` — assemble one run's JSONL eval record, on OTel field names."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...agent import invocation as agent_invocation
from .variant import role_of_meta, role_of_name

#: Bump when the record's field set changes, so an aggregator can read mixed stores.
SCHEMA_VERSION = 4

#: The role recorded for a subagent run whose meta is absent or unreadable.
_UNKNOWN_SUBAGENT_ROLE = "unknown-subagent"


def build(
    *,
    metrics: Mapping[str, Any],
    meta: Mapping[str, Any] | None,
    variant: Any,
    commit: str | None,
    timestamp: str,
    is_coordinator: bool,
    spawned_role: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """The eval record for one run; ``is_coordinator`` is the locator's classification."""
    meta = meta or {}
    stuck = _mapping(metrics.get("stuck_loop"))
    tokens = _mapping(metrics.get("token_usage"))
    hygiene = _mapping(metrics.get("exit_hygiene"))
    return {
        "eval.schema_version": SCHEMA_VERSION,
        "eval.timestamp": timestamp,
        "eval.run_id": run_id,
        "gen_ai.agent.name": _role_name(meta, is_coordinator, spawned_role),
        "gen_ai.request.model": meta.get("model"),
        "eval.permission_mode": meta.get("spawnMode"),
        "eval.tool_call_count": metrics.get("tool_call_count") or 0,
        "eval.tool_call_vector": dict(_mapping(metrics.get("tool_call_vector"))),
        "eval.turn_count": metrics.get("turn_count") or 0,
        "eval.stuck_loop": bool(stuck.get("detected")),
        "eval.max_repeated_calls": stuck.get("max_repeated_calls") or 0,
        "eval.max_turn_iterations": stuck.get("max_turn_iterations") or 0,
        "eval.no_verify_count": metrics.get("no_verify_count") or 0,
        "eval.break_glass_count": metrics.get("break_glass_count") or 0,
        "eval.error_count": metrics.get("error_count") or 0,
        "eval.retry_count": metrics.get("retry_count") or 0,
        "gen_ai.usage.input_tokens": tokens.get("input_tokens"),
        "gen_ai.usage.output_tokens": tokens.get("output_tokens"),
        "eval.usage.cache_read_tokens": tokens.get("cache_read_tokens"),
        "eval.usage.cache_creation_tokens": tokens.get("cache_creation_tokens"),
        "eval.usage.total_tokens": tokens.get("total_tokens"),
        "eval.exit_hygiene.worktree_clean": hygiene.get("worktree_clean"),
        "eval.exit_hygiene.dirty_file_count": hygiene.get("dirty_file_count"),
        "eval.exit_hygiene.stray_pid_count": hygiene.get("stray_pid_count"),
        "eval.variant": variant,
        "eval.invocation": _invocation_record(meta),
        "git.commit": commit,
    }


def _invocation_record(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """The run's launch-config attribution: the observed config, and the intended one if stamped."""
    intended = agent_invocation.intended_from_meta(meta)
    return {
        "observed": agent_invocation.observed_from_meta(meta).as_record(),
        "intended": intended.as_record() if intended is not None else None,
    }


def _role_name(
    meta: Mapping[str, Any], is_coordinator: bool, spawned_role: str | None
) -> str:
    """The run's agent name; ``spawned_role`` overrides the coordinator label ONLY."""
    if is_coordinator:
        return role_of_name(spawned_role).value
    if not str(meta.get("agentType") or "").strip():
        return _UNKNOWN_SUBAGENT_ROLE
    return role_of_meta(meta).value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
