"""gc as a plan + a sweep (CLI02-WS03, ADR-0030).

Typed tests for :mod:`shipit.tree.gc` — the promoted domain half of
``shipit tree gc``:

- :func:`~shipit.tree.gc.plan` is PURE — the partition and the incomplete-view
  counts are asserted as values, no fleet on disk;
- :func:`~shipit.tree.gc.sweep` is the effectful apply — driven against tmp
  clones (and an injected ``remove`` for the failure paths), asserting on the
  typed :class:`~shipit.tree.gc.GcResult` instead of captured stdout, and on
  the ``on_removed`` sink for the streamed audit trail (#1011), which is
  captured as a list here: the domain prints nothing, so the sink is a value
  like any other;
- :func:`~shipit.tree.gc.plan_fleet` is the gather — its one boundary read
  (:func:`~shipit.tree.registry.scan`) is patched at its seam. Since ADR-0072
  the rule reads nothing but the scanned record, so there is nothing else to
  patch: no PR read, no liveness probe, no provisioning record.
"""

from __future__ import annotations

import pytest

from shipit import gh
from shipit.tree import gc, registry
from shipit.tree.cleanup import IDLE_THRESHOLD_SECONDS, Cleanup
from shipit.tree.registry import TreeRecord

#: A `now` far past the 48h default boundary for `newest_mtime=0.0` records.
AGED_NOW = 20 * 86_400.0


def _record(**over) -> TreeRecord:
    # The removable baseline: clean, every commit on some remote (`unpushed_shas=()`)
    # and idle since the epoch. Both TreeRecord unreadable defaults (`unpushed_shas`
    # and `newest_mtime` = None) read as KEEP, so a removable row must pin them.
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr=None,
        pr_state=None,
        mtime=0.0,
        unpushed_shas=(),
        newest_mtime=0.0,
    )
    base.update(over)
    return TreeRecord(**base)


# --- plan: the pure decision -------------------------------------------------------


def test_plan_partitions_the_fleet():
    removable = _record(path="/t/1")
    keep_dirty = _record(path="/t/2", dirty=True)
    keep_active = _record(path="/t/3", newest_mtime=AGED_NOW - 60)

    plan = gc.plan([removable, keep_dirty, keep_active], now=AGED_NOW)

    assert [r.path for r in plan.partition.removable] == ["/t/1"]
    assert {r.path for r in plan.partition.keep} == {"/t/2", "/t/3"}
    assert plan.total == 3
    assert plan.unexamined == 0


def test_an_unknown_pr_state_is_not_reported_as_skipped_because_it_is_not_skipped():
    # The regression that repointed the count. PR state stopped deciding anything
    # (ADR-0072), so an UNKNOWN Tree is bucketed on its activity like every other —
    # here, removable. Counting it as "unexamined" would have the report describe a
    # Tree this very run DELETES: `INCOMPLETE - 1 of 2 skipped; removed 2, kept 0`.
    # A destructive command's audit trail may not contradict the destruction.
    plan = gc.plan(
        [
            _record(path="/t/1", pr_state="MERGED"),
            _record(path="/t/2", pr_state="UNKNOWN"),
        ],
        now=AGED_NOW,
    )

    assert {r.path for r in plan.partition.removable} == {"/t/1", "/t/2"}
    assert plan.unexamined == 0
    assert plan.incomplete is False


def test_unexamined_counts_the_signals_that_actually_suppress_a_removal():
    # #1012's property, repointed: the count names Trees kept because a signal could
    # not be READ, which is now the unpushed list and the activity walk. Each is a
    # silent conservative keep, and a fleet-wide failure of either would keep
    # everything while reporting `removed 0` — the #1011 shape, on the new signals.
    walk_failed = _record(path="/t/1", newest_mtime=None)
    rev_list_failed = _record(path="/t/2", unpushed_shas=None)
    judged = _record(path="/t/3")

    plan = gc.plan([walk_failed, rev_list_failed, judged], now=AGED_NOW)

    assert plan.unexamined == 2
    assert plan.judged == 1
    assert plan.incomplete is True
    # The invariant that makes the report honest: unexamined is a subset of `keep`,
    # so a counted Tree can never be one the same run removed.
    assert {r.path for r in plan.partition.keep} == {"/t/1", "/t/2"}
    assert [r.path for r in plan.partition.removable] == ["/t/3"]


