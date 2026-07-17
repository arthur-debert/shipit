"""gc as a plan + a sweep (CLI02-WS03, ADR-0030).

Typed tests for :mod:`shipit.tree.gc` — the promoted domain half of
``shipit tree gc``:

- :func:`~shipit.tree.gc.plan` is PURE — the partition and the incomplete-view
  counts are asserted as values, no fleet on disk;
- :func:`~shipit.tree.gc.sweep` is the effectful apply — driven against tmp
  clones (and an injected ``remove`` for the failure paths), asserting on the
  typed :class:`~shipit.tree.gc.GcResult` instead of captured stdout;
- :func:`~shipit.tree.gc.plan_fleet` is the gather — its boundary reads
  (scan / PR state / liveness / provisioning record) are patched at their one
  seam each.
"""

from __future__ import annotations

import json
import time as _time

from shipit import gh
from shipit.identity import Sha
from shipit.session import liveness
from shipit.tree import cleanup, gc, provision, registry
from shipit.tree.cleanup import Cleanup
from shipit.tree.registry import TreeRecord


def _plant_legacy_record(tree, shas: list[Sha]) -> None:
    """Plant the pre-ADR-0033 provision record a drift-window birth once wrote
    (the writer is retired; Trees born before the pin still carry these)."""
    provision.record_path(tree).write_text(
        json.dumps({"commits": [str(sha) for sha in shas]}), encoding="utf-8"
    )


def _record(**over) -> TreeRecord:
    # `unpushed_shas=()` (every commit on some remote), NOT the TreeRecord default
    # of None (list unreadable): classify's write/ephemeral ladders read None
    # conservatively as has-local-work and would KEEP every record.
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr=None,
        mtime=0.0,
        unpushed_shas=(),
    )
    base.update(over)
    return TreeRecord(**base)


def _head_pr(number: int, state: str, *, is_draft: bool = False) -> gh.HeadPr:
    # The typed pr_for_head hit (PROC03): gc only branches on number/state/
    # is_draft, so the base is a fixed placeholder.
    return gh.HeadPr(number=number, state=state, is_draft=is_draft, base_ref="main")


#: A `now` far past the 14-day default boundary for mtime=0.0 records.
AGED_NOW = 20 * 86_400.0


# --- plan: the pure decision -------------------------------------------------------


def test_plan_partitions_the_fleet():
    removable = _record(path="/t/1")
    stale = _record(path="/t/2")
    keep_dirty = _record(path="/t/3", dirty=True)
    keep_open = _record(path="/t/4")
    states = {"/t/1": "MERGED", "/t/2": None, "/t/3": "MERGED", "/t/4": "OPEN"}

    plan = gc.plan(
        [removable, stale, keep_dirty, keep_open], now=AGED_NOW, pr_states=states
    )

    assert [r.path for r in plan.partition.removable] == ["/t/1"]
    assert [r.path for r in plan.partition.stale] == ["/t/2"]
    assert {r.path for r in plan.partition.keep} == {"/t/3", "/t/4"}
    assert plan.total == 4
    assert plan.unknown == 0


def test_plan_counts_unknown_states_and_keeps_them_unremovable():
    readable = _record(path="/t/1")
    unreadable = _record(path="/t/2")
    states = {"/t/1": "MERGED", "/t/2": "UNKNOWN"}

    plan = gc.plan([readable, unreadable], now=AGED_NOW, pr_states=states)

    assert plan.unknown == 1
    assert plan.total == 2
    # The unreadable Tree is conservatively STALE — never in the removable set.
    assert [r.path for r in plan.partition.stale] == ["/t/2"]


