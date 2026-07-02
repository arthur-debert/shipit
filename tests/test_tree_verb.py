"""Unit tests for the ``shipit tree create`` verb handler (``run_create``).

The verb is thin glue: resolve repo identity at the gh boundary, hand a typed
:class:`TreeSpec` to the planner+orchestrator, and print READY. These tests mock
the ``gh``/``create`` boundary so they pin the glue — exit codes, the spec it
builds, and the error paths — without touching real git.
"""

from __future__ import annotations

import json

import pytest

from shipit import gh, proc
from shipit.tree.create import Tree
from shipit.tree.registry import TreeRecord
from shipit.verbs import tree as tree_verb


def test_run_create_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(gh, "current_repo", lambda: "acme/widget")
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:acme/widget")

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
    assert captured["spec"].org == "acme"
    assert captured["spec"].repo == "widget"
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
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(gh, "current_repo", lambda: "acme/widget")
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:acme/widget")


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
    monkeypatch.setattr(gh, "repo_root", lambda: None)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_run_create_reports_gh_error_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")

    def boom():
        raise gh.GhError("could not resolve repo")

    monkeypatch.setattr(gh, "current_repo", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def _record(**over) -> TreeRecord:
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr="#7 DRAFT",
        mtime=1000.0,
    )
    base.update(over)
    return TreeRecord(**base)


def test_run_list_renders_fleet_table(monkeypatch, capsys):
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
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/trees")
    monkeypatch.setattr(tree_verb.registry, "scan", lambda root: records)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    # Header + both Trees render, with branch, base, dirty state, and PR label.
    assert "BRANCH" in out and "BASE" in out and "PR" in out
    assert "issues/7/work" in out
    assert "HAR02/WS02" in out
    assert "clean" in out and "dirty" in out
    assert "#7 DRAFT" in out and "#9 OPEN" in out
    # Divergence is annotated on the BASE cell.
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_run_list_empty_root_is_not_an_error(monkeypatch, capsys):
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/trees")
    monkeypatch.setattr(tree_verb.registry, "scan", lambda root: [])

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
    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "issues/7/work")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "issues/7/work" in out
    assert str(clone) in out


def test_run_list_scans_the_central_root(monkeypatch, capsys):
    seen: dict = {}
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/central/trees")

    def fake_scan(root):
        seen["root"] = root
        return []

    monkeypatch.setattr(tree_verb.registry, "scan", fake_scan)

    tree_verb.run_list()

    assert seen["root"] == "/central/trees"


def test_run_create_maps_create_failure_to_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(gh, "current_repo", lambda: "acme/widget")
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:acme/widget")

    def boom(spec, *, source_repo, github_url):
        raise gh.GhError("clone failed")

    monkeypatch.setattr(tree_verb, "create", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc",
    [
        proc.ProcError(["pixi", "install"], 1, "boom"),  # provisioning failed
        OSError("disk full"),  # a filesystem step failed
        FileExistsError("tree dir already exists: /trees/...; refusing to clone"),
    ],
)
def test_run_create_maps_provisioning_and_fs_failures_to_clean_exit_1(
    monkeypatch, capsys, exc
):
    # The create contract: git/gh/provisioning/filesystem failures are a clean
    # exit-1 message, never a traceback. ProcError (provisioning), OSError (mkdir/
    # copy/stat), and the pre-existing-dest FileExistsError all funnel through here.
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(gh, "current_repo", lambda: "acme/widget")
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:acme/widget")

    def boom(spec, *, source_repo, github_url):
        raise exc

    monkeypatch.setattr(tree_verb, "create", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def _raise_relative_root() -> None:
    raise ValueError("SHIPIT_TREES_ROOT must be an absolute path")


def test_run_list_reports_misconfigured_root_cleanly(monkeypatch, capsys):
    # A relative SHIPIT_TREES_ROOT makes central_root() raise; list must surface it
    # as a clean exit-1 message, not a traceback.
    monkeypatch.setattr(tree_verb.layout, "central_root", _raise_relative_root)

    rc = tree_verb.run_list()

    assert rc == 1
    assert "tree list:" in capsys.readouterr().err


def test_run_remove_reports_misconfigured_root_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(tree_verb.layout, "central_root", _raise_relative_root)

    rc = tree_verb.run_remove("7-aaaa")

    assert rc == 1
    assert "tree remove:" in capsys.readouterr().err


def test_run_gc_reports_misconfigured_root_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(tree_verb.layout, "central_root", _raise_relative_root)

    rc = tree_verb.run_gc()

    assert rc == 1
    assert "tree gc:" in capsys.readouterr().err


# --- tree remove ---------------------------------------------------------------


def _make_tree_dir(root, rel: str):
    """Create ``root/<rel>`` as a fake Tree clone (a dir carrying a ``.git`` marker)."""
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def test_run_remove_deletes_exactly_one_tree(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    other = _make_tree_dir(root, "acme/widget/issues/9/work-bbbb")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target)), _record(path=str(other))],
    )

    rc = tree_verb.run_remove(str(target))

    assert rc == 0
    assert not target.exists()  # the matched Tree is gone
    assert other.exists()  # the sibling is untouched
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_matches_by_dir_name(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry, "scan", lambda r: [_record(path=str(target))]
    )

    rc = tree_verb.run_remove("work-aaaa")  # short dir-name, not the full path

    assert rc == 0
    assert not target.exists()


