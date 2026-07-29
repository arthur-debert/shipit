from __future__ import annotations

import pytest

from shipit import gh
from shipit.tree import gc, registry
from shipit.tree.cleanup import IDLE_THRESHOLD_SECONDS, Cleanup
from shipit.tree.registry import TreeRecord

AGED_NOW = 20 * 86_400.0


def _record(**over) -> TreeRecord:
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        mtime=0.0,
        unpushed_shas=(),
        newest_mtime=0.0,
    )
    base.update(over)
    base.setdefault("last_commit", base["newest_mtime"])
    return TreeRecord(**base)


def test_plan_partitions_the_fleet():
    removable = _record(path="/t/1")
    keep_dirty = _record(path="/t/2", dirty=True)
    keep_active = _record(path="/t/3", newest_mtime=AGED_NOW - 60)

    plan = gc.plan([removable, keep_dirty, keep_active], now=AGED_NOW)

    assert [r.path for r in plan.partition.removable] == ["/t/1"]
    assert {r.path for r in plan.partition.keep} == {"/t/2", "/t/3"}
    assert plan.total == 3
    assert plan.unexamined == 0


def test_unexamined_counts_the_signals_that_actually_suppress_a_removal():
    walk_failed = _record(path="/t/1", newest_mtime=None)
    rev_list_failed = _record(path="/t/2", unpushed_shas=None)
    stamp_failed = _record(path="/t/4", last_commit=None)
    judged = _record(path="/t/3")

    plan = gc.plan([walk_failed, rev_list_failed, stamp_failed, judged], now=AGED_NOW)

    assert plan.unexamined == 3
    assert plan.judged == 1
    assert plan.incomplete is True
    assert {r.path for r in plan.partition.keep} == {"/t/1", "/t/2", "/t/4"}
    assert [r.path for r in plan.partition.removable] == ["/t/3"]


def test_a_definite_keep_is_examined_even_if_another_signal_is_unreadable():
    plan = gc.plan(
        [_record(path="/t/1", dirty=True, newest_mtime=None)],
        now=AGED_NOW,
    )

    assert plan.unexamined == 0
    assert plan.incomplete is False


def test_plan_threshold_overrides_the_idle_boundary():
    record = _record(newest_mtime=0.0)
    idle_only_for_a_short_threshold = gc.plan(
        [record],
        now=3_600.0 * 2,
        idle_threshold_seconds=3_600.0,
    )
    kept_by_default = gc.plan([record], now=3_600.0 * 2)

    assert [r.path for r in idle_only_for_a_short_threshold.partition.removable] == [
        record.path
    ]
    assert [r.path for r in kept_by_default.partition.keep] == [record.path]


def test_plan_empty_fleet_is_a_valid_plan():
    plan = gc.plan([], now=AGED_NOW)
    assert plan == gc.GcPlan(
        partition=Cleanup(removable=[], keep=[]), total=0, unexamined=0
    )


def _clone(root, rel: str):
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _plan_of(partition: Cleanup, *, total: int | None = None, unexamined: int = 0):
    buckets = len(partition.removable) + len(partition.keep)
    return gc.GcPlan(
        partition=partition,
        total=total if total is not None else buckets,
        unexamined=unexamined,
    )


def test_sweep_removes_only_the_removable_bucket(tmp_path):
    removable = _clone(tmp_path, "issues/1/work-idle")
    keep = _clone(tmp_path, "issues/3/work-active")
    plan = _plan_of(
        Cleanup(
            removable=[_record(path=str(removable))],
            keep=[_record(path=str(keep))],
        )
    )

    result = gc.sweep(plan)

    assert not removable.exists()
    assert keep.exists()
    assert result.removed == (str(removable),)
    assert result.kept == 1
    assert result.failed == ()


def test_sweep_continues_past_a_failed_delete(tmp_path):
    bad = _clone(tmp_path, "issues/1/work-bad")
    good = _clone(tmp_path, "issues/2/work-good")
    plan = _plan_of(
        Cleanup(removable=[_record(path=str(bad)), _record(path=str(good))], keep=[])
    )
    from shipit.tree.readonly import remove_tree

    def flaky(path):
        if path == str(bad):
            raise OSError("read-only file")
        return remove_tree(path)

    result = gc.sweep(plan, remove=flaky)

    assert bad.exists()
    assert not good.exists()
    assert result.removed == (str(good),)
    assert result.failed == (gc.GcFailure(path=str(bad), error="read-only file"),)


def test_sweep_does_not_count_an_already_gone_tree(tmp_path):
    present = _clone(tmp_path, "issues/1/work-present")
    gone = tmp_path / "issues/2/work-gone"
    plan = _plan_of(
        Cleanup(
            removable=[_record(path=str(present)), _record(path=str(gone))], keep=[]
        )
    )

    result = gc.sweep(plan)

    assert result.removed == (str(present),)
    assert result.failed == ()


def test_sweep_carries_the_plan_counts_through():
    plan = _plan_of(Cleanup(removable=[], keep=[]), total=5, unexamined=2)
    result = gc.sweep(plan)
    assert result.total == 5
    assert result.unexamined == 2
    assert result.judged == 3
    assert result.incomplete is True


def test_sweep_announces_each_path_as_it_comes_off_disk(tmp_path):
    first = _clone(tmp_path, "issues/1/work-a")
    second = _clone(tmp_path, "issues/2/work-b")
    plan = _plan_of(
        Cleanup(
            removable=[_record(path=str(first)), _record(path=str(second))], keep=[]
        )
    )
    disk_at_announce: list[tuple[str, bool, bool]] = []

    def sink(path: str) -> None:
        disk_at_announce.append((path, first.exists(), second.exists()))

    result = gc.sweep(plan, on_removed=sink)

    assert disk_at_announce == [
        (str(first), False, True),
        (str(second), False, False),
    ]
    assert result.removed == (str(first), str(second))


