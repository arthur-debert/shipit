"""Unit tests for the ``shipit tree`` verb layer — glue + wiring only.

The ``create`` handler keeps its full glue coverage (resolve repo identity at
the gh boundary, hand a typed :class:`TreeSpec` to the planner+orchestrator,
print READY). The ``list``/``remove``/``gc`` sections are the CLI02-WS03 thin
WIRING smoke layer: the promoted domain logic (fleet rows, removal gating, gc
plan+sweep) is typed-tested in ``test_tree_fleet`` / ``test_tree_removal`` /
``test_tree_gc``; here we prove only the click binding, the render seam, the
error shell (``error: …`` + exit 1), and the two-tier exit contract.
"""

from __future__ import annotations

import json

import pytest

from shipit import cli, execrun, gh, git
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.tree import layout as layout_mod
from shipit.tree import registry as registry_mod
from shipit.tree.create import Tree
from shipit.tree.registry import TreeRecord
from shipit.verbs import tree as tree_verb


def test_run_create_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    # Identity derives LOCALLY from the origin remote (ADR-0024): the patched
    # remote URL is what identity.resolve_repo parses into the canonical Repo.
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
    # The verb resolved identity into the spec it handed the orchestrator.
    assert captured["spec"].repo == repo_from_slug("acme/widget")
    assert captured["spec"].issue == 7
    assert captured["spec"].slug == "Thing"
    assert captured["source_repo"] == "/repo"
    assert captured["github_url"] == "git@example:acme/widget"
    # READY summary is the orchestrator's result, as a READY line + JSON.
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "READY"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload == {
        "path": "/repo/trees/x",
        "branch": "issues/7/work",
        "base": "origin/main",
    }


def _patch_identity(monkeypatch):
    """Mock the gh boundary so run_create resolves a fixed repo identity (acme/widget)."""
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    # Identity derives LOCALLY from the origin remote (ADR-0024): the patched
    # remote URL is what identity.resolve_repo parses into the canonical Repo.
    monkeypatch.setattr(git, "remote_url", lambda *, cwd: "git@example:acme/widget")


def _capture_create(monkeypatch, tree: Tree | None = None) -> dict:
    """Replace the orchestrator with a spy; return the dict it records the spec into."""
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
    # The epic shape rides through as the typed fields the planner dispatches on.
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


def test_run_create_branch_shape_existing_remote_branch_uses_remote_head(monkeypatch):
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


def test_run_create_branch_shape_new_branch_keeps_default_base(monkeypatch):
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


def test_run_create_issue_shape_unchanged(monkeypatch, capsys):
    # The --issue path keeps its exact behavior now that it shares the verb with the
    # other two shapes: same spec fields, same READY summary.
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
    # No gh mocks needed: the flag-grammar gate fires before any repo resolution.
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
    # A well-formed flag grammar but a domain-invalid epic code: the gate passes,
    # then the planner's ValueError (raised by plan() before any clone side effect)
    # surfaces as a clean exit-1, not a traceback.
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
    # The typed pr_for_head hit (PROC03): gc's classifier only branches on
    # number/state/is_draft, so the base is a fixed placeholder.
    return gh.HeadPr(number=number, state=state, is_draft=is_draft, base_ref="main")


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
        pr="#7 DRAFT",
        mtime=1000.0,
        unpushed_shas=(),
    )
    base.update(over)
    return TreeRecord(**base)


# --- tree list: wiring smoke (typed fleet + renderer tested in test_tree_fleet) --


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
            pr="#9 OPEN",
            mtime=500.0,
        ),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda root: records)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    # The scan reached the renderer: both Trees, table headers, the BASE annotation.
    assert "BRANCH" in out and "BASE" in out and "PR" in out
    assert "issues/7/work" in out and "HAR02/WS02" in out
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_run_list_empty_root_is_not_an_error(monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda root: [])

    rc = tree_verb.run_list()

    assert rc == 0
    assert "No Trees" in capsys.readouterr().out