def test_run_remove_no_match_is_an_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: [])

    rc = tree_verb.run_remove("does-not-exist")

    assert rc == 1
    assert "no Tree matching" in capsys.readouterr().err


def test_run_remove_ambiguous_match_refuses(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    a = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    b = _make_tree_dir(
        root, "acme/gadget/issues/7/work-aaaa"
    )  # same dir name, two repos
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(a)), _record(path=str(b))],
    )

    rc = tree_verb.run_remove("work-aaaa")  # the shared leaf name — matches both repos

    assert rc == 1
    assert "ambiguous" in capsys.readouterr().err
    assert a.exists() and b.exists()  # nothing deleted on an ambiguous match


def test_run_remove_reports_rmtree_failure_cleanly(tmp_path, monkeypatch, capsys):
    # A failed delete (read-only file, lock, vanished dir) must surface as a clean
    # exit-1 + stderr message, never an unhandled traceback that breaks the contract.
    target = _make_tree_dir(tmp_path / "trees", "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        tree_verb.registry, "scan", lambda r: [_record(path=str(target))]
    )

    def boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(tree_verb, "remove_tree", boom)

    rc = tree_verb.run_remove(str(target))

    assert rc == 1
    assert "could not remove" in capsys.readouterr().err


# --- tree remove: risk-detection + confirmation gate ---------------------------


def test_removal_risk_clean_pushed_tree_is_safe():
    # A clean, fully-pushed Tree holds no work that the delete would lose -> no gate.
    assert tree_verb._removal_risk(_record(dirty=False, ahead=0)) is None


def test_removal_risk_flags_dirty():
    risk = tree_verb._removal_risk(_record(dirty=True, ahead=0))
    assert risk is not None and "uncommitted" in risk


def test_removal_risk_flags_unpushed_commits():
    risk = tree_verb._removal_risk(_record(dirty=False, ahead=3))
    assert risk is not None and "3 unpushed commit" in risk


def test_removal_risk_combines_dirty_and_unpushed():
    risk = tree_verb._removal_risk(_record(dirty=True, ahead=1))
    assert risk is not None
    assert "uncommitted" in risk and "1 unpushed commit" in risk


def _confirm_spy(answer: bool):
    """A confirm callback that records the prompt it was asked and returns ``answer``."""
    calls: list[str] = []

    def confirm(message: str) -> bool:
        calls.append(message)
        return answer

    return confirm, calls


def test_gate_removal_clean_proceeds_without_prompting():
    confirm, calls = _confirm_spy(False)
    block = tree_verb._gate_removal(
        _record(dirty=False, ahead=0),
        assume_yes=False,
        is_tty=lambda: True,
        confirm=confirm,
    )
    assert block is None  # safe -> proceed
    assert calls == []  # never prompted


def test_gate_removal_assume_yes_skips_prompt_even_when_risky():
    confirm, calls = _confirm_spy(False)
    block = tree_verb._gate_removal(
        _record(dirty=True, ahead=2),
        assume_yes=True,
        is_tty=lambda: True,
        confirm=confirm,
    )
    assert block is None  # --yes proceeds unconditionally
    assert calls == []  # bypassed the prompt entirely


def test_gate_removal_risky_tty_confirm_proceeds():
    confirm, calls = _confirm_spy(True)
    block = tree_verb._gate_removal(
        _record(dirty=True, ahead=0),
        assume_yes=False,
        is_tty=lambda: True,
        confirm=confirm,
    )
    assert block is None  # confirmed -> proceed
    assert len(calls) == 1  # the user was asked


