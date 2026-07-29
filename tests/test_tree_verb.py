from __future__ import annotations

import io
import json
import sys
import time as _time

import pytest

from shipit import cli, execrun, gh, git
from shipit.execrun import ExecError
from shipit.identity import Sha, repo_from_slug
from shipit.tree import layout as layout_mod
from shipit.tree import registry as registry_mod
from shipit.tree.create import Tree
from shipit.tree.registry import TreeRecord
from shipit.verbs import tree as tree_verb


def test_run_create_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    monkeypatch.setattr(git, "remote_url", lambda *, cwd: "git@example:acme/widget")

    captured: dict = {}

    def fake_create(spec, *, source_repo, github_url):
        captured["spec"] = spec
        captured["source_repo"] = source_repo
        captured["github_url"] = github_url
        return Tree(path="/repo/trees/x", branch="issues/7/work", base="origin/main")

    monkeypatch.setattr(tree_verb, "create", fake_create)

    rc = tree_verb.run_create(issue=7, slug="Thing")

    assert rc == 0
    assert captured["spec"].repo == repo_from_slug("acme/widget")
    assert captured["spec"].issue == 7
    assert captured["spec"].slug == "Thing"
    assert captured["source_repo"] == "/repo"
    assert captured["github_url"] == "git@example:acme/widget"
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "READY"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload == {
        "path": "/repo/trees/x",
        "branch": "issues/7/work",
        "base": "origin/main",
    }


def _patch_identity(monkeypatch):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    monkeypatch.setattr(git, "remote_url", lambda *, cwd: "git@example:acme/widget")


def _capture_create(monkeypatch, tree: Tree | None = None) -> dict:
    captured: dict = {}
    result = tree or Tree(path="/repo/trees/x", branch="b", base="origin/main")

    def fake_create(spec, *, source_repo, github_url):
        captured["spec"] = spec
        captured["source_repo"] = source_repo
        captured["github_url"] = github_url
        return result

    monkeypatch.setattr(tree_verb, "create", fake_create)
    return captured


def test_run_create_epic_ws_shape_builds_spec(monkeypatch, capsys):
    _patch_identity(monkeypatch)
    captured = _capture_create(
        monkeypatch,
        Tree(path="/repo/trees/ws", branch="HAR02/WS02", base="origin/HAR02/umbrella"),
    )

    rc = tree_verb.run_create(epic="HAR02", ws=2, slug="Tiling")

    assert rc == 0
    spec = captured["spec"]
    assert spec.epic == "HAR02"
    assert spec.ws == 2
    assert spec.slug == "Tiling"
    assert spec.issue is None and spec.branch is None
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "READY"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload == {
        "path": "/repo/trees/ws",
        "branch": "HAR02/WS02",
        "base": "origin/HAR02/umbrella",
    }


def test_run_create_branch_shape_builds_spec(monkeypatch, capsys):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(git, "remote_branch_exists", lambda branch, *, cwd: False)
    captured = _capture_create(
        monkeypatch,
        Tree(path="/repo/trees/spike", branch="spike/foo", base="origin/main"),
    )

    rc = tree_verb.run_create(branch="spike/foo")

    assert rc == 0
    spec = captured["spec"]
    assert spec.branch == "spike/foo"
    assert spec.issue is None and spec.epic is None and spec.ws is None
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "READY"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["branch"] == "spike/foo"


def test_run_create_branch_shape_existing_remote_branch_uses_remote_head(
    monkeypatch, capsys
):
    _patch_identity(monkeypatch)
    probes = []

    def remote_branch_exists(branch, *, cwd):
        probes.append((branch, cwd))
        return True

    monkeypatch.setattr(git, "remote_branch_exists", remote_branch_exists)
    captured = _capture_create(
        monkeypatch,
        Tree(path="/repo/trees/spike", branch="spike/foo", base="origin/spike/foo"),
    )

    rc = tree_verb.run_create(branch="spike/foo")

    assert rc == 0
    assert probes == [("spike/foo", "/repo")]
    assert captured["spec"].branch == "spike/foo"
    assert captured["spec"].base == "origin/spike/foo"
    capsys.readouterr()


