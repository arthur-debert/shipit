"""Eval-record builder: assert the record shape (OTel names, stamps, JSON-parses).

The record is the unit the store and (HAR02-WS04) the aggregator read, so the test
pins its external contract: `gen_ai.*` for the standard agent fields, `eval.*` for
the harness-local ones, `git.commit` + `variant` stamped, and the whole thing
round-trips through JSON.
"""

from __future__ import annotations

import json

from shipit.harness.eval.record import SCHEMA_VERSION, build


def test_subagent_record_carries_role_and_metrics_from_meta():
    record = build(
        metrics={"tool_call_count": 7},
        meta={"agentType": "implementer", "spawnMode": "bypassPermissions"},
        variant=None,
        commit="abc123",
        timestamp="2026-06-29T00:00:00+00:00",
        is_coordinator=False,
    )
    assert record["gen_ai.agent.name"] == "implementer"
    assert record["eval.permission_mode"] == "bypassPermissions"
    assert record["eval.tool_call_count"] == 7
    assert record["git.commit"] == "abc123"
    assert record["eval.schema_version"] == SCHEMA_VERSION
    assert record["eval.timestamp"] == "2026-06-29T00:00:00+00:00"


def test_record_carries_observed_and_intended_invocation():
    # ADR-0025 / COR01-WS02: the record threads the Backend × Model × ReasoningLevel
    # launch config — observed from the meta, intended a seam (None until stamped).
    record = build(
        metrics={"tool_call_count": 7},
        meta={
            "agentType": "implementer",
            "spawnMode": "bypassPermissions",
            "model": "gpt-5.5",
            "reasoning": "high",
            "backend": "codex",
        },
        variant=None,
        commit="abc123",
        timestamp="2026-06-29T00:00:00+00:00",
        is_coordinator=False,
    )
    assert record["eval.invocation"] == {
        "observed": {
            "backend": "codex",
            "model": "gpt-5.5",
            "provider": "openai",
            "reasoning_level": "high",
            "permission_mode": "bypassPermissions",
        },
        "intended": None,
    }


def test_coordinator_record_still_records_observed_invocation():
    # Even the coordinator run (meta=None) records an observed invocation: the eval
    # hooks fire for Claude Code, so the backend defaults to claude.
    record = build(
        metrics={"tool_call_count": 0},
        meta=None,
        variant=None,
        commit="deadbeef",
        timestamp="2026-06-29T00:00:00+00:00",
        is_coordinator=True,
    )
    assert record["eval.invocation"]["observed"]["backend"] == "claude"
    assert record["eval.invocation"]["observed"]["model"] is None
    assert record["eval.invocation"]["intended"] is None


def test_coordinator_record_defaults_role_when_meta_absent():
    # The coordinator run has no `.meta.json`; the locator's `is_coordinator` (NOT a
    # parsed meta) is the coordinator signal.
    record = build(
        metrics={"tool_call_count": 0},
        meta=None,
        variant=None,
        commit="deadbeef",
        timestamp="2026-06-29T00:00:00+00:00",
        is_coordinator=True,
    )
    assert record["gen_ai.agent.name"] == "coordinator"
    assert record["eval.permission_mode"] is None


def test_subagent_with_missing_meta_is_not_mis_stamped_as_coordinator():
    # A subagent whose `.meta.json` was missing/unreadable parses to meta=None, but
    # the locator still classifies it as a subagent. It must NOT pool under the
    # coordinator (which would pollute that aggregate); it stamps a distinct sentinel.
    record = build(
        metrics={"tool_call_count": 2},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=False,
    )
    assert record["gen_ai.agent.name"] == "unknown-subagent"


def test_subagent_role_is_normalized_like_the_variant_resolver():
    # Casing/whitespace on agentType must normalize to the same role the variant
    # resolver picks, so the record's role field and the variant attribution agree.
    record = build(
        metrics={},
        meta={"agentType": "  Shepherd  "},
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=False,
    )
    assert record["gen_ai.agent.name"] == "shepherd"


def test_unknown_subagent_role_attributes_to_worker_not_coordinator():
    # An unrecognized non-empty agentType is still a worker, NOT the coordinator —
    # it resolves to the generic worker role rather than pooling under coordinator.
    record = build(
        metrics={},
        meta={"agentType": "some-future-role"},
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=False,
    )
    assert record["gen_ai.agent.name"] == "implementer"


def test_spawned_role_overrides_the_would_be_coordinator_label():
    # #490: a headless `shipit spawn subagent --role implementer` Run is its own
    # top-level session (is_coordinator=True), but it is really the implementer — the
    # launch-context role must override the coordinator label.
    record = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
        spawned_role="implementer",
    )
    assert record["gen_ai.agent.name"] == "implementer"
    shepherd = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
        spawned_role="shepherd",
    )
    assert shepherd["gen_ai.agent.name"] == "shepherd"