def test_run_list_over_a_fixture_root_renders(tmp_path, monkeypatch, capsys):
    # End to end: a real fixture central root + a real scan, only the gh boundary
    # patched. `shipit tree list` must render the clone without error.
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
    # The full argv round trip for the new read-path surface: `tree list --json`
    # serializes the typed rows' declared field set through the render seam.
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda root: [_record()])

    rc = cli.main(["tree", "list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"trees"}
    row = payload["trees"][0]
    assert row["path"] == "/trees/acme/widget/issues/7/work-aaaa"
    assert row["kind"] == "write"
    assert row["branch"] == "issues/7/work"
    assert row["dirty"] is False


def test_list_help_advertises_json(capsys):
    rc = cli.main(["tree", "list", "--help"])
    assert rc == 0
    assert "--json" in capsys.readouterr().out


def test_run_create_maps_create_failure_to_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    # Identity derives LOCALLY from the origin remote (ADR-0024): the patched
    # remote URL is what identity.resolve_repo parses into the canonical Repo.
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
        execrun.ExecError(
            ["pixi", "install"], rc=1, stderr="boom"
        ),  # provisioning failed
        OSError("disk full"),  # a filesystem step failed
        FileExistsError("tree dir already exists: /trees/...; refusing to clone"),
    ],
)
def test_run_create_maps_provisioning_and_fs_failures_to_clean_exit_1(
    monkeypatch, capsys, exc
):
    # The create contract: git/gh/provisioning/filesystem failures are a clean
    # exit-1 message, never a traceback. ExecError (provisioning), OSError (mkdir/
    # copy/stat), and the pre-existing-dest FileExistsError all funnel through here.
    monkeypatch.setattr(git, "repo_root", lambda: "/repo")
    # Identity derives LOCALLY from the origin remote (ADR-0024): the patched
    # remote URL is what identity.resolve_repo parses into the canonical Repo.
    monkeypatch.setattr(git, "remote_url", lambda *, cwd: "git@example:acme/widget")

    def boom(spec, *, source_repo, github_url):
        raise exc

    monkeypatch.setattr(tree_verb, "create", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def test_fleet_verbs_report_misconfigured_root_through_the_shell(monkeypatch, capsys):
    # A relative SHIPIT_TREES_ROOT makes central_root() raise the typed
    # LayoutError; every fleet verb surfaces it through the shared error shell —
    # `error: …` + exit 1, never a traceback (the CLI02-WS03 exit-contract move
    # off the old per-verb `tree <verb>:` prefixes).
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


# --- tree remove: wiring smoke (gating/resolution typed in test_tree_removal) ---


def _make_tree_dir(root, rel: str):
    """Create ``root/<rel>`` as a fake Tree clone (a dir carrying a ``.git`` marker)."""
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _confirm_spy(answer: bool):
    """A confirm callback that records the prompt it was asked and returns ``answer``."""
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
    assert not target.exists()  # the matched Tree is gone
    assert other.exists()  # the sibling is untouched
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_refusals_map_through_the_error_shell(tmp_path, monkeypatch, capsys):
    # The typed RemovalError refusals (unknown target here; the full truth table
    # is typed-tested in test_tree_removal) surface as `error: …` + exit 1.
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
    # The one terminal concern left at the verb: a CONFIRM gate outcome puts the
    # domain's prompt to the injected confirm; declining is a typed refusal.
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
    assert target.exists()  # declined -> Tree survives
    assert len(calls) == 1  # prompted before deleting
    assert str(target) in calls[0]  # the domain's prompt reached the terminal
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
    assert not target.exists()  # confirmed -> removed
    assert len(calls) == 1
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_risky_non_interactive_refuses_and_does_not_hang(
    tmp_path, monkeypatch, capsys
):
    # No TTY and no --yes: the REFUSE gate outcome — refused, never blocking on
    # a prompt.
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
    assert target.exists()  # not destroyed
    assert calls == []  # never prompted -> cannot hang
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
    assert not target.exists()  # --yes removed it despite the risk
    assert calls == []  # prompt skipped unconditionally


def test_stdin_is_tty_false_when_stdin_none(monkeypatch):
    # The default is_tty must survive a detached process where sys.stdin is None
    # (would AttributeError on sys.stdin.isatty) — reading as not-a-TTY, not crashing.
    monkeypatch.setattr(tree_verb.sys, "stdin", None)
    assert tree_verb._stdin_is_tty() is False


def test_stdin_is_tty_false_when_stdin_closed(monkeypatch):
    # A closed stream raises ValueError from isatty(); the guard returns not-a-TTY.
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


# --- tree gc: wiring smoke (plan + sweep typed-tested in test_tree_gc) -----------


def _gc_fleet(root, monkeypatch):
    """A four-Tree fixture for gc: one removable, one stale, one dirty-keep, one open-keep.

    Returns ``(removable, stale, keep_dirty, keep_open)`` paths after wiring
    ``central_root``/``scan``/``pr_for_head`` so both ``run_gc()`` and its dry-run share
    one fleet. The removable Tree (merged + clean + aged) is the only delete candidate.
    """
    removable = _make_tree_dir(root, "acme/widget/issues/1/work-merged")
    stale = _make_tree_dir(root, "acme/widget/issues/2/work-orphan")
    keep_dirty = _make_tree_dir(root, "acme/widget/issues/3/work-dirty")
    keep_open = _make_tree_dir(root, "acme/widget/issues/4/work-open")
    aged = 0.0  # mtime far in the past -> always aged vs time.time()
    records = [
        _record(path=str(removable), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(stale), branch="b2", dirty=False, ahead=0, mtime=aged),
        _record(path=str(keep_dirty), branch="b3", dirty=True, ahead=0, mtime=aged),
        _record(path=str(keep_open), branch="b4", dirty=False, ahead=0, mtime=aged),
    ]
    pr_by_branch = {
        "b1": _head_pr(1, "MERGED"),
        "b2": None,
        "b3": _head_pr(3, "MERGED"),
        "b4": _head_pr(4, "OPEN"),
    }
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )
    return removable, stale, keep_dirty, keep_open