def test_run_create_branch_shape_new_branch_keeps_default_base(monkeypatch, capsys):
    _patch_identity(monkeypatch)
    probes = []

    def remote_branch_exists(branch, *, cwd):
        probes.append((branch, cwd))
        return False

    monkeypatch.setattr(git, "remote_branch_exists", remote_branch_exists)
    captured = _capture_create(monkeypatch)

    rc = tree_verb.run_create(branch="new/topic")

    assert rc == 0
    assert probes == [("new/topic", "/repo")]
    assert captured["spec"].branch == "new/topic"
    assert captured["spec"].base is None
    capsys.readouterr()


def test_run_create_branch_shape_invalid_freeform_skips_remote_probe(
    monkeypatch, capsys
):
    _patch_identity(monkeypatch)
    probes = []

    def remote_branch_exists(branch, *, cwd):
        probes.append((branch, cwd))
        return True

    def fake_create(spec, *, source_repo, github_url):
        layout_mod.plan(spec)
        raise AssertionError("planner should reject the branch")

    monkeypatch.setattr(git, "remote_branch_exists", remote_branch_exists)
    monkeypatch.setattr(tree_verb, "create", fake_create)

    rc = tree_verb.run_create(branch="///")

    assert rc == 1
    assert probes == []
    assert "sanitizes to an empty name" in capsys.readouterr().err


def test_run_create_issue_shape_unchanged(monkeypatch, capsys):
    _patch_identity(monkeypatch)
    captured = _capture_create(
        monkeypatch,
        Tree(path="/repo/trees/i", branch="issues/7/work", base="origin/main"),
    )

    rc = tree_verb.run_create(issue=7, slug="Thing")

    assert rc == 0
    spec = captured["spec"]
    assert spec.issue == 7
    assert spec.slug == "Thing"
    assert spec.epic is None and spec.ws is None and spec.branch is None


def test_run_create_zero_shapes_is_exit_1(monkeypatch, capsys):
    rc = tree_verb.run_create()

    assert rc == 1
    err = capsys.readouterr().err
    assert "exactly one shape" in err
    assert "got none" in err


def test_run_create_multiple_shapes_is_exit_1(monkeypatch, capsys):
    rc = tree_verb.run_create(issue=7, branch="spike/foo")

    assert rc == 1
    assert "exactly one shape" in capsys.readouterr().err


def test_run_create_partial_epic_missing_ws_is_exit_1(monkeypatch, capsys):
    rc = tree_verb.run_create(epic="HAR02")

    assert rc == 1
    err = capsys.readouterr().err
    assert "needs both --epic and --ws" in err


def test_run_create_partial_epic_missing_epic_is_exit_1(monkeypatch, capsys):
    rc = tree_verb.run_create(ws=2)

    assert rc == 1
    assert "needs both --epic and --ws" in capsys.readouterr().err


def test_run_create_bad_epic_code_surfaces_planner_error(monkeypatch, capsys):
    _patch_identity(monkeypatch)

    rc = tree_verb.run_create(epic="bad/code", ws=2)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def test_run_create_not_inside_checkout(monkeypatch, capsys):
    monkeypatch.setattr(git, "repo_root", lambda: None)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_run_create_reports_git_error_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")

    def boom(*, cwd):
        raise ExecError(["git"], rc=1, stderr="could not read origin remote")

    monkeypatch.setattr(git, "remote_url", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def _head_pr(number: int, state: str, *, is_draft: bool = False) -> gh.HeadPr:
    return gh.HeadPr(number=number, state=state, is_draft=is_draft, base_ref="main")


def _record(**over) -> TreeRecord:
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        mtime=1000.0,
        unpushed_shas=(),
    )
    base.update(over)
    base.setdefault("newest_mtime", base["mtime"])
    base.setdefault("last_commit", base["newest_mtime"])
    return TreeRecord(**base)


def test_run_list_renders_the_fleet_through_the_seam(monkeypatch, capsys):
    records = [
        _record(),
        _record(
            path="/trees/acme/widget/epics/HAR02/WS02-bbbb",
            branch="HAR02/WS02",
            base="origin/HAR02/umbrella",
            dirty=True,
            ahead=2,
            behind=1,
            mtime=500.0,
        ),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda root: records)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "BRANCH" in out and "BASE" in out and "PR" not in out
    assert "issues/7/work" in out and "HAR02/WS02" in out
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_run_list_empty_root_is_not_an_error(monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda root: [])

    rc = tree_verb.run_list()

    assert rc == 0
    assert "No Trees" in capsys.readouterr().out