def test_a_definite_keep_is_examined_even_if_another_signal_is_unreadable():
    # `unexamined` asks which signal DECIDED, in the rule's short-circuit order. A
    # dirty Tree is kept on positive evidence; the walk under it was never reached, so
    # its failure changes nothing and must not inflate the incomplete-view count into
    # crying wolf on every Tree with an unreadable corner.
    plan = gc.plan(
        [_record(path="/t/1", dirty=True, newest_mtime=None)],
        now=AGED_NOW,
    )

    assert plan.unexamined == 0
    assert plan.incomplete is False


def test_plan_threshold_overrides_the_idle_boundary():
    # `plan` threads idle_threshold_seconds down to `classify` — the ONE boundary.
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
    # 2h idle is well inside the 48h default.
    assert [r.path for r in kept_by_default.partition.keep] == [record.path]


def test_plan_empty_fleet_is_a_valid_plan():
    plan = gc.plan([], now=AGED_NOW)
    assert plan == gc.GcPlan(
        partition=Cleanup(removable=[], keep=[]), total=0, unexamined=0
    )


# --- sweep: the effectful apply ------------------------------------------------------


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

    assert bad.exists()  # the failed delete left it on disk
    assert not good.exists()  # the sweep continued and reclaimed the next one
    assert result.removed == (str(good),)
    assert result.failed == (gc.GcFailure(path=str(bad), error="read-only file"),)


def test_sweep_does_not_count_an_already_gone_tree(tmp_path):
    # A removable Tree whose directory is ALREADY gone (a concurrent sweep, a
    # manual rm) is neither counted nor reported: `removed` reflects what came
    # off disk, not what was merely planned.
    present = _clone(tmp_path, "issues/1/work-present")
    gone = tmp_path / "issues/2/work-gone"  # never created on disk
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


# --- sweep: streaming the destroyed set (#1011) --------------------------------------


def test_sweep_announces_each_path_as_it_comes_off_disk(tmp_path):
    # The sink fires DURING the sweep, not after it: at the moment each path is
    # announced, that Tree is already gone from disk and the later ones are not.
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
        (str(first), False, True),  # announced with the second Tree still standing
        (str(second), False, False),
    ]
    assert result.removed == (str(first), str(second))  # the typed result is intact


def test_interrupted_sweep_still_announced_what_it_destroyed(tmp_path):
    # THE regression (#1011): a sweep killed mid-fleet (a timeout, the Ctrl-C a
    # silent multi-minute delete invites) took its GcResult with it and left no
    # record of the Trees it had already destroyed. The sink is that record, so
    # it must survive the exception that eats the return value.
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

    # No GcResult came back at all — and the destroyed Tree is still named.
    assert announced == [str(doomed)]
    assert not doomed.exists()
    assert never_reached.exists()


def test_sweep_announces_only_what_actually_came_off_disk(tmp_path):
    # The sink mirrors `removed` exactly: a failed delete and an already-gone Tree
    # are not announced, because the audit trail must not claim a Tree it did not
    # destroy.
    failed = _clone(tmp_path, "issues/1/work-failed")
    gone = tmp_path / "issues/2/work-gone"  # never created on disk
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
    # `on_removed` is optional: the domain has no default sink to print through.
    removable = _clone(tmp_path, "issues/1/work-idle")
    plan = _plan_of(Cleanup(removable=[_record(path=str(removable))], keep=[]))

    result = gc.sweep(plan)

    assert result.removed == (str(removable),)
    assert not removable.exists()


# --- the incomplete-view predicate ---------------------------------------------------


