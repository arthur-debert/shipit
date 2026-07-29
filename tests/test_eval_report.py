from __future__ import annotations

import io

from shipit.execrun import ExecError
from shipit.harness.eval import store
from shipit.harness.eval.record import build
from shipit.harness.eval.variant import Variant
from shipit.identity import Owner, Repo
from shipit.verbs.eval import report

_REPO = Repo(owner=Owner(login="acme"), name="widget")


def _variant(content_hash, label=None):
    return Variant(content_hash=content_hash, label=label).as_record()


def _write(base, repo, *, role, tool_calls, variant, timestamp, meta_extra=None):
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


_V1 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
_V2 = "sha256:2222222222222222222222222222222222222222222222222222222222222222"


def _write_legacy(base, repo, *, role, tool_calls, variant, timestamp):
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
    assert result.by_role == [
        report.GroupRow(key="implementer", runs=2, avg_tool_calls=15.0),
        report.GroupRow(key="coordinator", runs=1, avg_tool_calls=6.0),
    ]


def test_aggregate_groups_by_variant(tmp_path):
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    assert result.by_variant == [
        report.GroupRow(key=_V1, runs=2, avg_tool_calls=15.0),
        report.GroupRow(key=_V2, runs=1, avg_tool_calls=6.0),
    ]


def test_aggregate_groups_by_invocation(tmp_path):
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
    _, _, path = _seed(tmp_path)
    result = report.aggregate(path)
    keys = {row.key for row in result.by_invocation}
    assert keys == {"claude/?"}


def test_aggregate_tolerates_store_with_no_invocation_column(tmp_path):
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
    assert result.by_role == [
        report.GroupRow(key="implementer", runs=2, avg_tool_calls=15.0),
    ]


def test_aggregate_tolerates_mixed_invocation_schema(tmp_path):
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

    def boom(cwd, **k):
        raise ExecError(["gh"], rc=1, stderr="not a git repository")

    monkeypatch.setattr(report.identity, "resolve_repo", boom)
    buf = io.StringIO()
    rc = report.run("/not/a/repo", base_dir=tmp_path / "state", out=buf)
    assert rc == 0
    assert "empty" in buf.getvalue().lower()


def _round(
    *,
    variant,
    findings=(),
    runs=(),
    duration_ms=1000,
    total_tokens=None,
):
    from shipit.finding import Disposition, Finding, JudgedFinding, Severity
    from shipit.review import roundrecord

    return roundrecord.build(
        review={"summary": {"status": "COMMENT"}, "comments": []},
        findings=[
            JudgedFinding(
                Finding(severity=Severity(sev), text=text, file="f.py"),
                Disposition(disposition),
                duplicate_of,
            )
            for text, sev, disposition, *rest in findings
            for duplicate_of in (rest[0] if rest else None,)
        ],
        repo=_REPO.slug,
        pr=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=None,
        variant=variant,
        runs=list(runs),
        duration_ms=duration_ms,
        total_tokens=total_tokens,
        timestamp="2026-07-09T00:00:00+00:00",
    )


def _seed_rounds(tmp_path):
    base = tmp_path / "state"
    record = build(
        metrics={"tool_call_count": 3, "token_usage": {"total_tokens": 500}},
        meta={"agentType": "reviewer"},
        variant=_variant(_V1),
        commit="abc123",
        timestamp="2026-07-09T00:00:00+00:00",
        is_coordinator=False,
        run_id="agent-unjoined",
    )
    store.append_record(record, _REPO, base_dir=base)
    for round_record in (
        _round(
            variant=_variant(_V1),
            findings=[
                ("real", "major", "post"),
                ("stale", "minor", "out-of-scope"),
                ("dup", "major", "post", 0),
            ],
            runs=[
                {
                    "run_id": "pass-1",
                    "variant": _variant(_V1),
                    "usage": {"total_tokens": 2500, "source": "codex-stderr"},
                }
            ],
            duration_ms=2000,
            total_tokens=2500,
        ),
        _round(
            variant=_variant(_V1),
            findings=[("tiny", "nit", "nit-suppressed")],
            duration_ms=1000,
        ),
        _round(variant=_variant(_V2, label="arm-b"), duration_ms=500),
    ):
        store.append_record(
            round_record, _REPO, base_dir=base, kind=store.REVIEW_ROUNDS_KIND
        )
    return (
        base,
        store.store_path(_REPO, base_dir=base),
        store.store_path(_REPO, base_dir=base, kind=store.REVIEW_ROUNDS_KIND),
    )