def test_gate_removal_risky_tty_decline_blocks():
    confirm, _calls = _confirm_spy(False)
    block = tree_verb._gate_removal(
        _record(dirty=True, ahead=0),
        assume_yes=False,
        is_tty=lambda: True,
        confirm=confirm,
    )
    assert block is not None and "aborted" in block


def test_gate_removal_risky_non_interactive_refuses_without_yes():
    confirm, calls = _confirm_spy(True)
    block = tree_verb._gate_removal(
        _record(dirty=False, ahead=1),
        assume_yes=False,
        is_tty=lambda: False,
        confirm=confirm,
    )
    assert block is not None and "non-interactively" in block
    assert calls == []  # never blocks on a prompt when there is no TTY


def test_run_remove_dirty_tree_prompts_and_decline_keeps_it(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target), dirty=True)],
    )
    confirm, calls = _confirm_spy(False)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: True)

    assert rc == 1
    assert target.exists()  # declined -> Tree survives
    assert len(calls) == 1  # prompted before deleting
    assert "aborted" in capsys.readouterr().err


def test_run_remove_dirty_tree_confirm_deletes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target), dirty=True)],
    )
    confirm, calls = _confirm_spy(True)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: True)

    assert rc == 0
    assert not target.exists()  # confirmed -> removed
    assert len(calls) == 1
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_unpushed_tree_prompts(tmp_path, monkeypatch, capsys):
    # Unpushed commits (ahead > 0) are risky even with a clean working tree.
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target), dirty=False, ahead=2)],
    )
    confirm, calls = _confirm_spy(False)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: True)

    assert rc == 1
    assert target.exists()
    assert len(calls) == 1  # the unpushed work triggered the prompt


def test_run_remove_clean_tree_deletes_without_prompt(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target), dirty=False, ahead=0)],
    )
    confirm, calls = _confirm_spy(True)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: True)

    assert rc == 0
    assert not target.exists()
    assert calls == []  # clean+pushed -> removed silently, no prompt


def test_run_remove_yes_flag_skips_prompt(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
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


def test_run_remove_risky_non_interactive_refuses_and_does_not_hang(
    tmp_path, monkeypatch, capsys
):
    # No TTY and no --yes: a risky remove is refused, never blocking on a prompt.
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target), dirty=True)],
    )
    confirm, calls = _confirm_spy(True)

    rc = tree_verb.run_remove(str(target), confirm=confirm, is_tty=lambda: False)

    assert rc == 1
    assert target.exists()  # not destroyed
    assert calls == []  # never prompted -> cannot hang
    assert "--yes" in capsys.readouterr().err


def test_run_remove_clean_tree_non_interactive_deletes(tmp_path, monkeypatch, capsys):
    # The safe non-interactive path: a clean+pushed Tree is removed without --yes.
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7/work-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry, "scan", lambda r: [_record(path=str(target))]
    )

    rc = tree_verb.run_remove(str(target), is_tty=lambda: False)

    assert rc == 0
    assert not target.exists()


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


# --- tree gc -------------------------------------------------------------------


def test_run_gc_removes_only_removable_lists_stale_keeps_rest(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    # Four Trees: one removable (merged+aged), one stale (no PR+aged), one kept dirty,
    # one kept in-flight (open PR). gc must delete ONLY the removable one.
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
        "b1": {"number": 1, "state": "MERGED", "isDraft": False},
        "b2": None,
        "b3": {"number": 3, "state": "MERGED", "isDraft": False},
        "b4": {"number": 4, "state": "OPEN", "isDraft": False},
    }
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )

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
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: [])

    rc = tree_verb.run_gc()

    assert rc == 0
    assert "removed 0, stale 0, kept 0" in capsys.readouterr().out


def test_run_gc_continues_past_a_failed_delete(tmp_path, monkeypatch, capsys):
    # Two removable Trees; the first delete fails. The sweep must continue to the
    # second, report the failure on stderr, and count only the delete that landed.
    root = tmp_path / "trees"
    bad = _make_tree_dir(root, "acme/widget/issues/1/work-bad")
    good = _make_tree_dir(root, "acme/widget/issues/2/work-good")
    aged = 0.0
    records = [
        _record(path=str(bad), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(good), branch="b2", dirty=False, ahead=0, mtime=aged),
    ]
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: {"number": 1, "state": "MERGED", "isDraft": False},
    )

    real_remove_tree = tree_verb.remove_tree

    def flaky(path, *args, **kwargs):
        if path == str(bad):
            raise OSError("read-only file")
        return real_remove_tree(path, *args, **kwargs)

    monkeypatch.setattr(tree_verb, "remove_tree", flaky)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert bad.exists()  # the failed delete left it on disk
    assert not good.exists()  # the sweep continued and reclaimed the next one
    captured = capsys.readouterr()
    assert f"FAILED  {bad}" in captured.err
    assert "removed 1, stale 0, kept 0" in captured.out  # count reflects disk reality


