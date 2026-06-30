"""Unit tests for the ``shipit spawn subagent`` verb handler (``run_subagent``).

The verb is thin glue: resolve repo identity at the gh boundary, REUSE the
tree-creation path, launch a headless ``claude`` child rooted in the Tree
(ADR-0019), then resolve the PR the Run opened on the Tree's branch (WS02). These
tests mock the ``gh``/``create`` boundary and inject a fake launcher so they pin the
glue — the launch contract (cwd = Tree, ``--agent <role>``, ``ANTHROPIC_API_KEY``
scrubbed), the Run↔PR linkage resolved from the branch, the fail-closed Tree-
creation path, and the exit codes — without touching real git or spawning claude.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipit import gh, proc
from shipit.spawn import launch
from shipit.tree.create import Tree
from shipit.verbs import spawn as spawn_verb


def _patch_identity(monkeypatch, *, root="/repo", org_repo="acme/widget"):
    """Mock the gh boundary so run_subagent resolves a fixed repo identity."""
    monkeypatch.setattr(gh, "repo_root", lambda: root)
    monkeypatch.setattr(gh, "current_repo", lambda: org_repo)
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:" + org_repo)


def _fake_create(monkeypatch, tree_dir: Path) -> dict:
    """Replace the orchestrator with a spy that 'creates' a Tree at ``tree_dir``.

    Returns the dict the spec/args are recorded into. The dir is made on disk so the
    Tree the verb roots the launch in (and resolves the PR from) is a real path.
    """
    captured: dict = {}

    def fake_create(spec, *, source_repo, github_url):
        captured["spec"] = spec
        captured["source_repo"] = source_repo
        captured["github_url"] = github_url
        tree_dir.mkdir(parents=True, exist_ok=True)
        return Tree(path=str(tree_dir), branch=spec.branch, base="origin/main")

    monkeypatch.setattr(spawn_verb, "create", fake_create)
    return captured


def _launcher(*, returncode=0):
    """A fake launcher recording its call (never touches the Tree — the Run is faked)."""
    calls: dict = {}

    def runner(cmd, *, cwd, env):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        return launch.LaunchResult(returncode=returncode, stdout="{}", stderr="boom")

    return runner, calls


def _patch_pr(monkeypatch, pr):
    """Patch ``gh.pr_for_head`` (the Run↔PR resolution boundary) and record its args.

    ``pr`` is the resolution result the verb sees: a snapshot dict (a PR was opened
    on the branch), ``None`` (the branch provably has no PR), or ``gh.UNKNOWN`` (the
    state is undetermined). Returns the dict the call's ``branch``/``cwd`` land in so
    a test can assert the link is resolved from the *Tree's* branch and cwd.
    """
    seen: dict = {}

    def fake_pr_for_head(branch, *, cwd=None):
        seen["branch"] = branch
        seen["cwd"] = cwd
        return pr

    monkeypatch.setattr(gh, "pr_for_head", fake_pr_for_head)
    return seen


def test_run_subagent_happy_path(tmp_path, monkeypatch, capsys):
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, root=str(parent))
    captured = _fake_create(monkeypatch, tree_dir)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    runner, calls = _launcher()
    _patch_pr(
        monkeypatch,
        {"number": 321, "state": "OPEN", "isDraft": True, "baseRefName": "main"},
    )

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE03",
        ws=1,
        issue=156,
        role="implementer",
        launcher=runner,
    )

    assert rc == 0
    # The Tree was created via the reused path, with the dumb origin/main base and
    # the slash-namespaced E/WSnn branch (freeform shape).
    spec = captured["spec"]
    assert spec.org == "acme" and spec.repo == "widget"
    assert spec.branch == "TRE03/WS01"
    assert spec.issue is None and spec.epic is None and spec.ws is None
    assert captured["source_repo"] == str(parent)
    # The launch contract: cwd IS the Tree, the role rides --agent, the key is gone.
    assert calls["cwd"] == str(tree_dir)
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "implementer"
    assert "ANTHROPIC_API_KEY" not in calls["env"]
    # The task tells the Run which issue to implement and the branch to PR from.
    task = calls["cmd"][calls["cmd"].index("-p") + 1]
    assert "#156" in task and "TRE03/WS01" in task
    # The SPAWNED summary reports the Run's coordinates AND the Run↔PR linkage.
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["tree"] == str(tree_dir)
    assert payload["branch"] == "TRE03/WS01"
    assert payload["base"] == "origin/main"
    assert payload["role"] == "implementer"
    assert payload["backend"] == "claude"
    assert payload["pr"] == 321
    assert payload["pr_state"] == "OPEN"
    assert payload["pr_is_draft"] is True


def test_run_subagent_links_pr_from_the_tree_branch(tmp_path, monkeypatch, capsys):
    # Acceptance #156: the Run↔PR link is resolved from the *Tree's* branch, read
    # inside the Tree (cwd) — the PR on the branch IS the link, no side database. And
    # the launch is rooted in the Tree (cwd), so the Run never runs in the parent.
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, root=str(parent))
    _fake_create(monkeypatch, tree_dir)
    runner, calls = _launcher()
    seen = _patch_pr(
        monkeypatch,
        {"number": 7, "state": "OPEN", "isDraft": True, "baseRefName": "main"},
    )

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE03",
        ws=2,
        issue=99,
        role="implementer",
        launcher=runner,
    )

    assert rc == 0
    assert calls["cwd"] == str(tree_dir)  # the Run is rooted in the Tree
    assert seen["branch"] == "TRE03/WS02"  # link resolved from the Tree branch
    assert seen["cwd"] == str(tree_dir)  # ...read from inside the Tree


def test_run_subagent_tree_creation_failure_fails_closed(tmp_path, monkeypatch, capsys):
    # Fail-closed (ADR-0017/0019): a Tree-creation error fails the spawn loud, and
    # NEVER falls back to launching anything — the launcher must not be called.
    _patch_identity(monkeypatch)

    def boom(spec, *, source_repo, github_url):
        raise gh.GhError("clone failed")

    monkeypatch.setattr(spawn_verb, "create", boom)

    launched: dict = {}

    def runner(cmd, *, cwd, env):
        launched["called"] = True
        return launch.LaunchResult(0, "", "")

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    assert "tree creation failed" in capsys.readouterr().err
    assert "called" not in launched  # no fallback launch


@pytest.mark.parametrize(
    "exc",
    [
        proc.ProcError(["pixi", "install"], 1, "boom"),  # provisioning failed
        OSError("disk full"),  # a filesystem step failed
        ValueError("planner rejected the spec"),  # the planner refused
        FileExistsError("tree dir already exists"),
    ],
)
def test_run_subagent_maps_create_failures_to_clean_exit_1(monkeypatch, capsys, exc):
    _patch_identity(monkeypatch)

    def boom(spec, *, source_repo, github_url):
        raise exc

    monkeypatch.setattr(spawn_verb, "create", boom)
    runner, _calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    assert "spawn subagent: tree creation failed" in capsys.readouterr().err


def test_run_subagent_unsupported_backend_is_exit_1(monkeypatch, capsys):
    # The backend gate fires before any repo resolution or Tree creation.
    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE03",
        ws=1,
        issue=1,
        role="implementer",
        backend="codex",
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "unsupported backend" in err and "codex" in err


def test_run_subagent_non_positive_ws_is_exit_1(monkeypatch, capsys):
    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=0, issue=1, role="implementer"
    )

    assert rc == 1
    assert "--ws must be a positive integer" in capsys.readouterr().err


@pytest.mark.parametrize("bad_issue", [0, -1])
def test_run_subagent_non_positive_issue_is_exit_1(monkeypatch, capsys, bad_issue):
    # --issue feeds the task prompt and the PR's `for #<issue>` link; a zero/negative
    # value (which click's int type accepts) is refused before any Tree/child work.
    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=bad_issue, role="implementer"
    )

    assert rc == 1
    assert "--issue must be a positive integer" in capsys.readouterr().err


def test_run_subagent_repo_mismatch_is_exit_1(monkeypatch, capsys):
    _patch_identity(monkeypatch, org_repo="acme/widget")

    rc = spawn_verb.run_subagent(
        repo="gadget", epic="TRE03", ws=1, issue=1, role="implementer"
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "--repo 'gadget'" in err and "acme/widget" in err


def test_run_subagent_slashless_ambient_repo_is_exit_1(monkeypatch, capsys):
    # A slashless ambient identity would put the whole string in ``org`` and leave
    # ``repo_name`` empty, which could slip past the --repo guard and feed an empty
    # repo into the TreeSpec. It must be refused with a clean exit-1.
    _patch_identity(monkeypatch, org_repo="widget")

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer"
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "not in org/repo form" in err
    assert "widget" in err


def test_run_subagent_repo_accepts_org_qualified_name(tmp_path, monkeypatch, capsys):
    # --repo may be given as either the bare name or the full org/repo slug.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, org_repo="acme/widget")
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher()
    _patch_pr(
        monkeypatch,
        {"number": 5, "state": "OPEN", "isDraft": True, "baseRefName": "main"},
    )

    rc = spawn_verb.run_subagent(
        repo="acme/widget",
        epic="TRE03",
        ws=1,
        issue=1,
        role="implementer",
        launcher=runner,
    )

    assert rc == 0


def test_run_subagent_not_inside_checkout_is_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: None)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer"
    )

    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_run_subagent_reports_gh_error_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")

    def boom():
        raise gh.GhError("could not resolve repo")

    monkeypatch.setattr(gh, "current_repo", boom)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer"
    )

    assert rc == 1
    assert "spawn subagent:" in capsys.readouterr().err


def test_run_subagent_child_nonzero_exit_is_exit_1(tmp_path, monkeypatch, capsys):
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=2)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "claude child exited 2" in err
    assert "boom" in err  # the child's stderr is surfaced, not swallowed


def test_run_subagent_launch_oserror_is_clean_exit_1(tmp_path, monkeypatch, capsys):
    # The child never starts — `claude` is not installed / not on PATH, so the
    # launcher raises FileNotFoundError (an OSError). The Tree already exists, so this
    # is a launch failure, and run_subagent promises a clean exit-1 with a stderr
    # message, never an escaping traceback.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)

    def runner(cmd, *, cwd, env):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'claude'")

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "spawn subagent:" in err
    assert "claude" in err


def test_run_subagent_no_pr_on_branch_is_exit_1(tmp_path, monkeypatch, capsys):
    # A child that exits 0 but opened NO PR on the Tree's branch did not report back
    # (acceptance #156): the Run↔PR link is absent, so the spawn is a clean exit-1.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0)
    _patch_pr(monkeypatch, None)  # gh: provably no PR for this branch

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    assert "opened no PR" in capsys.readouterr().err


def test_run_subagent_unknown_pr_state_is_exit_1(tmp_path, monkeypatch, capsys):
    # An UNDETERMINED PR state (gh unreadable — auth/network) must NOT masquerade as
    # success: the verb cannot claim the Run reported back, so it is a clean exit-1.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0)
    _patch_pr(monkeypatch, gh.UNKNOWN)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    assert "could not be read" in capsys.readouterr().err


def test_run_subagent_non_open_pr_is_exit_1(tmp_path, monkeypatch, capsys):
    # A PR exists on the branch, but it is CLOSED — an invalid lifecycle state. The Run
    # did not report back through an OPEN draft PR, so the spawn is a clean exit-1 and
    # emits no SPAWNED line.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0)
    _patch_pr(
        monkeypatch,
        {"number": 9, "state": "CLOSED", "isDraft": True, "baseRefName": "main"},
    )

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    out = capsys.readouterr()
    assert "is CLOSED, not OPEN" in out.err
    assert "SPAWNED" not in out.out


def test_run_subagent_non_draft_pr_is_exit_1(tmp_path, monkeypatch, capsys):
    # The PR is OPEN but already flipped to ready-for-review — the draft turn-signal the
    # coordinator drives is gone. That is an invalid handoff state -> clean exit-1.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0)
    _patch_pr(
        monkeypatch,
        {"number": 9, "state": "OPEN", "isDraft": False, "baseRefName": "main"},
    )

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    out = capsys.readouterr()
    assert "is not a draft" in out.err
    assert "SPAWNED" not in out.out


def test_run_subagent_wrong_base_pr_is_exit_1(tmp_path, monkeypatch, capsys):
    # The PR is OPEN and draft, but targets a DIFFERENT base than the Tree's intended
    # one (origin/main -> "main"). Reporting back against the wrong base is invalid, so
    # the spawn is a clean exit-1.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0)
    _patch_pr(
        monkeypatch,
        {"number": 9, "state": "OPEN", "isDraft": True, "baseRefName": "develop"},
    )

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    out = capsys.readouterr()
    assert "targets base 'develop'" in out.err
    assert "not the intended 'main'" in out.err
    assert "SPAWNED" not in out.out


def test_spawn_subagent_help_documents_the_verb():
    from click.testing import CliRunner

    result = CliRunner().invoke(spawn_verb.spawn, ["subagent", "--help"])

    assert result.exit_code == 0
    for token in ("--repo", "--epic", "--ws", "--issue", "--role", "--backend"):
        assert token in result.output
    assert "Tree" in result.output
