from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from shipit import execrun, git, logsetup
from shipit.identity import repo_from_slug
from shipit.tree import cleanup
from shipit.tree import gc as gc_mod
from shipit.tree import registry as registry_mod
from shipit.tree import removal as removal_mod
from shipit.tree.create import create
from shipit.tree.layout import TreeSpec
from shipit.tree.readonly import create_readonly, readonly_plan, remove_tree
from shipit.tree.registry import TreeRecord
from shipit.verbs import tree as tree_verb

_REPO = repo_from_slug("acme/widget")

_AGENT = "claude"
_CREATED = "20260717-081333"
_TREE_ID = "619cf51a-f501-44dc-992f-74df773204aa"

_PINNED = f'[shipit]\nversion = "{"5eed" * 10}"\n\n[managed]\n'


@pytest.fixture(autouse=True)
def _reset_package_logger():
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

    def fake_clone(url, dest, *, reference):
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()
        (d / ".shipit.toml").write_text(_PINNED)
        (d / "pixi.toml").write_text("# stub\n")

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(git, "checkout_create_or_reset", lambda *a, **k: None)
    monkeypatch.setattr(git, "submodule_update_init", lambda **k: None)
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
        agent=_AGENT,
        created=_CREATED,
        tree_id=_TREE_ID,
        issue=7,
        root=tmp_path / "trees",
    )


def _source(tmp_path: Path) -> str:
    src = tmp_path / "src"
    src.mkdir()
    return str(src)


def test_create_records_carry_tree_and_session_keys_and_durations(
    tmp_path, monkeypatch, jsonl_log
):
    _mock_write_boundary(monkeypatch)

    tree = create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    records = [r for r in jsonl_log() if r.get("logger") == "shipit.tree"]
    assert records, "the creation pipeline must narrate on the tree logger"
    for record in records:
        assert {"ts", "level", "logger", "msg"} <= record.keys()
    assert all(r.get("tree") == tree.path for r in records)
    assert all(r.get("session") == "work" for r in records)
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
        "checkout_create_or_reset",
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
    assert any("exception" in r and r.get("tree") for r in errors)


def test_ephemeral_shape_binds_its_session_id(tmp_path, monkeypatch, jsonl_log):
    _mock_write_boundary(monkeypatch)
    spec = TreeSpec(
        repo=_REPO,
        agent=_AGENT,
        created=_CREATED,
        tree_id=_TREE_ID,
        ephemeral="sess-1234",
        root=tmp_path / "trees",
    )

    create(spec, source_repo=_source(tmp_path), github_url="url")

    records = [r for r in jsonl_log() if r.get("logger") == "shipit.tree"]
    assert records
    assert all(r.get("session") == "sess-1234" for r in records)


def test_create_binding_does_not_leak_past_return(tmp_path, monkeypatch, jsonl_log):
    _mock_write_boundary(monkeypatch)
    create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    logging.getLogger("shipit.tree").info("after create, unrelated to any tree")

    later = [
        r for r in jsonl_log() if r.get("msg") == "after create, unrelated to any tree"
    ]
    assert later, "the post-create record must be captured"
    assert all("tree" not in r and "session" not in r for r in later)


def _tree_record(path: str, *, mtime: float, dirty: bool = False) -> TreeRecord:
    return TreeRecord(
        path=path,
        branch="issues/7/work",
        base="origin/main",
        dirty=dirty,
        ahead=0,
        behind=0,
        mtime=mtime,
        unpushed_shas=(),
        newest_mtime=mtime,
        last_commit=mtime,
    )


def test_classify_records_one_decision_per_tree_with_its_bucket(caplog):
    now = 20 * 86_400.0
    idle = _tree_record("/trees/acme/widget/issues/7/one", mtime=0.0)
    fresh = _tree_record("/trees/acme/widget/issues/8/two", mtime=now)

    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        decision = cleanup.classify([idle, fresh], now)

    decisions = {(r.tree, r.bucket) for r in caplog.records if hasattr(r, "bucket")}
    assert decisions == {
        (idle.path, "removable"),
        (fresh.path, "keep"),
    }
    assert [r.path for r in decision.removable] == [idle.path]
    assert [r.path for r in decision.keep] == [fresh.path]