def test_incomplete_is_the_unexamined_count_on_both_plan_and_result():
    # One predicate, shared by the two gc tails: any Tree kept on an unreadable signal
    # means the fleet was only partly judged, whatever the removable count says. It
    # reports on the run's COVERAGE; it decides no Tree.
    partial = gc.plan(
        [_record(path="/t/1"), _record(path="/t/2", newest_mtime=None)],
        now=AGED_NOW,
    )
    whole = gc.plan([_record(path="/t/1")], now=AGED_NOW)

    assert partial.incomplete is True
    assert partial.judged == 1
    assert whole.incomplete is False
    assert gc.sweep(_plan_of(whole.partition, total=1)).incomplete is False


# --- pr_state: the projection off the scanned record ---------------------------------


def test_pr_state_projects_the_records_state_without_reading_gh(monkeypatch):
    # #1011: pr_state makes NO call of its own — it reports the state the scan already
    # read (one call per repo). Any gh access here would be the second per-Tree
    # fan-out that exhausted the GraphQL budget mid-sweep, so make it fatal.
    monkeypatch.delattr(gh, "pr_for_head")

    # The vocabulary is the registry's: a draft open PR reads "DRAFT", not "OPEN".
    assert gc.pr_state(_record(path="/trees/x", pr_state="DRAFT")) == "DRAFT"
    assert gc.pr_state(_record(path="/trees/y", pr_state="MERGED")) == "MERGED"


def test_pr_state_unknown_stays_distinct_from_no_pr():
    # The projection still reports the split it always did — "UNKNOWN" (state
    # unreadable) never collapses into None (no branch / no PR). Nothing in gc acts on
    # either any more: this whole function is dead, kept only so WS02's behaviour
    # change and WS03's pure deletion stay separately reviewable.
    assert gc.pr_state(_record(path="/trees/x", pr_state="UNKNOWN")) == "UNKNOWN"
    assert gc.pr_state(_record(path="/trees/y", branch=None, pr_state=None)) is None
    assert gc.pr_state(_record(path="/trees/z", pr_state=None)) is None


# --- plan_fleet: the effectful gather -------------------------------------------------


def test_plan_fleet_composes_scan_and_classify(monkeypatch):
    # Everything the rule needs rides the records the scan returns — the activity
    # signal included — so the gather adds no per-Tree reads of its own.
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
    # The #1018 shape at the gather: an ephemeral session Tree, clean, no PR, whose
    # only sign of life is a file written a minute ago. It must survive the sweep — and
    # a review/write Tree in the same state must too: kind is not a decision input
    # (ADR-0072).
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


# --- the dead gather helpers (no caller since ADR-0072; deleted in WS03) --------------


def test_live_sessions_maps_only_ephemeral_trees():
    records = [_record(), _record(path="/trees/acme/widget/review/x", branch="b")]
    assert gc.live_sessions(records) == {}


def test_provision_shas_maps_only_ephemeral_trees(tmp_path):
    write = _record()
    ephemeral = _record(path=str(tmp_path / "ephemeral" / "sess-1"))
    shas = gc.provision_shas([write, ephemeral])
    # The write Tree is absent; the ephemeral one reads the (missing) record as
    # the empty set — the safe direction.
    assert shas == {ephemeral.path: frozenset()}


def test_the_gather_no_longer_calls_the_dead_helpers(monkeypatch):
    # The behaviour half of "liveness and provisioning are retired" (ADR-0072): the
    # functions still exist (WS03 deletes them), but plan_fleet must not consult them —
    # the liveness probe's false-negatives are exactly what deleted a live Tree (#1018).
    def _fail(*args, **kwargs):
        raise AssertionError("the gc gather must not read liveness or provisioning")

    monkeypatch.setattr(gc, "live_sessions", _fail)
    monkeypatch.setattr(gc, "provision_shas", _fail)
    monkeypatch.setattr(registry, "scan", lambda root: [_record()])

    gc.plan_fleet("/trees")