def test_plan_threshold_overrides_the_age_boundary():
    # `plan` threads max_age_seconds down to `classify`. Probed on an UNMERGED (no PR)
    # Tree — the only shape the age boundary governs (#1009): a merged Tree is decided
    # before the gate, on its own grace window, which `plan` does NOT thread (mirroring
    # the ephemeral backstops).
    record = _record(mtime=0.0)
    aged_only_for_short_threshold = gc.plan(
        [record],
        now=3_600.0 * 2,
        pr_states={record.path: None},
        max_age_seconds=3_600.0,
    )
    kept_by_default = gc.plan([record], now=3_600.0 * 2, pr_states={record.path: None})

    assert [r.path for r in aged_only_for_short_threshold.partition.stale] == [
        record.path
    ]
    assert [r.path for r in kept_by_default.partition.keep] == [record.path]


def test_plan_empty_fleet_is_a_valid_plan():
    plan = gc.plan([], now=AGED_NOW, pr_states={})
    assert plan == gc.GcPlan(
        partition=Cleanup(removable=[], stale=[], keep=[]), total=0, unknown=0
    )


# --- sweep: the effectful apply ------------------------------------------------------


def _clone(root, rel: str):
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _plan_of(partition: Cleanup, *, total: int | None = None, unknown: int = 0):
    buckets = len(partition.removable) + len(partition.stale) + len(partition.keep)
    return gc.GcPlan(
        partition=partition,
        total=total if total is not None else buckets,
        unknown=unknown,
    )


def test_sweep_removes_only_the_removable_bucket(tmp_path):
    removable = _clone(tmp_path, "issues/1/work-merged")
    stale = _clone(tmp_path, "issues/2/work-orphan")
    keep = _clone(tmp_path, "issues/3/work-open")
    plan = _plan_of(
        Cleanup(
            removable=[_record(path=str(removable))],
            stale=[_record(path=str(stale))],
            keep=[_record(path=str(keep))],
        )
    )

    result = gc.sweep(plan)

    assert not removable.exists()
    assert stale.exists() and keep.exists()
    assert result.removed == (str(removable),)
    assert result.stale == (str(stale),)
    assert result.kept == 1
    assert result.failed == ()


def test_sweep_continues_past_a_failed_delete(tmp_path):
    bad = _clone(tmp_path, "issues/1/work-bad")
    good = _clone(tmp_path, "issues/2/work-good")
    plan = _plan_of(
        Cleanup(
            removable=[_record(path=str(bad)), _record(path=str(good))],
            stale=[],
            keep=[],
        )
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
            removable=[_record(path=str(present)), _record(path=str(gone))],
            stale=[],
            keep=[],
        )
    )

    result = gc.sweep(plan)

    assert result.removed == (str(present),)
    assert result.failed == ()


def test_sweep_carries_the_plan_counts_through():
    plan = _plan_of(Cleanup(removable=[], stale=[], keep=[]), total=5, unknown=2)
    result = gc.sweep(plan)
    assert result.total == 5
    assert result.unknown == 2
    assert result.swept == 3


# --- pr_state: the gh boundary read --------------------------------------------------


def test_pr_state_normalizes_draft(monkeypatch):
    # A draft open PR reads as "DRAFT" (one fleet-wide vocabulary, mirroring the
    # registry label), not the raw "OPEN" GitHub state.
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: _head_pr(7, "OPEN", is_draft=True),
    )
    assert gc.pr_state(_record(path="/trees/x", branch="b1")) == "DRAFT"


def test_pr_state_unknown_when_gh_state_unreadable(monkeypatch):
    # An unreadable PR state (gh.pr_for_head -> UNKNOWN) surfaces as the "UNKNOWN"
    # string, distinct from None (no branch / no PR), so gc can both treat it
    # conservatively and warn.
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: gh.UNKNOWN)
    assert gc.pr_state(_record(path="/trees/x", branch="b1")) == "UNKNOWN"


def test_pr_state_none_when_no_branch_or_no_pr(monkeypatch):
    # No branch -> None without even hitting gh; a branch with no PR -> None too.
    assert gc.pr_state(_record(path="/trees/x", branch=None)) is None
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)
    assert gc.pr_state(_record(path="/trees/y", branch="b1")) is None


# --- plan_fleet: the effectful gather -------------------------------------------------