def test_interrupted_sweep_still_announced_what_it_destroyed(tmp_path):
    doomed = _clone(tmp_path, "issues/1/work-doomed")
    interrupted_at = _clone(tmp_path, "issues/2/work-interrupted")
    never_reached = _clone(tmp_path, "issues/3/work-never")
    plan = _plan_of(
        Cleanup(
            removable=[
                _record(path=str(doomed)),
                _record(path=str(interrupted_at)),
                _record(path=str(never_reached)),
            ],
            keep=[],
        )
    )
    announced: list[str] = []
    from shipit.tree.readonly import remove_tree

    def killed_mid_sweep(path):
        if path == str(interrupted_at):
            raise KeyboardInterrupt
        return remove_tree(path)

    with pytest.raises(KeyboardInterrupt):
        gc.sweep(plan, remove=killed_mid_sweep, on_removed=announced.append)

    assert announced == [str(doomed)]
    assert not doomed.exists()
    assert never_reached.exists()


def test_sweep_announces_only_what_actually_came_off_disk(tmp_path):
    failed = _clone(tmp_path, "issues/1/work-failed")
    gone = tmp_path / "issues/2/work-gone"
    good = _clone(tmp_path, "issues/3/work-good")
    plan = _plan_of(
        Cleanup(
            removable=[
                _record(path=str(failed)),
                _record(path=str(gone)),
                _record(path=str(good)),
            ],
            keep=[],
        )
    )
    from shipit.tree.readonly import remove_tree

    def flaky(path):
        if path == str(failed):
            raise OSError("read-only file")
        return remove_tree(path)

    announced: list[str] = []
    result = gc.sweep(plan, remove=flaky, on_removed=announced.append)

    assert announced == [str(good)] == list(result.removed)


def test_sweep_without_a_sink_is_unchanged(tmp_path):
    removable = _clone(tmp_path, "issues/1/work-idle")
    plan = _plan_of(Cleanup(removable=[_record(path=str(removable))], keep=[]))

    result = gc.sweep(plan)

    assert result.removed == (str(removable),)
    assert not removable.exists()


def test_incomplete_is_the_unexamined_count_on_both_plan_and_result():
    partial = gc.plan(
        [_record(path="/t/1"), _record(path="/t/2", newest_mtime=None)],
        now=AGED_NOW,
    )
    whole = gc.plan([_record(path="/t/1")], now=AGED_NOW)

    assert partial.incomplete is True
    assert partial.judged == 1
    assert whole.incomplete is False
    assert gc.sweep(_plan_of(whole.partition, total=1)).incomplete is False


def test_plan_fleet_composes_scan_and_classify(monkeypatch):
    import time as _time

    now = _time.time()
    records = [
        _record(path="/t/idle", branch="b1", newest_mtime=now - (49 * 3_600)),
        _record(path="/t/active", branch="b2", newest_mtime=now - 60),
    ]
    monkeypatch.setattr(registry, "scan", lambda root: records)

    plan = gc.plan_fleet("/trees")

    assert [r.path for r in plan.partition.removable] == ["/t/idle"]
    assert [r.path for r in plan.partition.keep] == ["/t/active"]
    assert plan.total == 2 and plan.unexamined == 0


def test_plan_fleet_keeps_a_tree_someone_is_working_in_whatever_its_kind(monkeypatch):
    import time as _time

    now = _time.time()
    live_paths = [
        "/trees/acme/widget/ephemeral/sess-live",
        "/trees/acme/widget/review/tre03-ws03",
        "/trees/acme/widget/issues/7/work-aaaa",
    ]
    records = [
        _record(path=path, branch="b", newest_mtime=now - 60) for path in live_paths
    ]
    monkeypatch.setattr(registry, "scan", lambda root: records)

    plan = gc.plan_fleet("/trees")

    assert plan.partition.removable == []
    assert {r.path for r in plan.partition.keep} == set(live_paths)


def test_plan_fleet_threshold_defaults_to_48h(monkeypatch):
    import time as _time

    now = _time.time()
    records = [
        _record(path="/t/just-under", newest_mtime=now - (IDLE_THRESHOLD_SECONDS - 60)),
        _record(path="/t/just-over", newest_mtime=now - (IDLE_THRESHOLD_SECONDS + 60)),
    ]
    monkeypatch.setattr(registry, "scan", lambda root: records)

    plan = gc.plan_fleet("/trees")

    assert [r.path for r in plan.partition.removable] == ["/t/just-over"]
    assert [r.path for r in plan.partition.keep] == ["/t/just-under"]


def _git(cwd, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_gc_makes_zero_network_calls(tmp_path, monkeypatch):
    def _explode(*_a, **_k):
        raise AssertionError("gc made a network (gh) call")

    for name in dir(gh):
        obj = getattr(gh, name)
        if callable(obj) and not isinstance(obj, type) and not name.startswith("__"):
            monkeypatch.setattr(gh, name, _explode, raising=False)

    clone = tmp_path / "acme" / "widget" / "issues" / "7" / "work-aaaa"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    (clone / "f.txt").write_text("x")
    _git(clone, "add", ".")
    _git(clone, "commit", "-qm", "init")

    plan = gc.plan_fleet(str(tmp_path))

    assert plan.total == 1