def _paths_after(out: str, marker: str) -> set[str]:
    """The set of paths on lines whose first whitespace-delimited token is ``marker``."""
    paths = set()
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == marker:
            paths.add(parts[1])
    return paths


def test_run_gc_removes_only_removable_lists_stale_keeps_rest(
    tmp_path, monkeypatch, capsys
):
    # The full wiring round trip: plan_fleet -> sweep -> the rendered summary.
    root = tmp_path / "trees"
    removable, stale, keep_dirty, keep_open = _gc_fleet(root, monkeypatch)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert not removable.exists()  # only the removable Tree is deleted
    assert stale.exists()  # ambiguous -> listed, never removed
    assert keep_dirty.exists()  # local work protected
    assert keep_open.exists()  # in-flight PR protected
    out = capsys.readouterr().out
    assert f"REMOVED {removable}" in out
    assert f"STALE   {stale}" in out
    assert "removed 1, stale 1, kept 2" in out


def test_run_gc_empty_root_is_not_an_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: [])

    rc = tree_verb.run_gc()

    assert rc == 0
    assert "removed 0, stale 0, kept 0" in capsys.readouterr().out


def test_run_gc_renders_sweep_failures_on_stderr(monkeypatch, capsys):
    # The renderer's stderr contract for a typed GcResult: the failures the sweep
    # continued past read as FAILED lines, and the count reflects disk reality.
    result = tree_verb.gc.GcResult(
        removed=("/trees/good",),
        failed=(tree_verb.gc.GcFailure(path="/trees/bad", error="read-only file"),),
        stale=(),
        kept=0,
        total=2,
        unknown=0,
    )
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    monkeypatch.setattr(registry_mod, "scan", lambda r: [])
    monkeypatch.setattr(tree_verb.gc, "sweep", lambda plan: result)

    rc = tree_verb.run_gc()

    assert rc == 0
    captured = capsys.readouterr()
    assert "REMOVED /trees/good" in captured.out
    assert "FAILED  /trees/bad: read-only file" in captured.err
    assert "removed 1, stale 0, kept 0" in captured.out


