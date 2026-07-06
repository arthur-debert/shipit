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

from ...agent import invocation as agent_invocation
from .variant import role_of_meta, role_of_name

#: Bump when the record's field set changes, so an aggregator can read mixed stores.
#: v2 adds the full WS02 objective metric set (tool-call vector, turn count,
#: stuck-loop, check-bypass / break-glass, error / retry, tokens, exit-hygiene).
#: v3 (COR01-WS02) adds ``eval.invocation`` — the observed + intended
#: Backend × Model × ReasoningLevel launch config (ADR-0025), a group-by dimension
#: for ``shipit eval report``.
SCHEMA_VERSION = 3

#: The role recorded for a SUBAGENT run whose meta is absent/unreadable. The locator
#: still classifies it as a subagent (off the transcript filename), but with no
#: `.meta.json` we cannot name *which* worker role ran — so it stamps this distinct
#: sentinel rather than defaulting to ``coordinator`` (which would pollute the
#: coordinator aggregate and contradict the locator's own ``is_coordinator``).
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
) -> dict[str, Any]:
    """Assemble the eval record for one run.

    ``meta`` is the parsed `agent-<id>.meta.json` for a subagent run, or ``None``
    for the coordinator (whose role is implied) — and also for a subagent whose meta
    sidecar was missing/unreadable. ``is_coordinator`` is the locator's run-kind
    classification (off the transcript filename, NOT off whether ``meta`` parsed), so
    a subagent with an unreadable meta is never mistaken for the coordinator.
    ``spawned_role`` is the role from a spawned top-level Run's launch context (the
    eval seam reads ``SHIPIT_LOG_CTX_ROLE``); it overrides ONLY the would-be
    ``coordinator`` label (see :func:`_role_name`). ``variant`` is stamped verbatim
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
        "gen_ai.agent.name": _role_name(meta, is_coordinator, spawned_role),
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
        "eval.invocation": _invocation_record(meta),
        "git.commit": commit,
    }


def _invocation_record(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """The run's :class:`shipit.agent.Invocation` attribution — observed + intended.

    The **observed** launch config (Backend × Model × ReasoningLevel + permission_mode)
    is read from the run's ``.meta.json`` (:func:`shipit.agent.observed_from_meta`); the
    **intended** side is a clean seam — ``None`` until the spawn surface stamps an
    ``invocation`` intent block into the meta (:func:`shipit.agent.intended_from_meta`),
    mirroring how ``variant`` was staged. ``shipit eval report`` groups by the observed
    config, so the harness can compare configurations (ADR-0025).
    """
    intended = agent_invocation.intended_from_meta(meta)
    return {
        "observed": agent_invocation.observed_from_meta(meta).as_record(),
        "intended": intended.as_record() if intended is not None else None,
    }


def _role_name(
    meta: Mapping[str, Any], is_coordinator: bool, spawned_role: str | None
) -> str:
    """The ``gen_ai.agent.name`` stamped for a run — the SAME role resolution the
    variant uses, plus the run-kind distinction the locator already drew.

    - The coordinator run (``is_coordinator``) is the ``coordinator`` — UNLESS it is
      a spawned top-level Run. A headless ``shipit spawn subagent --role R`` Run is
      its own top-level session (no ``agent-`` transcript, no ``.meta.json``), so the
      locator classifies it a coordinator, yet it is really the role it was spawned
      as. The spawn threaded that role into the child's environment
      (``SHIPIT_LOG_CTX_ROLE``), read at the eval seam and passed IN as
      ``spawned_role``: a non-blank value resolves through
      :func:`shipit.harness.eval.variant.role_of_name` (the SAME rules the meta path
      uses) and OVERRIDES the coordinator label. The genuine interactive coordinator
      was not spawned via that channel, so ``spawned_role`` is blank/``None`` and it
      stays ``coordinator``. The override applies to the coordinator branch ONLY —
      never to a subagent, whose own ``agentType`` is authoritative (a nested
      subagent inherits the parent Run's ambient ``SHIPIT_LOG_CTX_ROLE``, so
      consulting it there would mislabel).
    - A subagent with a readable ``agentType`` resolves through
      :func:`shipit.harness.eval.variant.role_of_meta` — the SAME resolver the
      variant attribution uses — so the record's role field and the variant's
      prompt selection agree, casing/whitespace are normalized, and an unknown
      non-empty role attributes to a generic worker (``implementer``) rather than
      pooling under the coordinator.
    - A subagent whose meta is missing/unreadable (no ``agentType``) is a known
      subagent of an UNKNOWN role, so it stamps :data:`_UNKNOWN_SUBAGENT_ROLE` —
      neither the coordinator nor a guessed worker.
    """
    if is_coordinator:
        # A spawned top-level Run's role rides in as ``spawned_role``; a
        # blank/``None`` (the genuine interactive coordinator) resolves to
        # ``coordinator``, so the one call covers both the spawned-role override
        # and the coordinator default.
        return role_of_name(spawned_role).value
    if not str(meta.get("agentType") or "").strip():
        return _UNKNOWN_SUBAGENT_ROLE
    return role_of_meta(meta).value


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` if it is a mapping, else an empty mapping.

    Lets :func:`build` read sub-blocks (``stuck_loop`` / ``token_usage`` /
    ``exit_hygiene``) uniformly whether the extractor produced them or returned
    ``None`` (no tokens logged, subagent run with no exit-hygiene block).
    """
    return value if isinstance(value, Mapping) else {}
