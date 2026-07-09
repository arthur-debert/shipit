"""Review-round record: pure build, disposition seam, store write, service tee.

The record is the persisted product of one review round (RVW02-WS03): findings
WITH dispositions (routed-out ones included — the Opportunity-harvest seam),
the coverage attestation, the range reviewed, the invocation, and the
review-instructions **Variant** — appended to the review-rounds kind of the ONE
harness store family, never a repo file. The service tee is fail-open: a record
miss never degrades the review; the posting path is untouched.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from shipit.finding import Disposition, Finding, Severity
from shipit.harness.eval import store
from shipit.identity import repo_from_slug
from shipit.review import roundrecord

_REVIEW = {
    "summary": {
        "status": "REQUEST_CHANGES",
        "overall_feedback": "one blocker",
        "coverage": {
            "reviewed": ["src/a.py"],
            "skipped": [{"file": "src/b.py", "reason": "generated"}],
        },
    },
    "comments": [
        {
            "file": "src/a.py",
            "line": 3,
            "text": "off-by-one",
            "severity": "major",
            "category": "correctness",
            "confidence": 0.9,
            "evidence": "for i in range(n-1):",
            "fix": "range(n)",
        }
    ],
}


def _build(**overrides):
    kwargs = dict(
        review=_REVIEW,
        findings=roundrecord.dispositioned(_REVIEW),
        repo="acme/widget",
        pr=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=None,
        variant={"content_hash": "sha256:abc", "label": None},
        timestamp="2026-07-09T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return roundrecord.build(**kwargs)


def test_record_carries_product_range_invocation_and_round_trips_json():
    record = _build(duration_ms=1234)
    assert record["round.schema_version"] == roundrecord.SCHEMA_VERSION
    assert record["round.repo"] == "acme/widget"
    assert record["round.pr"] == 7
    assert record["round.range"] == {"base": "a" * 40, "head": "b" * 40}
    assert record["round.reviewer"] == "codex"
    assert record["round.status"] == "REQUEST_CHANGES"
    # The coverage attestation rides verbatim — silence means "clean", not "skipped".
    assert record["round.coverage"]["skipped"][0]["reason"] == "generated"
    assert record["round.invocation"] == {
        "model": "pro",
        "timeout": "600s",
        "instructions_path": None,
    }
    assert record["round.variant"] == {"content_hash": "sha256:abc", "label": None}
    assert record["round.usage"] == {"duration_ms": 1234, "total_tokens": None}
    [finding] = record["round.findings"]
    assert finding["severity"] == "major"
    assert finding["disposition"] == "post"
    assert finding["confidence"] == 0.9
    # One JSONL line — the record must serialize as-is.
    assert json.loads(json.dumps(record)) == record


def test_offline_replay_round_has_no_pr():
    # A range replay touches no PR: the record says so honestly (`round.pr` None)
    # while still carrying the range it reviewed.
    record = _build(pr=None)
    assert record["round.pr"] is None
    assert record["round.range"] == {"base": "a" * 40, "head": "b" * 40}


def test_routed_out_findings_are_recorded_with_their_disposition():
    # The Opportunity-harvest seam: a dropped finding is RETAINED with its
    # disposition — the record is never just the posted subset.
    dropped = Finding(severity=Severity.MINOR, text="pre-existing", file="old.py")
    posted = Finding(severity=Severity.MAJOR, text="real", file="src/a.py", line=3)
    record = _build(
        findings=[
            (posted, Disposition.POST),
            (dropped, Disposition.OUT_OF_SCOPE),
        ]
    )
    dispositions = {f["text"]: f["disposition"] for f in record["round.findings"]}
    assert dispositions == {"real": "post", "pre-existing": "out-of-scope"}


def test_contributing_runs_ride_verbatim_as_the_ws04_seam():
    # `round.runs` carries the run ids + variant hashes of every contributing
    # run — the join key `shipit eval report` resolves eval records by. Today's
    # single-pass producer contributes none; the shape is the WS04 seam.
    runs = [{"run_id": "agent-a7c77e10", "variant": {"content_hash": "sha256:x"}}]
    assert _build(runs=runs)["round.runs"] == runs
    assert _build()["round.runs"] == []


def test_dispositioned_maps_every_comment_to_post_via_the_trust_boundary():
    # Pre-calibrator, the whole judged output reaches the PR — every finding is
    # `post`, coerced through the SAME trust boundary the posting path uses (a
    # malformed severity lands on the `major` fail-safe, not a crash).
    review = {
        "comments": [
            {"file": "a.py", "line": 1, "text": "x", "severity": "nonsense"},
            "not-a-dict",
        ]
    }
    [(finding, disposition)] = roundrecord.dispositioned(review)
    assert disposition is Disposition.POST
    assert finding.severity is Severity.MAJOR


def test_malformed_summary_never_crashes_the_build():
    # The agy path is schema-unenforced: a non-dict summary/coverage degrades to
    # None fields, never an exception at the record seam.
    record = _build(review={"summary": "not-a-dict", "comments": []})
    assert record["round.status"] is None
    assert record["round.coverage"] is None


def test_record_round_appends_to_the_review_rounds_store(tmp_path, monkeypatch):
    # The boundary: timestamps, hashes the review INSTRUCTIONS as the round's
    # variant (the experiment-arm handle), and appends to the review-rounds KIND
    # of the one store family — repo-keyed, outside any repo tree.
    monkeypatch.setenv("SHIPIT_EVAL_VARIANT_LABEL", "arm-a")
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("review carefully", encoding="utf-8")
    path = roundrecord.record_round(
        _REVIEW,
        repo_slug="acme/widget",
        pr=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewer="codex",
        model="pro",
        timeout="600s",
        instructions_path=str(instructions),
        duration_ms=99,
        base_dir=tmp_path / "state",
    )
    assert path == store.store_path(
        repo_from_slug("acme/widget"),
        tmp_path / "state",
        kind=store.REVIEW_ROUNDS_KIND,
    )
    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["round.pr"] == 7
    assert record["round.usage"]["duration_ms"] == 99
    # The variant is the instructions content-hash + the env label: identical
    # instructions pool across PRs; an edited prompt separates arms.
    assert record["round.variant"]["content_hash"].startswith("sha256:")
    assert record["round.variant"]["label"] == "arm-a"
    assert record["round.timestamp"]  # stamped at the boundary


def test_same_instructions_pool_and_edited_instructions_separate(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")
    edited = tmp_path / "c.txt"
    edited.write_text("different", encoding="utf-8")

    def _variant(path):
        record_path = roundrecord.record_round(
            {"summary": {"status": "COMMENT"}, "comments": []},
            repo_slug="acme/widget",
            pr=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            reviewer="agy",
            model="pro",
            timeout="600s",
            instructions_path=str(path),
            base_dir=tmp_path / "state",
            env={},
        )
        return json.loads(record_path.read_text().splitlines()[-1])["round.variant"]

    assert _variant(a) == _variant(b)
    assert _variant(a) != _variant(edited)


# --- the service tee (generate time, fail-open, posting path untouched) --------


def _tee_ctx(repo="acme/widget"):
    return SimpleNamespace(
        repo=repo,
        number=5,
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="",
        workdir="/tmp/wd",
        head_ref="branch",
    )


def test_generate_review_tees_a_round_record(monkeypatch, tmp_path):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.producer, "run_tree_review", lambda backend, ctx, **kw: dict(review)
    )
    written = []
    monkeypatch.setattr(
        service.roundrecord,
        "record_round",
        lambda r, **kw: written.append((r, kw)) or tmp_path / "store.jsonl",
    )
    result = service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert result == review
    [(teed, kwargs)] = written
    assert teed == review
    assert kwargs["repo_slug"] == "acme/widget"
    assert kwargs["pr"] == 5
    assert kwargs["base_sha"] == "a" * 40
    assert kwargs["head_sha"] == "b" * 40
    assert kwargs["reviewer"] == "codex"
    assert kwargs["duration_ms"] >= 0


def test_tee_failure_is_fail_open_and_never_degrades_the_review(monkeypatch, caplog):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.producer, "run_tree_review", lambda backend, ctx, **kw: dict(review)
    )

    def _boom(*a, **k):
        raise OSError("store unwritable")

    monkeypatch.setattr(service.roundrecord, "record_round", _boom)
    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        result = service.generate_review(agent_backend.CODEX, _tee_ctx())
    assert result == review  # the review is unaffected
    assert any("review-round record" in r.getMessage() for r in caplog.records)


def test_tee_skips_cleanly_when_ctx_has_no_repo_identity(monkeypatch, caplog):
    from shipit.agent import backend as agent_backend
    from shipit.review import service

    review = {"summary": {"status": "COMMENT", "overall_feedback": ""}, "comments": []}
    monkeypatch.setattr(
        service.producer, "run_tree_review", lambda backend, ctx, **kw: dict(review)
    )
    called = []
    monkeypatch.setattr(
        service.roundrecord, "record_round", lambda *a, **k: called.append(1)
    )
    with caplog.at_level(logging.WARNING, logger="shipit.review"):
        service.generate_review(agent_backend.CODEX, _tee_ctx(repo=None))
    assert called == []  # no record — and no crash — for a repo-less ctx