def test_run_gc_dry_run_lists_classifications_and_deletes_nothing(
    tmp_path, monkeypatch, capsys
):
    # --dry-run prints every Tree's bucket and must not touch disk: sweeping is
    # fatal here.
    root = tmp_path / "trees"
    removable, stale, keep_dirty, keep_open = _gc_fleet(root, monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not sweep")

    monkeypatch.setattr(tree_verb.gc, "sweep", boom)

    rc = tree_verb.run_gc(dry_run=True)

    assert rc == 0
    # Nothing was deleted.
    assert removable.exists() and stale.exists()
    assert keep_dirty.exists() and keep_open.exists()
    out = capsys.readouterr().out
    # Each Tree is listed under its classification, and the summary says zero deleted.
    assert f"REMOVABLE {removable}" in out
    assert f"STALE     {stale}" in out
    assert f"KEEP      {keep_dirty}" in out
    assert f"KEEP      {keep_open}" in out
    assert "no Trees deleted" in out
    assert "removable 1, stale 1, keep 2" in out


def test_run_gc_dry_run_decisions_match_the_real_sweep(tmp_path, monkeypatch, capsys):
    # Parity: the paths --dry-run labels REMOVABLE are exactly the ones the real sweep
    # REMOVEs. Both modes consume the ONE plan_fleet plan, so the preview cannot drift.
    root = tmp_path / "trees"
    _gc_fleet(root, monkeypatch)

    assert tree_verb.run_gc(dry_run=True) == 0
    dry_out = capsys.readouterr().out

    assert tree_verb.run_gc() == 0  # real sweep over the same fleet (dry-run deleted 0)
    real_out = capsys.readouterr().out

    assert _paths_after(dry_out, "REMOVABLE") == _paths_after(real_out, "REMOVED")
    assert _paths_after(
        dry_out, "REMOVABLE"
    )  # and it is non-empty (proves real parity)


def _capture_plan_fleet(monkeypatch) -> dict:
    """Spy on gc.plan_fleet: record its kwargs, return an empty plan."""
    seen: dict = {}

    def fake_plan_fleet(root, *, max_age_seconds):
        seen["max_age_seconds"] = max_age_seconds
        return tree_verb.gc.GcPlan(
            partition=tree_verb.cleanup.Cleanup(removable=[], stale=[], keep=[]),
            total=0,
            unknown=0,
        )

    monkeypatch.setattr(tree_verb.gc, "plan_fleet", fake_plan_fleet)
    return seen


def test_run_gc_threshold_overrides_the_age_boundary(monkeypatch, capsys):
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    seen = _capture_plan_fleet(monkeypatch)

    rc = tree_verb.run_gc(max_age_seconds=36 * 3600.0)

    assert rc == 0
    assert seen["max_age_seconds"] == 36 * 3600


def test_run_gc_default_threshold_is_two_weeks(monkeypatch, capsys):
    # Omitting --threshold passes the 14-day default through to the plan unchanged.
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    seen = _capture_plan_fleet(monkeypatch)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert seen["max_age_seconds"] == tree_verb.cleanup.DEFAULT_MAX_AGE_SECONDS


def test_gc_threshold_parses_at_click(monkeypatch, capsys):
    # The shared DURATION param mints seconds at argv parse: the verb sees a float.
    monkeypatch.setattr(layout_mod, "central_root", lambda: "/trees")
    seen = _capture_plan_fleet(monkeypatch)

    rc = cli.main(["tree", "gc", "--threshold", "36h"])

    assert rc == 0
    assert seen["max_age_seconds"] == 36 * 3600


def test_gc_bad_threshold_is_a_usage_error(monkeypatch, capsys):
    # The CLI02-WS03 exit-contract move: a malformed --threshold is click's job
    # now — a usage error (exit 2) at parse, never a sweep (scan is fatal here).
    def boom(_r):
        raise AssertionError("must not scan when the threshold is invalid")

    monkeypatch.setattr(registry_mod, "scan", boom)

    rc = cli.main(["tree", "gc", "--threshold", "nope"])

    assert rc == 2
    assert "--threshold" in capsys.readouterr().err


def test_run_gc_warns_on_incomplete_sweep(tmp_path, monkeypatch, capsys):
    # When any Tree's PR state is UNKNOWN, gc prints the incomplete-sweep warning so
    # the operator knows the sweep did not see the whole fleet. The UNKNOWN Tree is
    # classified conservatively (stale -> never removed).
    root = tmp_path / "trees"
    merged = _make_tree_dir(root, "acme/widget/issues/1/work-merged")
    unknown = _make_tree_dir(root, "acme/widget/issues/2/work-unknown")
    aged = 0.0
    records = [
        _record(path=str(merged), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(unknown), branch="b2", dirty=False, ahead=0, mtime=aged),
    ]
    pr_by_branch = {
        "b1": _head_pr(1, "MERGED"),
        "b2": gh.UNKNOWN,
    }
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )

    rc = tree_verb.run_gc()

    assert rc == 0
    assert not merged.exists()  # the readable, merged Tree is reclaimed
    assert unknown.exists()  # the unreadable Tree is left untouched (conservative)
    captured = capsys.readouterr()
    assert "swept 1 of 2; 1 skipped (state unknown)" in captured.err
    # The summary still counts it as stale, not removed.
    assert "removed 1, stale 1, kept 0" in captured.out