def test_absent_spawned_role_leaves_the_coordinator_label_unchanged():
    # The genuine interactive coordinator carries no SHIPIT_LOG_CTX_ROLE — a
    # None/blank spawned_role must leave the coordinator label exactly as before.
    for spawned in (None, "", "   "):
        record = build(
            metrics={},
            meta=None,
            variant=None,
            commit="c",
            timestamp="t",
            is_coordinator=True,
            spawned_role=spawned,
        )
        assert record["gen_ai.agent.name"] == "coordinator"


def test_spawned_role_is_normalized_and_unknown_falls_back_to_worker():
    # Case/whitespace normalize like role_of_meta, and an unknown non-blank role
    # attributes to the generic worker (implementer), not the coordinator.
    padded = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
        spawned_role="  Implementer  ",
    )
    assert padded["gen_ai.agent.name"] == "implementer"
    unknown = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
        spawned_role="some-future-role",
    )
    assert unknown["gen_ai.agent.name"] == "implementer"


def test_spawned_role_never_touches_a_subagent_record():
    # A subagent (is_coordinator=False) resolves from its own meta agentType; a stray
    # spawned_role (an inherited ambient SHIPIT_LOG_CTX_ROLE) must NOT override it.
    record = build(
        metrics={},
        meta={"agentType": "reviewer"},
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=False,
        spawned_role="implementer",
    )
    assert record["gen_ai.agent.name"] == "reviewer"


def test_variant_is_stamped_verbatim():
    # WS01 passes None; WS03's resolver fills it — build() stamps whatever it is given.
    placeholder = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
    )
    assert placeholder["eval.variant"] is None
    filled = build(
        metrics={},
        meta=None,
        variant={"content_hash": "sha256:deadbeef", "label": "A"},
        commit="c",
        timestamp="t",
        is_coordinator=True,
    )
    assert filled["eval.variant"] == {"content_hash": "sha256:deadbeef", "label": "A"}


def test_tool_call_count_defaults_to_zero_int_for_partial_metrics():
    # A partial/empty metrics mapping must still yield an int (0), not None, so the
    # store stays single-typed for downstream aggregators.
    record = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
    )
    assert record["eval.tool_call_count"] == 0
    assert isinstance(record["eval.tool_call_count"], int)


def test_record_round_trips_through_json():
    record = build(
        metrics={"tool_call_count": 3},
        meta={"agentType": "shepherd"},
        variant=None,
        commit="abc",
        timestamp="2026-06-29T00:00:00+00:00",
        is_coordinator=False,
    )
    assert json.loads(json.dumps(record)) == record


def test_record_folds_in_full_ws02_metric_set():
    # The WS02 metrics fold into stable OTel `gen_ai.usage.*` + harness `eval.*` names.
    record = build(
        metrics={
            "tool_call_count": 9,
            "tool_call_vector": {"Bash": 5, "Read": 4},
            "turn_count": 6,
            "stuck_loop": {
                "detected": True,
                "max_repeated_calls": 4,
                "max_turn_iterations": 9,
            },
            "no_verify_count": 1,
            "break_glass_count": 2,
            "error_count": 3,
            "retry_count": 1,
            "token_usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 5,
                "cache_creation_tokens": 7,
                "total_tokens": 120,
            },
            "exit_hygiene": {
                "worktree_clean": False,
                "dirty_file_count": 2,
                "stray_pid_count": 1,
            },
        },
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
    )
    assert record["eval.tool_call_vector"] == {"Bash": 5, "Read": 4}
    assert record["eval.turn_count"] == 6
    assert record["eval.stuck_loop"] is True
    assert record["eval.max_repeated_calls"] == 4
    assert record["eval.max_turn_iterations"] == 9
    assert record["eval.no_verify_count"] == 1
    assert record["eval.break_glass_count"] == 2
    assert record["eval.error_count"] == 3
    assert record["eval.retry_count"] == 1
    assert record["gen_ai.usage.input_tokens"] == 100
    assert record["gen_ai.usage.output_tokens"] == 20
    assert record["eval.usage.total_tokens"] == 120
    assert record["eval.exit_hygiene.worktree_clean"] is False
    assert record["eval.exit_hygiene.dirty_file_count"] == 2
    assert record["eval.exit_hygiene.stray_pid_count"] == 1


def test_record_token_and_hygiene_fields_are_none_when_absent():
    # No tokens logged and no exit-hygiene block (subagent) → None, not hollow zeros.
    record = build(
        metrics={},
        meta=None,
        variant=None,
        commit="c",
        timestamp="t",
        is_coordinator=True,
    )
    assert record["gen_ai.usage.input_tokens"] is None
    assert record["eval.usage.total_tokens"] is None
    assert record["eval.exit_hygiene.worktree_clean"] is None
    assert record["eval.stuck_loop"] is False
    assert record["eval.turn_count"] == 0
