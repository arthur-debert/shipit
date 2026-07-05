"""``shipit eval report`` aggregator: store -> expected aggregate row (HAR02-WS04).

The aggregator's value is the roll-up, so the tests assert *external behaviour*:
write known eval records to a temp store (via the real builder + store), run the
aggregation, and assert the expected aggregate rows — never DuckDB internals or
the SQL text. One thin end-to-end case drives the verb boundary through stdout.
"""

from __future__ import annotations

import io

from shipit.execrun import ExecError
from shipit.harness.eval import store
from shipit.harness.eval.record import build
from shipit.harness.eval.variant import Variant
from shipit.identity import Owner, Repo
from shipit.verbs.eval import report

#: The identity every seeded run keys under — the store is keyed by `Repo` identity
#: (origin owner/name), not a filesystem path (ADR-0024).
_REPO = Repo(owner=Owner(login="acme"), name="widget")


def _variant(content_hash, label=None):
    """The real persisted variant shape — what ``Variant.as_record()`` produces and
    the hook writes (a nested object, NOT a plain string)."""
    return Variant(content_hash=content_hash, label=label).as_record()


def _write(base, repo, *, role, tool_calls, variant, timestamp, meta_extra=None):
    """Append one realistic eval record (built by the real builder) to the store."""
    meta = None if role == "coordinator" else {"agentType": role}
    if meta is not None and meta_extra:
        meta = {**meta, **meta_extra}
    record = build(
        metrics={"tool_call_count": tool_calls},
        meta=meta,
        variant=variant,
        commit="abc123",
        timestamp=timestamp,
        is_coordinator=role == "coordinator",
    )
    store.append_record(record, repo, base_dir=base)


#: The variant content-hashes the seeded runs carry — the real ``sha256:`` key shape.
_V1 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
_V2 = "sha256:2222222222222222222222222222222222222222222222222222222222222222"


def _write_legacy(base, repo, *, role, tool_calls, variant, timestamp):
    """Append a PRE-v3 eval record — one with NO ``eval.invocation`` key at all.

    Simulates a record written before WS02 added the invocation dimension (including
    ones already in the current Repo-keyed store from the WS01→WS02 window): the real
    builder always stamps `eval.invocation` now, so we build a normal record and strip
    the key to reproduce the old on-disk shape. A store of only these has NO
    `eval.invocation` column for DuckDB to bind."""
    meta = None if role == "coordinator" else {"agentType": role}
    record = build(
        metrics={"tool_call_count": tool_calls},
        meta=meta,
        variant=variant,
        commit="abc123",
        timestamp=timestamp,
        is_coordinator=role == "coordinator",
    )
    record.pop("eval.invocation", None)
    store.append_record(record, repo, base_dir=base)


def _seed(tmp_path):
    """Three records: two implementer runs (variant V1, day 06-01), one
    coordinator run (variant V2, day 06-02). Variants are the real nested-object
    shape the hook persists, so the by-variant grouping is exercised against the
    actual stored type, not a plain-string stand-in."""
    base = tmp_path / "state"
    repo = _REPO
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant=_variant(_V1),
        timestamp="2026-06-01T08:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=20,
        variant=_variant(_V1),
        timestamp="2026-06-01T09:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="coordinator",
        tool_calls=6,
        variant=_variant(_V2),
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
    # The real persisted variant is a nested object; grouping must key on its
    # content-hash (a null label collapses to the bare hash), NOT the struct's
    # text repr. So the V1 runs pool under the V1 content-hash, etc.
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    assert result.by_variant == [
        report.GroupRow(key=_V1, runs=2, avg_tool_calls=15.0),
        report.GroupRow(key=_V2, runs=1, avg_tool_calls=6.0),
    ]