def test_run_gc_does_not_count_an_already_gone_tree(tmp_path, monkeypatch, capsys):
    # A removable Tree whose directory is ALREADY gone (a concurrent sweep, a manual
    # rm) must not be counted or printed as REMOVED: remove_tree reports False (no-op),
    # so `removed` reflects what actually came off disk, not what was merely planned.
    root = tmp_path / "trees"
    present = _make_tree_dir(root, "acme/widget/issues/1/work-present")
    gone = root / "acme/widget/issues/2/work-gone"  # never created on disk
    aged = 0.0
    records = [
        _record(path=str(present), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(gone), branch="b2", dirty=False, ahead=0, mtime=aged),
    ]
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: {"number": 1, "state": "MERGED", "isDraft": False},
    )

    rc = tree_verb.run_gc()

    assert rc == 0
    assert not present.exists()  # the present Tree was reclaimed
    captured = capsys.readouterr()
    assert f"REMOVED {present}" in captured.out
    assert f"REMOVED {gone}" not in captured.out  # nothing came off disk for it
    assert "removed 1, stale 0, kept 0" in captured.out  # the gone Tree is uncounted


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
        "b1": {"number": 1, "state": "MERGED", "isDraft": False},
        "b2": None,
        "b3": {"number": 3, "state": "MERGED", "isDraft": False},
        "b4": {"number": 4, "state": "OPEN", "isDraft": False},
    }
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
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


def test_run_gc_dry_run_lists_classifications_and_deletes_nothing(
    tmp_path, monkeypatch, capsys
):
    # --dry-run prints every Tree's bucket and must not touch disk: rmtree is fatal here.
    root = tmp_path / "trees"
    removable, stale, keep_dirty, keep_open = _gc_fleet(root, monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not delete anything")

    monkeypatch.setattr(tree_verb, "remove_tree", boom)

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
    # REMOVEs. Both modes share _scan_and_classify, so the preview can never drift.
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


def _capture_classify_kwargs(monkeypatch) -> dict:
    """Spy on cleanup.classify: record its kwargs, return an empty partition."""
    seen: dict = {}

    def fake_classify(records, *, now, pr_states, **kwargs):
        seen["max_age_seconds"] = kwargs.get("max_age_seconds")
        return tree_verb.Cleanup(removable=[], stale=[], keep=[])

    monkeypatch.setattr(tree_verb.cleanup, "classify", fake_classify)
    return seen


def test_run_gc_threshold_overrides_the_age_boundary(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: [])
    seen = _capture_classify_kwargs(monkeypatch)

    rc = tree_verb.run_gc(threshold="36h")

    assert rc == 0
    assert seen["max_age_seconds"] == 36 * 3600


def test_run_gc_default_threshold_is_two_weeks(tmp_path, monkeypatch, capsys):
    # Omitting --threshold passes the 14-day default through to classify unchanged.
    root = tmp_path / "trees"
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: [])
    seen = _capture_classify_kwargs(monkeypatch)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert seen["max_age_seconds"] == tree_verb.cleanup.DEFAULT_MAX_AGE_SECONDS


def test_run_gc_bad_threshold_is_clean_exit_1(tmp_path, monkeypatch, capsys):
    # A malformed --threshold is a clean message, never a traceback — and never a sweep.
    root = tmp_path / "trees"
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))

    def boom(_r):
        raise AssertionError("must not scan when the threshold is invalid")

    monkeypatch.setattr(tree_verb.registry, "scan", boom)

    rc = tree_verb.run_gc(threshold="nope")

    assert rc == 1
    assert "tree gc:" in capsys.readouterr().err


def test_pr_state_normalizes_draft(monkeypatch):
    # A draft open PR reads as "DRAFT" (one fleet-wide vocabulary, mirroring the
    # registry label), not the raw "OPEN" GitHub state.
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: {"number": 7, "state": "OPEN", "isDraft": True},
    )
    record = _record(path="/trees/x", branch="b1")

    assert tree_verb._pr_state(record) == "DRAFT"


