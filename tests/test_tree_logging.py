"""LOG02-WS01 (#246): the Tree axis narrates its lifecycle on the spray conventions.

Convention-level assertions ONLY: the key lifecycle events EXIST and carry the
REQUIRED FIELDS — level, logger, bound domain keys (``tree``/``session``),
durations where meaningful, the exception attached on a propagating failure —
never per-message string pinning (ADR-0029; glassbox PRD spray conventions).

Two capture styles, each used for what it proves best:

- the REAL JSONL file sink (``logsetup.configure_logging`` with an injected
  ``base_dir``) for the creation pipeline, because only the rendered record
  proves the bound domain keys actually LAND on the durable log; and
- ``caplog`` for the per-module events (ladder decisions, removal, sweep),
  asserting on record attributes (extras land as attributes via ``extra=``),
  which is exactly what the pipeline's ``ExtraAdder`` adopts into the record.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from shipit import execrun, git, logsetup
from shipit.identity import Sha, repo_from_slug
from shipit.tree import cleanup, provision
from shipit.tree import gc as gc_mod
from shipit.tree import registry as registry_mod
from shipit.tree import removal as removal_mod
from shipit.tree.create import create
from shipit.tree.layout import TreeSpec
from shipit.tree.readonly import create_readonly, readonly_plan, remove_tree
from shipit.tree.registry import TreeRecord
from shipit.verbs import tree as tree_verb

_REPO = repo_from_slug("acme/widget")

#: An onboarded .shipit.toml body — provisioning fails closed without one (#210).
_ONBOARDED = '[shipit]\nversion = "seed"\n\n[managed]\n'


@pytest.fixture(autouse=True)
def _reset_package_logger():
    """Fully reset the process-lifetime ``shipit`` logger around each test, so the
    file sink the JSONL tests attach never leaks into the next test (mirrors
    ``test_logsetup``'s fixture)."""
    logger = logging.getLogger(logsetup.LOGGER_NAME)
    saved = list(logger.handlers)
    saved_level, saved_prop = logger.level, logger.propagate
    for handler in saved:
        logger.removeHandler(handler)
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for handler in saved:
            logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_prop


@pytest.fixture
def jsonl_log(tmp_path):
    """Attach the REAL per-repo JSONL file sink and return a records reader.

    The reader parses the rendered durable record, so an assertion on a field
    (``tree``, ``session``, ``duration_ms``, ``exception``) proves the whole
    convention end to end: context-merge -> extras adoption -> flat JSONL.
    """
    base = tmp_path / "logbase"
    logsetup.configure_logging(env={}, repo=_REPO, base_dir=base)

    def read() -> list[dict]:
        path = logsetup.log_file_path(_REPO, base_dir=base)
        if not path.is_file():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]

    return read


def _mock_write_boundary(monkeypatch):
    """Patch the git boundary so ``create`` runs its full pipeline with no real git.

    The fake clone carries an ONBOARDED ``.shipit.toml`` (provisioning fails
    closed otherwise) and no pixi/npm manifests, so exactly one provisioning
    step (``shipit install . --local``) runs — through a canned Exec result.
    """

    def fake_clone(url, dest, *, reference):
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()
        (d / ".shipit.toml").write_text(_ONBOARDED)

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(git, "checkout_new_branch", lambda *a, **k: None)
    monkeypatch.setattr(git, "head_commit", lambda **k: Sha("abc123" + "0" * 34))
    monkeypatch.setattr(
        execrun,
        "run",
        lambda cmd, **k: execrun.ExecResult(
            argv=tuple(cmd), rc=0, stdout="", stderr="", duration_ms=42
        ),
    )


def _spec(tmp_path: Path) -> TreeSpec:
    return TreeSpec(
        repo=_REPO,
        agent_hash="abcd1234",
        issue=7,
        root=tmp_path / "trees",
    )


def _source(tmp_path: Path) -> str:
    src = tmp_path / "src"
    src.mkdir()
    return str(src)


# --------------------------------------------------------------------------
# Creation — milestones at info with durations, domain keys on every record
# --------------------------------------------------------------------------


def test_create_records_carry_tree_and_session_keys_and_durations(
    tmp_path, monkeypatch, jsonl_log
):
    _mock_write_boundary(monkeypatch)

    tree = create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    records = [r for r in jsonl_log() if r.get("logger") == "shipit.tree"]
    assert records, "the creation pipeline must narrate on the tree logger"
    # The flat-record contract's base fields, on every record.
    for record in records:
        assert {"ts", "level", "logger", "msg"} <= record.keys()
    # The Tree-birth seam binds its domain keys: EVERY tree-axis record of the
    # run carries the Tree, and the issue shape's session identity.
    assert all(r.get("tree") == tree.path for r in records)
    assert all(r.get("session") == "work" for r in records)
    # Lifecycle milestones at INFO with durations where meaningful: at least
    # the per-provisioning-step record AND the created-milestone record.
    timed_infos = [
        r
        for r in records
        if r["level"] == "info" and isinstance(r.get("duration_ms"), int)
    ]
    assert len(timed_infos) >= 2