def test_aggregate_groups_by_invocation(tmp_path):
    # The observed Backend × Model × ReasoningLevel launch config (ADR-0025) is a
    # group-by dimension: two runs at the same (backend, model, reasoning) pool; a
    # different reasoning level (or model) separates. Records carry the model /
    # reasoning in their meta (the observed config the harness reads).
    base = tmp_path / "state"
    repo = _REPO
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant=_variant(_V1),
        timestamp="2026-06-01T08:00:00+00:00",
        meta_extra={"model": "gpt-5.5", "reasoning": "high", "backend": "codex"},
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=20,
        variant=_variant(_V1),
        timestamp="2026-06-01T09:00:00+00:00",
        meta_extra={"model": "gpt-5.5", "reasoning": "high", "backend": "codex"},
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=4,
        variant=_variant(_V1),
        timestamp="2026-06-01T10:00:00+00:00",
        meta_extra={"model": "gpt-5.5", "reasoning": "low", "backend": "codex"},
    )
    path = store.store_path(repo, base_dir=base)
    result = report.aggregate(path)
    assert result.by_invocation == [
        report.GroupRow(key="codex/gpt-5.5 (high)", runs=2, avg_tool_calls=15.0),
        report.GroupRow(key="codex/gpt-5.5 (low)", runs=1, avg_tool_calls=4.0),
    ]


def test_invocation_with_no_observed_model_buckets_under_backend(tmp_path):
    # Every record records an observed invocation (backend defaults to claude for a
    # Claude Code run), so a run whose meta names no model still groups — under
    # "claude/?" (the '?' standing in for the unknown model) rather than vanishing.
    # The seeded runs carry meta without a model, so all bucket together.
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    keys = {row.key for row in result.by_invocation}
    assert keys == {"claude/?"}


def test_aggregate_tolerates_store_with_no_invocation_column(tmp_path):
    # A store of ONLY pre-v3 records (no `eval.invocation` key on any row) has NO
    # such column for DuckDB to infer, so a naive query naming it would fail to bind.
    # The report must stay schema-tolerant: it buckets every old row under "(none)"
    # and still rolls up the other dimensions, rather than raising. (Forward-compat
    # WITHIN the store's own history — NOT compat with the orphaned path-keyed stores.)
    base = tmp_path / "state"
    repo = _REPO
    _write_legacy(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant=_variant(_V1),
        timestamp="2026-06-01T08:00:00+00:00",
    )
    _write_legacy(
        base,
        repo,
        role="implementer",
        tool_calls=20,
        variant=_variant(_V1),
        timestamp="2026-06-01T09:00:00+00:00",
    )
    result = report.aggregate(store.store_path(repo, base_dir=base))
    assert result.total_runs == 2
    assert result.by_invocation == [
        report.GroupRow(key="(none)", runs=2, avg_tool_calls=15.0),
    ]
    # The other roll-ups still work over the mixed/old shape.
    assert result.by_role == [
        report.GroupRow(key="implementer", runs=2, avg_tool_calls=15.0),
    ]


def test_aggregate_tolerates_mixed_invocation_schema(tmp_path):
    # A store with SOME rows carrying `eval.invocation` and some missing it (the
    # WS01→WS02 window): the new rows group by their observed config, the old rows
    # fall under "(none)" — the report never raises on the mixed schema.
    base = tmp_path / "state"
    repo = _REPO
    _write_legacy(
        base,
        repo,
        role="implementer",
        tool_calls=4,
        variant=_variant(_V1),
        timestamp="2026-06-01T07:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant=_variant(_V1),
        timestamp="2026-06-01T08:00:00+00:00",
        meta_extra={"model": "gpt-5.5", "reasoning": "high", "backend": "codex"},
    )
    result = report.aggregate(store.store_path(repo, base_dir=base))
    assert result.total_runs == 2
    assert result.by_invocation == [
        report.GroupRow(key="(none)", runs=1, avg_tool_calls=4.0),
        report.GroupRow(key="codex/gpt-5.5 (high)", runs=1, avg_tool_calls=10.0),
    ]


