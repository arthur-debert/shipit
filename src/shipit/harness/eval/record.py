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
SCHEMA_VERSION = 1

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
    """
    meta = meta or {}
    return {
        "eval.schema_version": SCHEMA_VERSION,
        "eval.timestamp": timestamp,
        "gen_ai.agent.name": meta.get("agentType") or _COORDINATOR_ROLE,
        "gen_ai.request.model": meta.get("model"),
        "eval.permission_mode": meta.get("spawnMode"),
        "eval.tool_call_count": metrics.get("tool_call_count"),
        "eval.variant": variant,
        "git.commit": commit,
    }