def test_classify_partition_is_unchanged_by_logging(caplog):
    now = 20 * 86_400.0
    record = _tree_record("/trees/acme/widget/issues/7/one", mtime=0.0)

    quiet = cleanup.classify([record], now)
    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        captured = cleanup.classify([record], now)

    assert quiet == captured


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
            removable=[_tree_record(str(leaf), mtime=0.0)], keep=[]
        ),
        total=3,
        unexamined=1,
    )

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        gc_mod.sweep(plan)

    assert any(
        r.levelno == logging.INFO and not hasattr(r, "tree") for r in caplog.records
    )
    assert any(getattr(r, "tree", None) == str(leaf) for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_gc_sweep_failure_is_a_warning_with_the_exception_and_continues(
    tmp_path, caplog
):
    record = _tree_record(str(tmp_path / "stuck"), mtime=0.0)
    plan = gc_mod.GcPlan(
        partition=cleanup.Cleanup(removable=[record], keep=[]),
        total=1,
        unexamined=0,
    )

    def boom(path):
        raise OSError("locked")

    with caplog.at_level(logging.DEBUG, logger="shipit.tree"):
        gc_mod.sweep(plan, remove=boom)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(r.exc_info and getattr(r, "tree", None) == record.path for r in warnings)


def test_remove_verb_failure_is_an_error_with_the_exception(
    tmp_path, monkeypatch, caplog
):
    leaf = tmp_path / "trees" / f"widget-{_AGENT}-{_CREATED}-{_TREE_ID}"
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
    monkeypatch.setattr(tree_verb.git, "repo_root", lambda: "/checkout")
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


def _mock_readonly_boundary(monkeypatch):
    def fake_clone(url, dest, *, reference):
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(git, "checkout", lambda *a, **k: None)
    monkeypatch.setattr(git, "submodule_update_init", lambda **k: None)


def _readonly_plan(tmp_path, *, tree_id):
    return readonly_plan(
        repo=_REPO,
        branch="feat/x",
        agent=_AGENT,
        created=_CREATED,
        tree_id=tree_id,
        root=tmp_path / "trees",
    )


def test_readonly_per_run_creations_are_info_milestones_with_the_tree(
    tmp_path, monkeypatch, caplog
):
    _mock_readonly_boundary(monkeypatch)
    first = _readonly_plan(tmp_path, tree_id="11111111-1111-4111-8111-111111111111")
    second = _readonly_plan(tmp_path, tree_id="22222222-2222-4222-8222-222222222222")
    assert first.dir != second.dir

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_readonly(first, source_repo="/ref", github_url="url")
    fresh = [r for r in caplog.records if getattr(r, "tree", None) == str(first.dir)]
    assert any(
        r.levelno == logging.INFO and isinstance(getattr(r, "duration_ms", None), int)
        for r in fresh
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_readonly(second, source_repo="/ref", github_url="url")
    assert any(
        r.levelno == logging.INFO
        and getattr(r, "tree", None) == str(second.dir)
        and isinstance(getattr(r, "duration_ms", None), int)
        for r in caplog.records
    )


def test_create_milestone_is_the_tree_created_event(tmp_path, monkeypatch, jsonl_log):
    _mock_write_boundary(monkeypatch)

    tree = create(_spec(tmp_path), source_repo=_source(tmp_path), github_url="url")

    tagged = [r for r in jsonl_log() if r.get("event") == "tree.created"]
    assert len(tagged) == 1
    record = tagged[0]
    assert record["level"] == "info"
    assert record["tree"] == tree.path
    assert record["session"] == "work"
    assert isinstance(record["duration_ms"], int)
    assert {r["event"] for r in jsonl_log() if r.get("event")} == {"tree.created"}