def test_aggregate_separates_ab_label_arms_of_the_same_prompt(tmp_path):
    """Two runs of the SAME prompt (same content-hash) tagged with different A/B
    labels must separate into distinct variant buckets — that is what makes a
    same-prompt A/B separable by data (CONTEXT.md "variant")."""
    base = tmp_path / "state"
    repo = _REPO
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant=_variant(_V1, label="arm-a"),
        timestamp="2026-06-01T08:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=20,
        variant=_variant(_V1, label="arm-b"),
        timestamp="2026-06-01T09:00:00+00:00",
    )
    result = report.aggregate(store.store_path(repo, base_dir=base))
    assert result.by_variant == [
        report.GroupRow(key=f"{_V1} [arm-a]", runs=1, avg_tool_calls=10.0),
        report.GroupRow(key=f"{_V1} [arm-b]", runs=1, avg_tool_calls=20.0),
    ]


def test_aggregate_trends_by_day(tmp_path):
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    assert result.by_day == [
        report.GroupRow(key="2026-06-01", runs=2, avg_tool_calls=15.0),
        report.GroupRow(key="2026-06-02", runs=1, avg_tool_calls=6.0),
    ]


def test_aggregate_trends_by_day_is_chronological_not_by_run_count(tmp_path):
    """The day trend must read oldest→newest even when an earlier day has FEWER
    runs than a later one — i.e. run-count ordering would reverse them.

    Seeds an older day (06-01, 1 run) and a busier newer day (06-02, 2 runs): a
    ``runs DESC`` ordering would surface 06-02 first, so asserting 06-01 first
    proves the day roll-up orders by the date key, not by run count.
    """
    base = tmp_path / "state"
    repo = _REPO
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=5,
        variant=_variant(_V1),
        timestamp="2026-06-01T08:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=10,
        variant=_variant(_V1),
        timestamp="2026-06-02T08:00:00+00:00",
    )
    _write(
        base,
        repo,
        role="implementer",
        tool_calls=20,
        variant=_variant(_V1),
        timestamp="2026-06-02T09:00:00+00:00",
    )
    result = report.aggregate(store.store_path(repo, base_dir=base))
    assert result.by_day == [
        report.GroupRow(key="2026-06-01", runs=1, avg_tool_calls=5.0),
        report.GroupRow(key="2026-06-02", runs=2, avg_tool_calls=15.0),
    ]


def test_null_variant_buckets_as_none(tmp_path):
    base = tmp_path / "state"
    repo = _REPO
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
        total_runs=0, by_role=[], by_variant=[], by_invocation=[], by_day=[]
    )


def test_run_prints_report_for_repo_store(tmp_path, monkeypatch):
    # The verb resolves its path argument to a `Repo` identity (via the origin
    # remote) and reads THAT store — so the reader lands on exactly the store the
    # seeded runs wrote under. The resolver is stubbed to the seeded repo.
    base, repo, _ = _seed(tmp_path)
    monkeypatch.setattr(report.identity, "resolve_repo", lambda cwd, **k: repo)
    buf = io.StringIO()
    rc = report.run("/some/checkout", base_dir=base, out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "3 run(s)" in text
    assert "implementer" in text
    assert "coordinator" in text
    assert "2026-06-01" in text


def test_run_on_empty_store_reports_no_records(tmp_path, monkeypatch):
    base = tmp_path / "state"
    monkeypatch.setattr(report.identity, "resolve_repo", lambda cwd, **k: _REPO)
    buf = io.StringIO()
    rc = report.run("/some/checkout", base_dir=base, out=buf)
    assert rc == 0
    assert "empty" in buf.getvalue().lower()


def test_run_on_a_non_checkout_reports_no_records(tmp_path, monkeypatch):
    # A path that is not a checkout (or has no origin) has no per-repo store: the
    # resolver raises and the verb degrades to the empty report rather than erroring.

    def boom(cwd, **k):
        raise ExecError(["gh"], rc=1, stderr="not a git repository")

    monkeypatch.setattr(report.identity, "resolve_repo", boom)
    buf = io.StringIO()
    rc = report.run("/not/a/repo", base_dir=tmp_path / "state", out=buf)
    assert rc == 0
    assert "empty" in buf.getvalue().lower()