def test_run_gc_dry_run_warns_on_unknown_and_deletes_nothing(
    tmp_path, monkeypatch, capsys
):
    # A --dry-run preview over a fleet that contains an unreadable-state Tree must
    # still surface the incomplete-view warning, yet touch nothing on disk. The
    # UNKNOWN Tree lands in STALE (conservative).
    root = tmp_path / "trees"
    merged = _make_tree_dir(root, "acme/widget/issues/1/work-merged")
    unknown = _make_tree_dir(root, "acme/widget/issues/2/work-unknown")
    aged = 0.0
    records = [
        _record(path=str(merged), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(unknown), branch="b2", dirty=False, ahead=0, mtime=aged),
    ]
    pr_by_branch = {
        "b1": _head_pr(1, "MERGED"),
        "b2": gh.UNKNOWN,
    }
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not sweep")

    monkeypatch.setattr(tree_verb.gc, "sweep", boom)

    rc = tree_verb.run_gc(dry_run=True)

    assert rc == 0
    assert merged.exists() and unknown.exists()  # nothing deleted in dry-run
    captured = capsys.readouterr()
    # Preview lists the partition (the UNKNOWN Tree is conservatively STALE) ...
    assert f"REMOVABLE {merged}" in captured.out
    assert f"STALE     {unknown}" in captured.out
    assert "no Trees deleted" in captured.out
    # ... and still warns that the fleet was only partially seen, phrased for a preview.
    assert "would sweep 1 of 2; 1 skipped (state unknown)" in captured.err


def test_run_gc_no_warning_when_no_unknown(tmp_path, monkeypatch, capsys):
    # A sweep where every PR state is readable prints NO incomplete-sweep warning.
    root = tmp_path / "trees"
    merged = _make_tree_dir(root, "acme/widget/issues/1/work-merged")
    records = [
        _record(path=str(merged), branch="b1", dirty=False, ahead=0, mtime=0.0),
    ]
    monkeypatch.setattr(layout_mod, "central_root", lambda: str(root))
    monkeypatch.setattr(registry_mod, "scan", lambda r: records)
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: _head_pr(1, "MERGED"),
    )

    rc = tree_verb.run_gc()

    assert rc == 0
    captured = capsys.readouterr()
    assert "skipped (state unknown)" not in captured.err
    assert "skipped (state unknown)" not in captured.out