def test_plan_fleet_composes_scan_states_and_classify(monkeypatch):
    records = [
        _record(path="/t/merged", branch="b1"),
        _record(path="/t/open", branch="b2"),
    ]
    pr_by_branch = {"b1": _head_pr(1, "MERGED"), "b2": _head_pr(2, "OPEN")}
    monkeypatch.setattr(registry, "scan", lambda root: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )

    plan = gc.plan_fleet("/trees")

    assert [r.path for r in plan.partition.removable] == ["/t/merged"]
    assert [r.path for r in plan.partition.keep] == ["/t/open"]
    assert plan.total == 2 and plan.unknown == 0


def _ephemeral_clone(root, leaf: str) -> str:
    tree = root / "acme" / "widget" / "ephemeral" / leaf
    (tree / ".git").mkdir(parents=True)
    return str(tree)


def test_plan_fleet_reads_session_liveness_for_ephemeral_trees(tmp_path, monkeypatch):
    # End to end through the gather: liveness comes from the pidfile + probe, and
    # the ephemeral ladder keeps the live session's Tree while reclaiming the dead
    # one (both clean, pushed, and past the grace window).
    root = tmp_path / "trees"
    live_path = _ephemeral_clone(root, "sess-live")
    dead_path = _ephemeral_clone(root, "sess-dead")
    created = 1_750_000_000.0
    liveness.write_pidfile(
        live_path, liveness.LivenessRecord(pid=100, session_id="a", create_time=created)
    )
    liveness.write_pidfile(
        dead_path, liveness.LivenessRecord(pid=200, session_id="b", create_time=created)
    )

    #: pid 100 is alive and IS the recorded claude session; pid 200 is gone.
    def probe(pid):
        if pid == 100:
            return liveness.ProcessInfo(
                pid=100,
                ppid=1,
                create_time=created,
                argv="node /x/claude-code/cli.js -w sess-live",
            )
        return None

    monkeypatch.setattr(liveness, "os_probe", probe)
    past_grace = _time.time() - (cleanup.EPHEMERAL_GRACE_SECONDS + 60)
    records = [
        _record(
            path=live_path, branch="ephemeral/sess-live", base=None, mtime=past_grace
        ),
        _record(
            path=dead_path, branch="ephemeral/sess-dead", base=None, mtime=past_grace
        ),
    ]
    monkeypatch.setattr(registry, "scan", lambda root: records)
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    plan = gc.plan_fleet(str(root))

    assert [r.path for r in plan.partition.keep] == [live_path]
    assert [r.path for r in plan.partition.removable] == [dead_path]


def test_plan_fleet_excludes_the_recorded_provisioning_commit(tmp_path, monkeypatch):
    # End to end through the gather (#232): two dead, clean ephemeral Trees past
    # the grace window, each carrying ONE local-only commit (the drift-window
    # managed-set reconcile). The one whose provisioning RECORDED that commit's
    # SHA is reclaimable; the one without a record keeps — the exclusion is
    # exact-identity, never a guess.
    root = tmp_path / "trees"
    recorded_path = _ephemeral_clone(root, "sess-recorded")
    unrecorded_path = _ephemeral_clone(root, "sess-unrecorded")
    sha = Sha("a" * 40)
    _plant_legacy_record(recorded_path, [sha])

    past_grace = _time.time() - (cleanup.EPHEMERAL_GRACE_SECONDS + 60)
    records = [
        _record(
            path=recorded_path,
            branch="ephemeral/sess-recorded",
            base=None,
            unpushed_shas=(sha,),
            mtime=past_grace,
        ),
        _record(
            path=unrecorded_path,
            branch="ephemeral/sess-unrecorded",
            base=None,
            unpushed_shas=(sha,),
            mtime=past_grace,
        ),
    ]
    monkeypatch.setattr(registry, "scan", lambda root: records)
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    plan = gc.plan_fleet(str(root))

    assert [r.path for r in plan.partition.removable] == [recorded_path]
    assert [r.path for r in plan.partition.keep] == [unrecorded_path]


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
