"""Eval-record builder — assemble one run's JSONL **eval record** (pure).

`build(...)` takes the extracted metrics, the run's meta (``None`` for the
coordinator), the **variant** attribution, the `git.commit`, and a timestamp, and
returns the dict written as one JSONL line. Field names follow OpenTelemetry
`gen_ai.*` for the standard agent fields and `eval.*` for the harness-local ones
(docs/prd/har02-run-eval.md, module #3); `git.commit` correlates the record to
repo state without the record ever entering the tree.

Pure: a function of its arguments only (the timestamp and commit are passed in by
the boundary), so the record shape is unit-testable from fixtures. The **variant**
is a clean seam — WS01 stamps the placeholder the caller passes (``None``); WS03's
variant resolver fills it with the role-prompt content-hash + optional A/B label.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Bump when the record's field set changes, so an aggregator can read mixed stores.
#: v2 adds the full WS02 objective metric set (tool-call vector, turn count,
#: stuck-loop, check-bypass / break-glass, error / retry, tokens, exit-hygiene).
SCHEMA_VERSION = 2

#: The role recorded for a run whose meta is absent — the coordinator's session
#: transcript has no `.meta.json`, so an absent meta *is* the coordinator signal.
_COORDINATOR_ROLE = "coordinator"


def build(
    *,
    metrics: Mapping[str, Any],
    meta: Mapping[str, Any] | None,
    variant: Any,
    commit: str | None,
    timestamp: str,
) -> dict[str, Any]:
    """Assemble the eval record for one run.

    ``meta`` is the parsed `agent-<id>.meta.json` for a subagent run, or ``None``
    for the coordinator (whose role is implied). ``variant`` is stamped verbatim
    (WS01 passes ``None``; WS03 fills it). ``commit`` is the stamping `git.commit`
    (``None`` when it could not be resolved — the record is still valid).

    The objective metrics fold in from :func:`shipit.harness.eval.extractors.extract`
    under stable OTel ``gen_ai.usage.*`` names for the standard token fields and
    ``eval.*`` for the harness-local ones, so the store is single-typed for the
    aggregator. The coordinator-only ``exit_hygiene`` block is present in ``metrics``
    for the coordinator run and absent for a subagent (its fields stamp ``None``).
    """
    meta = meta or {}
    stuck = _mapping(metrics.get("stuck_loop"))
    tokens = _mapping(metrics.get("token_usage"))
    hygiene = _mapping(metrics.get("exit_hygiene"))
    return {
        "eval.schema_version": SCHEMA_VERSION,
        "eval.timestamp": timestamp,
        "gen_ai.agent.name": meta.get("agentType") or _COORDINATOR_ROLE,
        "gen_ai.request.model": meta.get("model"),
        "eval.permission_mode": meta.get("spawnMode"),
        # Tool usage.
        "eval.tool_call_count": metrics.get("tool_call_count") or 0,
        "eval.tool_call_vector": dict(_mapping(metrics.get("tool_call_vector"))),
        "eval.turn_count": metrics.get("turn_count") or 0,
        # Stuck-loop fingerprints.
        "eval.stuck_loop": bool(stuck.get("detected")),
        "eval.max_repeated_calls": stuck.get("max_repeated_calls") or 0,
        "eval.max_turn_iterations": stuck.get("max_turn_iterations") or 0,
        # Check-bypass / break-glass / errors.
        "eval.no_verify_count": metrics.get("no_verify_count") or 0,
        "eval.break_glass_count": metrics.get("break_glass_count") or 0,
        "eval.error_count": metrics.get("error_count") or 0,
        "eval.retry_count": metrics.get("retry_count") or 0,
        # Token totals (None when the transcript logged none).
        "gen_ai.usage.input_tokens": tokens.get("input_tokens"),
        "gen_ai.usage.output_tokens": tokens.get("output_tokens"),
        "eval.usage.cache_read_tokens": tokens.get("cache_read_tokens"),
        "eval.usage.cache_creation_tokens": tokens.get("cache_creation_tokens"),
        "eval.usage.total_tokens": tokens.get("total_tokens"),
        # Exit hygiene (coordinator run only; None for a subagent run).
        "eval.exit_hygiene.worktree_clean": hygiene.get("worktree_clean"),
        "eval.exit_hygiene.dirty_file_count": hygiene.get("dirty_file_count"),
        "eval.exit_hygiene.stray_pid_count": hygiene.get("stray_pid_count"),
        "eval.variant": variant,
        "git.commit": commit,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` if it is a mapping, else an empty mapping.

    Lets :func:`build` read sub-blocks (``stuck_loop`` / ``token_usage`` /
    ``exit_hygiene``) uniformly whether the extractor produced them or returned
    ``None`` (no tokens logged, subagent run with no exit-hygiene block).
    """
    return value if isinstance(value, Mapping) else {}
