"""``shipit eval report`` aggregator: store -> expected aggregate row (HAR02-WS04).

The aggregator's value is the roll-up, so the tests assert *external behaviour*:
write known eval records to a temp store (via the real builder + store), run the
aggregation, and assert the expected aggregate rows — never DuckDB internals or
the SQL text. One thin end-to-end case drives the verb boundary through stdout.
"""

from __future__ import annotations

import io

from shipit.harness.eval import store
from shipit.harness.eval.record import build
from shipit.verbs.eval import report


def _write(base, repo, *, role, tool_calls, variant, timestamp):
    """Append one realistic eval record (built by the real builder) to the store."""
    meta = None if role == "coordinator" else {"agentType": role}
    record = build(
        metrics={"tool_call_count": tool_calls},
        meta=meta,
        variant=variant,
        commit="abc123",
        timestamp=timestamp,
    )
    store.append_record(record, repo, base_dir=base)


def _seed(tmp_path):
    """Three records: two implementer runs (variant v1, day 06-01), one
    coordinator run (variant v2, day 06-02)."""
    base = tmp_path / "state"
    repo = tmp_path / "repo"
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant="v1",
        timestamp="2026-06-01T08:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=20,
        variant="v1",
        timestamp="2026-06-01T09:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="coordinator",
        tool_calls=6,
        variant="v2",
        timestamp="2026-06-02T10:00:00+00:00",
    )
    return base, repo, store.store_path(repo, base_dir=base)


def test_aggregate_groups_by_role(tmp_path):
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    assert result.total_runs == 3
    # Most runs first: implementer (2) before coordinator (1).
    assert result.by_role == [
        report.GroupRow(key="implementer", runs=2, avg_tool_calls=15.0),
        report.GroupRow(key="coordinator", runs=1, avg_tool_calls=6.0),
    ]


def test_aggregate_groups_by_variant(tmp_path):
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    assert result.by_variant == [
        report.GroupRow(key="v1", runs=2, avg_tool_calls=15.0),
        report.GroupRow(key="v2", runs=1, avg_tool_calls=6.0),
    ]


def test_aggregate_trends_by_day(tmp_path):
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    assert result.by_day == [
        report.GroupRow(key="2026-06-01", runs=2, avg_tool_calls=15.0),
        report.GroupRow(key="2026-06-02", runs=1, avg_tool_calls=6.0),
    ]


def test_null_variant_buckets_as_none(tmp_path):
    base = tmp_path / "state"
    repo = tmp_path / "repo"
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=3,
        variant=None,
        timestamp="2026-06-03T08:00:00+00:00",
    )
    result = report.aggregate(store.store_path(repo, base_dir=base))
    assert result.by_variant == [
        report.GroupRow(key="(none)", runs=1, avg_tool_calls=3.0),
    ]


def test_aggregate_empty_store_is_empty_report(tmp_path):
    missing = tmp_path / "state" / "nope.jsonl"
    result = report.aggregate(missing)
    assert result == report.EvalReport(
        total_runs=0, by_role=[], by_variant=[], by_day=[]
    )


def test_run_prints_report_for_repo_store(tmp_path):
    base, repo, _ = _seed(tmp_path)
    buf = io.StringIO()
    rc = report.run(str(repo), base_dir=base, out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "3 run(s)" in text
    assert "implementer" in text
    assert "coordinator" in text
    assert "2026-06-01" in text


def test_run_on_empty_store_reports_no_records(tmp_path):
    base = tmp_path / "state"
    repo = tmp_path / "repo"  # never written to
    buf = io.StringIO()
    rc = report.run(str(repo), base_dir=base, out=buf)
    assert rc == 0
    assert "empty" in buf.getvalue().lower()