def test_pr_state_unknown_when_gh_state_unreadable(monkeypatch):
    # An unreadable PR state (gh.pr_for_head -> UNKNOWN) surfaces as the "UNKNOWN"
    # string, distinct from None (no branch / no PR), so gc can both treat it
    # conservatively and warn.
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: gh.UNKNOWN)
    record = _record(path="/trees/x", branch="b1")

    assert tree_verb._pr_state(record) == "UNKNOWN"


def test_pr_state_none_when_no_branch_or_no_pr(monkeypatch):
    # No branch -> None without even hitting gh; a branch with no PR -> None too.
    assert tree_verb._pr_state(_record(path="/trees/x", branch=None)) is None
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)
    assert tree_verb._pr_state(_record(path="/trees/y", branch="b1")) is None


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
        "b1": {"number": 1, "state": "MERGED", "isDraft": False},
        "b2": gh.UNKNOWN,
    }
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
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
    # The merged interaction (WS01 dry-run + WS03 UNKNOWN): a --dry-run preview over a
    # fleet that contains an unreadable-state Tree must still surface the incomplete-view
    # warning, yet touch nothing on disk. The UNKNOWN Tree lands in STALE (conservative).
    root = tmp_path / "trees"
    merged = _make_tree_dir(root, "acme/widget/issues/1/work-merged")
    unknown = _make_tree_dir(root, "acme/widget/issues/2/work-unknown")
    aged = 0.0
    records = [
        _record(path=str(merged), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(unknown), branch="b2", dirty=False, ahead=0, mtime=aged),
    ]
    pr_by_branch = {
        "b1": {"number": 1, "state": "MERGED", "isDraft": False},
        "b2": gh.UNKNOWN,
    }
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )

    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not delete anything")

    monkeypatch.setattr(tree_verb, "remove_tree", boom)

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
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: {"number": 1, "state": "MERGED", "isDraft": False},
    )

    rc = tree_verb.run_gc()

    assert rc == 0
    captured = capsys.readouterr()
    assert "skipped (state unknown)" not in captured.err
    assert "skipped (state unknown)" not in captured.out


# --- ephemeral kind as first-class fleet state (SES02, ADR-0027) ----------------


def test_run_list_renders_the_kind_column(monkeypatch, capsys):
    records = [
        _record(),  # issues/<id>/... -> write
        _record(
            path="/trees/acme/widget/review/tre03-ws03",
            branch="TRE03/WS03",
            pr=None,
        ),
        _record(
            path="/trees/acme/widget/ephemeral/sess-1",
            branch="ephemeral/sess-1",
            base=None,
            pr=None,
        ),
    ]
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/trees")
    monkeypatch.setattr(tree_verb.registry, "scan", lambda root: records)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "KIND" in out
    rows = {line.split()[0]: line.split()[1] for line in out.splitlines()[1:]}
    assert rows["/trees/acme/widget/issues/7/work-aaaa"] == "write"
    assert rows["/trees/acme/widget/review/tre03-ws03"] == "review"
    assert rows["/trees/acme/widget/ephemeral/sess-1"] == "ephemeral"


def _ephemeral_clone(root, leaf: str) -> str:
    tree = root / "acme" / "widget" / "ephemeral" / leaf
    (tree / ".git").mkdir(parents=True)
    return str(tree)


def test_run_gc_keeps_a_live_session_and_reclaims_a_dead_one(
    tmp_path, monkeypatch, capsys
):
    # End to end through the gc verb: liveness comes from the pidfile + probe, and
    # the ephemeral ladder keeps the live session's Tree while reclaiming the dead
    # one (both clean, pushed, and past the grace window).
    import time as _time

    from shipit.session import liveness

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
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))

    past_grace = _time.time() - (tree_verb.cleanup.EPHEMERAL_GRACE_SECONDS + 60)
    records = [
        _record(
            path=live_path,
            branch="ephemeral/sess-live",
            base=None,
            pr=None,
            unpushed=0,
            mtime=past_grace,
        ),
        _record(
            path=dead_path,
            branch="ephemeral/sess-dead",
            base=None,
            pr=None,
            unpushed=0,
            mtime=past_grace,
        ),
    ]
    monkeypatch.setattr(tree_verb.registry, "scan", lambda root: records)
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    rc = tree_verb.run_gc(dry_run=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"KEEP      {live_path}" in out
    assert f"REMOVABLE {dead_path}" in out