def test_create_failure_is_an_error_record_with_the_exception_attached(
    tmp_path, monkeypatch, jsonl_log
):
    _mock_write_boundary(monkeypatch)
    monkeypatch.setattr(
        git,
        "checkout_new_branch",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError):
        create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    errors = [
        r
        for r in jsonl_log()
        if r.get("logger") == "shipit.tree" and r["level"] == "error"
    ]
    assert errors, "a propagating create failure must land an ERROR record"
    # ...with the exception attached and the Tree key bound.
    assert any("exception" in r and r.get("tree") for r in errors)


def test_ephemeral_shape_binds_its_session_id(tmp_path, monkeypatch, jsonl_log):
    _mock_write_boundary(monkeypatch)
    spec = TreeSpec(
        repo=_REPO,
        agent_hash="ignored",
        ephemeral="sess-1234",
        root=tmp_path / "trees",
    )

    create(spec, source_repo=_source(tmp_path), github_url="url")

    records = [r for r in jsonl_log() if r.get("logger") == "shipit.tree"]
    assert records
    # The ephemeral leaf IS the per-launch session id (ADR-0027) — bound as the
    # session key on every record of the birth.
    assert all(r.get("session") == "sess-1234" for r in records)


def test_create_binding_does_not_leak_past_return(tmp_path, monkeypatch, jsonl_log):
    """The Tree-birth bind is SCOPED to the pipeline: a record emitted after
    ``create`` returns must NOT inherit the created Tree/session, else the
    durable correlation of a later, unrelated Tree is silently corrupted."""
    _mock_write_boundary(monkeypatch)
    create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    logging.getLogger("shipit.tree").info("after create, unrelated to any tree")

    later = [
        r for r in jsonl_log() if r.get("msg") == "after create, unrelated to any tree"
    ]
    assert later, "the post-create record must be captured"
    assert all("tree" not in r and "session" not in r for r in later)


# --------------------------------------------------------------------------
# Record writes — the provisioning-commit record narrates its write
# --------------------------------------------------------------------------


def test_write_record_narrates_the_record_write(tmp_path, caplog):
    tree = tmp_path / "t"
    (tree / ".git").mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        provision.write_record(tree, [Sha("1" * 40), Sha("2" * 40)])

    assert any(
        r.levelno == logging.INFO and getattr(r, "tree", None) == str(tree)
        for r in caplog.records
    )


# --------------------------------------------------------------------------
# gc ladder — one decision record per Tree, carrying the Tree and its bucket
# --------------------------------------------------------------------------


def _tree_record(path: str, *, mtime: float, dirty: bool = False) -> TreeRecord:
    return TreeRecord(
        path=path,
        branch="issues/7/work",
        base="origin/main",
        dirty=dirty,
        ahead=0,
        behind=0,
        pr=None,
        mtime=mtime,
        unpushed_shas=(),
    )


def test_classify_records_one_decision_per_tree_with_its_bucket(caplog):
    now = 20 * 86_400.0  # past the 14-day default age boundary
    aged_merged = _tree_record("/trees/acme/widget/issues/7/one", mtime=0.0)
    fresh = _tree_record("/trees/acme/widget/issues/8/two", mtime=now)
    states = {aged_merged.path: "MERGED", fresh.path: None}

    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        decision = cleanup.classify([aged_merged, fresh], now, states)

    decisions = {(r.tree, r.bucket) for r in caplog.records if hasattr(r, "bucket")}
    # One decision record per Tree, agreeing with the returned partition.
    assert decisions == {
        (aged_merged.path, "removable"),
        (fresh.path, "keep"),
    }
    assert [r.path for r in decision.removable] == [aged_merged.path]
    assert [r.path for r in decision.keep] == [fresh.path]


def test_classify_partition_is_unchanged_by_logging(caplog):
    """The decision record is the ONLY side effect — same inputs, same partition,
    with or without a capturing handler (mirrors the prstate precedent)."""
    now = 20 * 86_400.0  # past the 14-day default age boundary
    record = _tree_record("/trees/acme/widget/issues/7/one", mtime=0.0)
    states = {record.path: "MERGED"}

    quiet = cleanup.classify([record], now, states)
    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        captured = cleanup.classify([record], now, states)

    assert quiet == captured


# --------------------------------------------------------------------------
# Cleanup and removal — the reclaim funnel and the sweep narrate themselves
# --------------------------------------------------------------------------


def test_remove_tree_records_the_removal(tmp_path, caplog):
    leaf = tmp_path / "t"
    (leaf / ".git").mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        assert remove_tree(leaf)

    assert any(
        r.levelno == logging.INFO and getattr(r, "tree", None) == str(leaf)
        for r in caplog.records
    )