def test_review_axis_groups_rounds_by_variant_and_splits_dispositions(tmp_path):
    _, _, rounds_path = _seed_rounds(tmp_path)
    rows = report.review_axis(rounds_path)
    assert [(r.key, r.rounds) for r in rows] == [
        (_V1, 2),
        (f"{_V2} [arm-b]", 1),
    ]
    v1 = rows[0]
    assert (v1.findings, v1.posted, v1.dropped) == (4, 1, 3)
    assert v1.avg_duration_ms == 1500.0


def test_review_axis_reads_token_cost_off_the_round_records(tmp_path):
    _, _, rounds_path = _seed_rounds(tmp_path)
    v1, v2 = report.review_axis(rounds_path)
    assert v1.token_rounds == 1
    assert v1.avg_round_tokens == 2500.0
    assert v2.token_rounds == 0
    assert v2.avg_round_tokens is None


def test_review_render_marks_latency_only_cells_as_such(tmp_path):
    _, eval_path, rounds_path = _seed_rounds(tmp_path)
    text = report.format_report(report.aggregate(eval_path, rounds_path))
    review_lines = text[text.index("Review rounds (by variant):") :].splitlines()
    v1_line = next(line for line in review_lines if line.strip().startswith(_V1))
    v2_line = next(line for line in review_lines if f"{_V2} [arm-b]" in line)
    assert "2500" in v1_line
    assert "latency-only" in v2_line
    assert "latency-only" not in v1_line


def test_review_axis_reports_rounds_even_with_no_eval_store(tmp_path):
    base = tmp_path / "state"
    store.append_record(
        _round(variant=_variant(_V1)),
        _REPO,
        base_dir=base,
        kind=store.REVIEW_ROUNDS_KIND,
    )
    eval_path = store.store_path(_REPO, base_dir=base)
    rounds_path = store.store_path(_REPO, base_dir=base, kind=store.REVIEW_ROUNDS_KIND)
    result = report.aggregate(eval_path, rounds_path)
    assert result.total_runs == 0
    assert [r.rounds for r in result.review] == [1]
    text = report.format_report(result)
    assert "Review rounds (by variant):" in text
    assert _V1 in text


def test_aggregate_without_rounds_path_has_an_empty_review_axis(tmp_path):
    _, _, eval_path = _seed(tmp_path)
    assert report.aggregate(eval_path).review == []


def test_read_jsonl_skips_a_malformed_line_loudly(tmp_path, caplog):
    import logging

    base = tmp_path / "state"
    path = store.append_record(
        _round(variant=_variant(_V1)),
        _REPO,
        base_dir=base,
        kind=store.REVIEW_ROUNDS_KIND,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{spliced garbage\n")
    with caplog.at_level(logging.WARNING, logger="shipit.harness"):
        records = report._read_jsonl(path)
    assert len(records) == 1
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert str(path) in warning
    assert "line 2" in warning


def test_read_jsonl_warns_on_a_non_object_line(tmp_path, caplog):
    import logging

    base = tmp_path / "state"
    path = store.append_record(
        _round(variant=_variant(_V1)),
        _REPO,
        base_dir=base,
        kind=store.REVIEW_ROUNDS_KIND,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write('["not", "a", "record"]\n')
    with caplog.at_level(logging.WARNING, logger="shipit.harness"):
        records = report._read_jsonl(path)
    assert len(records) == 1
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert str(path) in warning
    assert "line 2" in warning
    assert "expected a JSON object" in warning
    assert "list" in warning


def test_aggregate_waits_out_an_in_flight_exclusive_append(tmp_path):
    import fcntl
    import threading

    _, _, path = _seed(tmp_path)
    holding = threading.Event()
    release = threading.Event()

    def hold_exclusive():
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_exclusive, daemon=True)
    holder.start()
    try:
        assert holding.wait(timeout=10)
        done = threading.Event()
        result: dict = {}

        def run_aggregate():
            result["report"] = report.aggregate(path)
            done.set()

        reader = threading.Thread(target=run_aggregate, daemon=True)
        reader.start()
        assert not done.wait(timeout=0.5)
        release.set()
        assert done.wait(timeout=10)
        assert result["report"].total_runs == 3
    finally:
        release.set()
        holder.join(timeout=10)


def test_run_renders_the_review_axis_from_the_same_family_root(tmp_path, monkeypatch):
    base, _, _ = _seed_rounds(tmp_path)
    monkeypatch.setattr(report, "_resolve_repo", lambda start: _REPO)
    buf = io.StringIO()
    rc = report.run("/some/checkout", base_dir=base, out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "Review rounds (by variant):" in text
    assert f"{_V2} [arm-b]" in text
