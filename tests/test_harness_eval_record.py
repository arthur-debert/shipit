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
    )
    assert record["gen_ai.agent.name"] == "implementer"
    assert record["eval.permission_mode"] == "bypassPermissions"
    assert record["eval.tool_call_count"] == 7
    assert record["git.commit"] == "abc123"
    assert record["eval.schema_version"] == SCHEMA_VERSION
    assert record["eval.timestamp"] == "2026-06-29T00:00:00+00:00"


def test_coordinator_record_defaults_role_when_meta_absent():
    # The coordinator run has no `.meta.json`; an absent meta IS the coordinator signal.
    record = build(
        metrics={"tool_call_count": 0},
        meta=None,
        variant=None,
        commit="deadbeef",
        timestamp="2026-06-29T00:00:00+00:00",
    )
    assert record["gen_ai.agent.name"] == "coordinator"
    assert record["eval.permission_mode"] is None


def test_variant_is_stamped_verbatim():
    # WS01 passes None; WS03's resolver fills it — build() stamps whatever it is given.
    placeholder = build(metrics={}, meta=None, variant=None, commit="c", timestamp="t")
    assert placeholder["eval.variant"] is None
    filled = build(
        metrics={},
        meta=None,
        variant={"content_hash": "sha256:deadbeef", "label": "A"},
        commit="c",
        timestamp="t",
    )
    assert filled["eval.variant"] == {"content_hash": "sha256:deadbeef", "label": "A"}


def test_tool_call_count_defaults_to_zero_int_for_partial_metrics():
    # A partial/empty metrics mapping must still yield an int (0), not None, so the
    # store stays single-typed for downstream aggregators.
    record = build(metrics={}, meta=None, variant=None, commit="c", timestamp="t")
    assert record["eval.tool_call_count"] == 0
    assert isinstance(record["eval.tool_call_count"], int)


def test_record_round_trips_through_json():
    record = build(
        metrics={"tool_call_count": 3},
        meta={"agentType": "shepherd"},
        variant=None,
        commit="abc",
        timestamp="2026-06-29T00:00:00+00:00",
    )
    assert json.loads(json.dumps(record)) == record