def test_run_list_over_a_fixture_root_renders(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    clone = root / "acme" / "widget" / "issues" / "7" / "work-aaaa"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(root))
    monkeypatch.setattr(git, "current_branch", lambda *, cwd: "issues/7/work")
    monkeypatch.setattr(git, "upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(git, "status_porcelain", lambda *, cwd: [])
    monkeypatch.setattr(git, "ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "issues/7/work" in out
    assert str(clone) in out


def test_list_json_emits_the_typed_rows(monkeypatch, capsys):
    leaf = "widget-claude-20260717-081333-619cf51a-f501-44dc-992f-74df773204aa"
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(
        registry_mod, "scan", lambda root: [_record(path=f"/trees/{leaf}")]
    )

    rc = cli.main(["tree", "list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"trees"}
    row = payload["trees"][0]
    assert row["path"] == f"/trees/{leaf}"
    assert row["created"] == "20260717-081333"
    assert "kind" not in row
    assert row["branch"] == "issues/7/work"
    assert row["dirty"] is False


def test_list_help_advertises_json(capsys):
    rc = cli.main(["tree", "list", "--help"])
    assert rc == 0
    assert "--json" in capsys.readouterr().out


def test_run_create_maps_create_failure_to_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    monkeypatch.setattr(git, "remote_url", lambda *, cwd: "git@example:acme/widget")

    def boom(spec, *, source_repo, github_url):
        raise ExecError(["gh"], rc=1, stderr="clone failed")

    monkeypatch.setattr(tree_verb, "create", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc",
    [
        execrun.ExecError(["pixi", "install"], rc=1, stderr="boom"),
        OSError("disk full"),
        FileExistsError("tree dir already exists: /trees/...; refusing to clone"),
    ],
)
def test_run_create_maps_provisioning_and_fs_failures_to_clean_exit_1(
    monkeypatch, capsys, exc
):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    monkeypatch.setattr(git, "remote_url", lambda *, cwd: "git@example:acme/widget")

    def boom(spec, *, source_repo, github_url):
        raise exc

    monkeypatch.setattr(tree_verb, "create", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def test_fleet_verbs_report_misconfigured_root_through_the_shell(monkeypatch, capsys):
    monkeypatch.setenv("SHIPIT_TREES_ROOT", "relative/trees")

    for run in (
        tree_verb.run_list,
        lambda: tree_verb.run_remove("7-aaaa"),
        tree_verb.run_gc,
    ):
        rc = run()
        assert rc == 1
        err = capsys.readouterr().err
        assert err.startswith("error:")
        assert "SHIPIT_TREES_ROOT" in err


def _make_tree_dir(root, rel: str):
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _confirm_spy(answer: bool):
    calls: list[str] = []

    def confirm(message: str) -> bool:
        calls.append(message)
        return answer

    return confirm, calls


def test_run_remove_deletes_exactly_one_tree(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    other = _make_tree_dir(root, "acme/widget/issues/9/work-bbbb")
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(
        registry_mod,
        "scan",
        lambda r: [_record(path=str(target)), _record(path=str(other))],
    )

    rc = tree_verb.run_remove(str(target))

    assert rc == 0
    assert not target.exists()
    assert other.exists()
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_refusals_map_through_the_error_shell(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(tmp_path))
    monkeypatch.setattr(registry_mod, "scan", lambda r: [])

    rc = tree_verb.run_remove("does-not-exist")

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "no Tree matching" in err


def test_run_remove_dirty_tree_prompts_and_decline_keeps_it(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(
        registry_mod,
        "scan",
        lambda r: [_record(path=str(target), dirty=True)],
    )
    confirm, calls = _confirm_spy(False)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: True)

    assert rc == 1
    assert target.exists()
    assert len(calls) == 1
    assert str(target) in calls[0]
    err = capsys.readouterr().err
    assert err.startswith("error:") and "aborted" in err


def test_run_remove_dirty_tree_confirm_deletes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(
        registry_mod,
        "scan",
        lambda r: [_record(path=str(target), dirty=True)],
    )
    confirm, calls = _confirm_spy(True)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: True)

    assert rc == 0
    assert not target.exists()
    assert len(calls) == 1
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_risky_non_interactive_refuses_and_does_not_hang(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(
        registry_mod,
        "scan",
        lambda r: [_record(path=str(target), dirty=True)],
    )
    confirm, calls = _confirm_spy(True)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: False)

    assert rc == 1
    assert target.exists()
    assert calls == []
    assert "--yes" in capsys.readouterr().err


def test_run_remove_yes_flag_skips_prompt(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(
        registry_mod,
        "scan",
        lambda r: [_record(path=str(target), dirty=True, ahead=4)],
    )
    confirm, calls = _confirm_spy(False)

    rc = tree_verb.run_remove(
        str(target), assume_yes=True, confirm=confirm, is_tty=lambda: True
    )

    assert rc == 0
    assert not target.exists()
    assert calls == []


def test_stdin_is_tty_false_when_stdin_none(monkeypatch):
    # The default is_tty must survive a detached process where sys.stdin is None
    monkeypatch.setattr(tree_verb.sys, "stdin", None)
    assert tree_verb._stdin_is_tty() is False


def test_stdin_is_tty_false_when_stdin_closed(monkeypatch):
    class _Closed:
        closed = True

        def isatty(self):  # pragma: no cover - guard short-circuits on `closed`
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(tree_verb.sys, "stdin", _Closed())
    assert tree_verb._stdin_is_tty() is False


def test_stdin_is_tty_reflects_real_stream(monkeypatch):
    class _Stream:
        closed = False

        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    monkeypatch.setattr(tree_verb.sys, "stdin", _Stream(True))
    assert tree_verb._stdin_is_tty() is True
    monkeypatch.setattr(tree_verb.sys, "stdin", _Stream(False))
    assert tree_verb._stdin_is_tty() is False


def _gc_fleet(root, monkeypatch):
    removable = _make_tree_dir(root, "acme/widget/issues/1/work-idle")
    keep_dirty = _make_tree_dir(root, "acme/widget/issues/2/work-dirty")
    keep_unpushed = _make_tree_dir(root, "acme/widget/issues/3/work-unpushed")
    keep_active = _make_tree_dir(root, "acme/widget/issues/4/work-active")
    idle = 0.0
    records = [
        _record(path=str(removable), branch="b1", mtime=idle),
        _record(path=str(keep_dirty), branch="b2", dirty=True, mtime=idle),
        _record(
            path=str(keep_unpushed),
            branch="b3",
            unpushed_shas=(Sha("a" * 40),),
            mtime=idle,
        ),
        _record(path=str(keep_active), branch="b4", mtime=_time.time()),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)
    return removable, keep_dirty, keep_unpushed, keep_active


def _paths_after(out: str, marker: str) -> set[str]:
    paths = set()
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == marker:
            paths.add(parts[1])
    return paths


def test_run_gc_removes_only_removable_keeps_rest(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    removable, keep_dirty, keep_unpushed, keep_active = _gc_fleet(root, monkeypatch)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert not removable.exists()
    assert keep_dirty.exists()
    assert keep_unpushed.exists()
    assert keep_active.exists()
    out = capsys.readouterr().out
    assert f"REMOVED {removable}" in out
    assert "removed 1, kept 3" in out


def test_run_gc_empty_root_is_not_an_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: [])

    rc = tree_verb.run_gc()

    assert rc == 0
    assert "removed 0, kept 0" in capsys.readouterr().out


def test_run_gc_renders_sweep_failures_on_stderr(monkeypatch, capsys):
    result = tree_verb.gc.GcResult(
        removed=("/trees/good",),
        failed=(tree_verb.gc.GcFailure(path="/trees/bad", error="read-only file"),),
        kept=0,
        total=2,
        unexamined=0,
    )
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda r: [])

    def fake_sweep(plan, *, on_removed=None):
        on_removed("/trees/good")
        return result

    monkeypatch.setattr(tree_verb.gc, "sweep", fake_sweep)

    rc = tree_verb.run_gc()

    assert rc == 0
    captured = capsys.readouterr()
    assert "REMOVED /trees/good" in captured.out
    assert "FAILED  /trees/bad: read-only file" in captured.err
    assert "removed 1, kept 0" in captured.out


def test_run_gc_dry_run_lists_classifications_and_deletes_nothing(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    removable, keep_dirty, keep_unpushed, keep_active = _gc_fleet(root, monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not sweep")

    monkeypatch.setattr(tree_verb.gc, "sweep", boom)

    rc = tree_verb.run_gc(dry_run=True)

    assert rc == 0
    assert removable.exists() and keep_dirty.exists()
    assert keep_unpushed.exists() and keep_active.exists()
    out = capsys.readouterr().out
    assert f"REMOVABLE {removable}" in out
    assert f"KEEP      {keep_dirty}" in out
    assert f"KEEP      {keep_unpushed}" in out
    assert f"KEEP      {keep_active}" in out
    assert "no Trees deleted" in out
    assert "removable 1, keep 3" in out


def test_run_gc_dry_run_decisions_match_the_real_sweep(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    _gc_fleet(root, monkeypatch)

    assert tree_verb.run_gc(dry_run=True) == 0
    dry_out = capsys.readouterr().out

    assert tree_verb.run_gc() == 0
    real_out = capsys.readouterr().out

    assert _paths_after(dry_out, "REMOVABLE") == _paths_after(real_out, "REMOVED")
    assert _paths_after(dry_out, "REMOVABLE")


def _capture_plan_fleet(monkeypatch) -> dict:
    seen: dict = {}

    def fake_plan_fleet(root, *, idle_threshold_seconds):
        seen["idle_threshold_seconds"] = idle_threshold_seconds
        return tree_verb.gc.GcPlan(
            partition=tree_verb.cleanup.Cleanup(removable=[], keep=[]),
            total=0,
            unexamined=0,
        )

    monkeypatch.setattr(tree_verb.gc, "plan_fleet", fake_plan_fleet)
    return seen


def test_run_gc_threshold_overrides_the_idle_boundary(monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    seen = _capture_plan_fleet(monkeypatch)

    rc = tree_verb.run_gc(idle_threshold_seconds=36 * 3600.0)

    assert rc == 0
    assert seen["idle_threshold_seconds"] == 36 * 3600


def test_run_gc_default_threshold_is_48_hours(monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    seen = _capture_plan_fleet(monkeypatch)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert seen["idle_threshold_seconds"] == tree_verb.cleanup.IDLE_THRESHOLD_SECONDS


def test_gc_threshold_parses_at_click(monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    seen = _capture_plan_fleet(monkeypatch)

    rc = cli.main(["tree", "gc", "--threshold", "36h"])

    assert rc == 0
    assert seen["idle_threshold_seconds"] == 36 * 3600


def test_gc_bad_threshold_is_a_usage_error(monkeypatch, capsys):
    def boom(_r):
        raise AssertionError("must not scan when the threshold is invalid")

    monkeypatch.setattr(registry_mod, "scan", boom)

    rc = cli.main(["tree", "gc", "--threshold", "nope"])

    assert rc == 2
    assert "--threshold" in capsys.readouterr().err


def test_run_gc_incomplete_sweep_is_loud_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    judged = _make_tree_dir(root, "acme/widget/issues/1/work-idle")
    blind = _make_tree_dir(root, "acme/widget/issues/2/work-unreadable")
    records = [
        _record(path=str(judged), branch="b1", mtime=0.0),
        _record(path=str(blind), branch="b2", mtime=0.0, newest_mtime=None),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)

    rc = tree_verb.run_gc()

    assert rc == 1
    assert not judged.exists()
    assert blind.exists()
    captured = capsys.readouterr()
    assert "judged 1 of 2; 1 kept UNEXAMINED" in captured.err
    assert "not judged safe" in captured.err
    assert "gh api rate_limit" not in captured.err
    assert "local" in captured.err
    assert (
        "gc: INCOMPLETE — 1 of 2 unexamined (a signal could not be read); "
        "removed 1, kept 1" in captured.out
    )


def test_run_gc_never_reports_a_tree_it_deleted_as_unexamined(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    plain = _make_tree_dir(root, "acme/widget/issues/1/work-idle")
    other = _make_tree_dir(root, "acme/widget/issues/2/work-idle2")
    records = [
        _record(path=str(plain), branch="b1", mtime=0.0),
        _record(path=str(other), branch="b2", mtime=0.0),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)

    rc = tree_verb.run_gc()

    captured = capsys.readouterr()
    assert not plain.exists()
    assert not other.exists()
    assert "removed 2, kept 0" in captured.out
    assert "INCOMPLETE" not in captured.out
    assert "UNEXAMINED" not in captured.err
    assert rc == 0


def test_run_gc_incomplete_view_names_a_local_cause_not_a_network_one(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    blind = _make_tree_dir(root, "acme/widget/issues/1/work-unreadable")
    records = [
        _record(path=str(blind), branch="b1", mtime=0.0, unpushed_shas=None),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)

    rc = tree_verb.run_gc(dry_run=True)

    assert rc == 1
    err = capsys.readouterr().err
    assert "unpushed-commit list" in err
    assert "activity walk" in err
    assert "local" in err
    assert "gh api rate_limit" not in err
    assert "read once per repo" not in err


def test_run_gc_dry_run_warns_on_an_unexamined_tree_and_deletes_nothing(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    idle = _make_tree_dir(root, "acme/widget/issues/1/work-idle")
    blind = _make_tree_dir(root, "acme/widget/issues/2/work-unreadable")
    aged = 0.0
    records = [
        _record(path=str(idle), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(blind), branch="b2", mtime=aged, newest_mtime=None),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not sweep")

    monkeypatch.setattr(tree_verb.gc, "sweep", boom)

    rc = tree_verb.run_gc(dry_run=True)

    assert rc == 1
    assert idle.exists() and blind.exists()
    captured = capsys.readouterr()
    assert f"REMOVABLE {idle}" in captured.out
    assert f"KEEP      {blind}" in captured.out
    assert "no Trees deleted" in captured.out
    assert "INCOMPLETE — 1 of 2 unexamined (a signal could not be read)" in captured.out
    assert "would judge 1 of 2" in captured.err


def test_run_gc_streams_removals_before_the_summary(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    removable, _keep_dirty, _keep_unpushed, _keep_active = _gc_fleet(root, monkeypatch)

    assert tree_verb.run_gc() == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines.index(f"REMOVED {removable}") < lines.index(
        next(line for line in lines if line.startswith("gc: removed"))
    )
    assert [line for line in lines if line.startswith("REMOVED")] == [
        f"REMOVED {removable}"
    ]


def test_run_gc_interrupted_sweep_still_printed_what_it_destroyed(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda r: [])

    def killed_mid_sweep(plan, *, on_removed=None):
        on_removed("/trees/acme/widget/issues/1/work-a")
        on_removed("/trees/acme/widget/issues/2/work-b")
        raise KeyboardInterrupt

    monkeypatch.setattr(tree_verb.gc, "sweep", killed_mid_sweep)

    with pytest.raises(KeyboardInterrupt):
        tree_verb.run_gc()

    out = capsys.readouterr().out
    assert "REMOVED /trees/acme/widget/issues/1/work-a" in out
    assert "REMOVED /trees/acme/widget/issues/2/work-b" in out


def test_run_gc_flushes_each_removed_line(tmp_path, monkeypatch):
    class FlushCountingStdout(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushed_text: list[str] = []

        def flush(self):
            self.flushed_text.append(self.getvalue())

    root = tmp_path / "trees"
    removable, _stale, _kd, _ko = _gc_fleet(root, monkeypatch)
    stdout = FlushCountingStdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    tree_verb.run_gc()

    assert stdout.flushed_text
    assert stdout.flushed_text[0] == f"REMOVED {removable}\n"


def test_run_gc_no_warning_when_no_unknown(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    healthy = _make_tree_dir(root, "acme/widget/issues/1/work-healthy")
    records = [
        _record(
            path=str(healthy),
            branch="b1",
            dirty=False,
            ahead=0,
            mtime=0.0,
        ),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)

    rc = tree_verb.run_gc()

    assert rc == 0
    captured = capsys.readouterr()
    assert "INCOMPLETE" not in captured.out
    assert "skipped" not in captured.err and "skipped" not in captured.out
    assert "gc: removed 1, kept 0" in captured.out