def test_remove_tree_noop_on_a_missing_path_records_nothing(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        assert not remove_tree(tmp_path / "absent")
    assert not caplog.records


def test_gc_sweep_logs_milestone_and_incomplete_view_warning(tmp_path, caplog):
    leaf = tmp_path / "gone"
    (leaf / ".git").mkdir(parents=True)
    plan = gc_mod.GcPlan(
        partition=cleanup.Cleanup(
            removable=[_tree_record(str(leaf), mtime=0.0)], stale=[], keep=[]
        ),
        total=3,
        unknown=1,
    )

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        gc_mod.sweep(plan)

    # The sweep milestone at INFO (the summary record, distinct from the
    # per-Tree removal records which carry a `tree` field).
    assert any(
        r.levelno == logging.INFO and not hasattr(r, "tree") for r in caplog.records
    )
    # The per-Tree removal narrated through the funnel.
    assert any(getattr(r, "tree", None) == str(leaf) for r in caplog.records)
    # The incomplete-view (UNKNOWN PR states) degraded outcome at WARNING.
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_gc_sweep_failure_is_a_warning_with_the_exception_and_continues(
    tmp_path, caplog
):
    record = _tree_record(str(tmp_path / "stuck"), mtime=0.0)
    plan = gc_mod.GcPlan(
        partition=cleanup.Cleanup(removable=[record], stale=[], keep=[]),
        total=1,
        unknown=0,
    )

    def boom(path):
        raise OSError("locked")

    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        gc_mod.sweep(plan, remove=boom)

    # Degraded-but-continuing: WARNING, exception attached, Tree identified.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(r.exc_info and getattr(r, "tree", None) == record.path for r in warnings)


def test_remove_verb_failure_is_an_error_with_the_exception(
    tmp_path, monkeypatch, caplog
):
    leaf = tmp_path / "trees" / "acme" / "widget" / "issues" / "7" / "leaf"
    (leaf / ".git").mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(tmp_path / "trees"))
    monkeypatch.setattr(
        removal_mod,
        "remove_tree",
        lambda path: (_ for _ in ()).throw(OSError("locked")),
    )
    monkeypatch.setattr(
        registry_mod,
        "scan",
        lambda root: [_tree_record(str(leaf), mtime=0.0)],
    )

    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        rc = tree_verb.run_remove(str(leaf), assume_yes=True)

    assert rc == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(r.exc_info and getattr(r, "tree", None) == str(leaf) for r in errors)


def test_create_verb_prepipeline_failure_is_an_error_with_the_exception(
    monkeypatch, caplog
):
    """A failure `create`'s own rollback record never sees (e.g. a rejected spec)
    still lands an ERROR record at the verb boundary — the stderr print is not
    the only record of the failed action."""
    monkeypatch.setattr(tree_verb.git, "repo_root", lambda: "/checkout")
    # Identity derives LOCALLY from the origin remote (ADR-0024): the patched
    # remote URL is what identity.resolve_repo parses into the canonical Repo.
    monkeypatch.setattr(
        tree_verb.git, "remote_url", lambda **k: "git@example:acme/widget"
    )
    monkeypatch.setattr(
        tree_verb,
        "create",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad spec")),
    )

    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


# --------------------------------------------------------------------------
# Read-only (reviewer) Trees — creation and shared reuse are narrated
# --------------------------------------------------------------------------


def _mock_readonly_boundary(monkeypatch):
    def fake_clone(url, dest, *, reference):
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(git, "checkout", lambda *a, **k: None)
    monkeypatch.setattr(git, "reset_hard", lambda *a, **k: None)


def test_readonly_create_and_reuse_are_info_milestones_with_the_tree(
    tmp_path, monkeypatch, caplog
):
    _mock_readonly_boundary(monkeypatch)
    plan = readonly_plan(repo=_REPO, branch="feat/x", root=tmp_path / "trees")

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_readonly(plan, source_repo="/ref", github_url="url")
    fresh = [r for r in caplog.records if getattr(r, "tree", None) == str(plan.dir)]
    # Fresh creation: an INFO milestone carrying the Tree and its duration.
    assert any(
        r.levelno == logging.INFO and isinstance(getattr(r, "duration_ms", None), int)
        for r in fresh
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_readonly(plan, source_repo="/ref", github_url="url")
    # Shared reuse (the second reviewer): an INFO milestone carrying the Tree AND
    # its refresh cost as the structured `duration_ms` field (not only in the
    # message text), so reuse cost is queryable like the fresh-creation record.
    assert any(
        r.levelno == logging.INFO
        and getattr(r, "tree", None) == str(plan.dir)
        and isinstance(getattr(r, "duration_ms", None), int)
        for r in caplog.records
    )


def test_create_milestone_is_the_tree_created_event(tmp_path, monkeypatch, jsonl_log):
    """The birth milestone IS the `tree.created` dev-cycle event (LOG04-WS02 /
    ADR-0032): the same INFO record, tagged with the durable `event` field and
    carrying the scoped tree/session keys — exactly one birth per create."""
    _mock_write_boundary(monkeypatch)

    tree = create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    tagged = [r for r in jsonl_log() if r.get("event") == "tree.created"]
    assert len(tagged) == 1
    record = tagged[0]
    assert record["level"] == "info"
    assert record["tree"] == tree.path
    assert record["session"] == "work"
    assert isinstance(record["duration_ms"], int)
    # No other event name was minted by the creation pipeline.
    assert {r["event"] for r in jsonl_log() if r.get("event")} == {"tree.created"}
