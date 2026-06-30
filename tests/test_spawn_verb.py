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
from shipit.tree import layout
from shipit.tree.create import Tree
from shipit.verbs import spawn as spawn_verb


def _patch_identity(monkeypatch, *, root="/repo", org_repo="acme/widget"):
    """Mock the gh boundary so run_subagent resolves a fixed repo identity.

    Also stubs ``remote_branch_exists`` → True: the write path fail-closes on the
    epic umbrella branch existing on the remote (#176), so a happy-path test must see
    it present. Tests that exercise the MISSING-branch fail-closed path override it.
    """
    monkeypatch.setattr(gh, "repo_root", lambda: root)
    monkeypatch.setattr(gh, "current_repo", lambda: org_repo)
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:" + org_repo)
    monkeypatch.setattr(gh, "remote_branch_exists", lambda *a, **k: True)


def _fake_create(monkeypatch, tree_dir: Path) -> dict:
    """Replace the orchestrator with a spy that 'creates' a Tree at ``tree_dir``.

    Returns the dict the spec/args are recorded into. The dir is made on disk so the
    Tree the verb roots the launch in (and resolves the PR from) is a real path. The
    branch/base are resolved from the spec through the REAL pure planner
    (:func:`shipit.tree.layout.plan`), so the fake reflects the true epic-grouped
    base (``origin/E/umbrella``) the verb now selects — the PR-target check in the
    verb reads ``tree.base``, so a hardcoded base would not exercise it faithfully.
    """
    captured: dict = {}

    def fake_create(spec, *, source_repo, github_url):
        captured["spec"] = spec
        captured["source_repo"] = source_repo
        captured["github_url"] = github_url
        tree_dir.mkdir(parents=True, exist_ok=True)
        tp = layout.plan(spec)
        return Tree(path=str(tree_dir), branch=tp.branch, base=tp.base)

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
        {
            "number": 321,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": "TRE03/umbrella",
        },
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
    # The Tree was created via the reused path with the EPIC shape (#176): the
    # slash-namespaced E/WSnn branch cut from the epic-grouped umbrella base
    # (origin/E/umbrella), NOT the dumb origin/main — so the draft PR targets the
    # epic branch.
    spec = captured["spec"]
    assert spec.org == "acme" and spec.repo == "widget"
    assert spec.epic == "TRE03" and spec.ws == 1
    assert spec.issue is None and spec.branch is None
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
    assert payload["base"] == "origin/TRE03/umbrella"
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
        {
            "number": 7,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": "TRE03/umbrella",
        },
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


def test_run_subagent_resolves_epic_grouped_base_and_pr_target(
    tmp_path, monkeypatch, capsys
):
    # #176: --epic E --ws N resolves the epic-grouped base. The umbrella branch's
    # existence is checked against the remote (E/umbrella, read from the source repo),
    # the TreeSpec is the EPIC shape (so create cuts from origin/E/umbrella), and the
    # Run's draft PR must target the epic branch E/umbrella — never main.
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, root=str(parent))
    captured = _fake_create(monkeypatch, tree_dir)
    checked: dict = {}

    def fake_exists(branch, *, cwd=None, remote="origin"):
        checked["branch"] = branch
        checked["cwd"] = cwd
        return True

    monkeypatch.setattr(gh, "remote_branch_exists", fake_exists)
    runner, _calls = _launcher()
    _patch_pr(
        monkeypatch,
        {
            "number": 42,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": "TRE04/umbrella",
        },
    )

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE04",
        ws=7,
        issue=200,
        role="implementer",
        launcher=runner,
    )

    assert rc == 0
    # Fail-closed precondition checked the EPIC umbrella branch on the remote, read
    # from the source checkout.
    assert checked["branch"] == "TRE04/umbrella"
    assert checked["cwd"] == str(parent)
    # The spec is the EPIC shape, so the real planner resolves origin/E/umbrella.
    spec = captured["spec"]
    assert spec.epic == "TRE04" and spec.ws == 7
    assert layout.plan(spec).base == "origin/TRE04/umbrella"
    out = capsys.readouterr().out
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["base"] == "origin/TRE04/umbrella"


def test_run_subagent_missing_epic_branch_fails_closed_no_main_fallback(
    tmp_path, monkeypatch, capsys
):
    # #176 fail-closed: --epic E with NO origin/E/umbrella on the remote must exit 1
    # LOUD and NEVER silently fall back to origin/main. The Tree is never created and
    # nothing is launched — the precondition gates before any side effect.
    _patch_identity(monkeypatch)
    monkeypatch.setattr(gh, "remote_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        spawn_verb,
        "create",
        lambda *a, **k: pytest.fail("must not create a Tree on a missing epic base"),
    )

    launched: dict = {}

    def runner(cmd, *, cwd, env):
        launched["called"] = True
        return launch.LaunchResult(0, "", "")

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE04",
        ws=1,
        issue=1,
        role="implementer",
        launcher=runner,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "TRE04/umbrella" in err
    assert "does not exist" in err
    assert "origin/main" in err  # the diagnostic names the refused fallback
    assert "called" not in launched  # nothing launched


@pytest.mark.parametrize("bad_epic", ["", "   ", "TRE/04", "..", "TRE 04"])
def test_run_subagent_invalid_epic_is_clean_exit_1(
    tmp_path, monkeypatch, capsys, bad_epic
):
    # An invalid/empty epic code is not a single alphanumeric token, so the pure
    # `epic_umbrella_base` helper raises ValueError. The verb must catch that and
    # return a clean exit-1 with a stderr diagnostic — never let the traceback escape
    # (the verb's "never a traceback" contract). The precondition gates before any
    # side effect: no Tree is created and nothing is launched.
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        spawn_verb,
        "create",
        lambda *a, **k: pytest.fail("must not create a Tree on an invalid epic code"),
    )

    launched: dict = {}

    def runner(cmd, *, cwd, env):
        launched["called"] = True
        return launch.LaunchResult(0, "", "")

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic=bad_epic,
        ws=1,
        issue=1,
        role="implementer",
        launcher=runner,
    )

    assert rc == 1  # a clean exit code, NOT an escaping ValueError
    err = capsys.readouterr().err
    assert "spawn subagent:" in err
    assert "epic code" in err  # the helper's diagnostic is surfaced
    assert "called" not in launched  # nothing launched


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
        backend="nonexistent",
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "unsupported backend" in err and "nonexistent" in err


def test_run_subagent_non_positive_ws_is_exit_1(monkeypatch, capsys):
    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=0, issue=1, role="implementer"
    )

    assert rc == 1
    assert "--ws must be a positive integer" in capsys.readouterr().err


@pytest.mark.parametrize("bad_issue", [0, -1, None])
def test_run_subagent_non_positive_issue_is_exit_1(monkeypatch, capsys, bad_issue):
    # --issue feeds the task prompt and the PR's `for #<issue>` link; a zero/negative
    # value (which click's int type accepts) — OR a MISSING value (None) for a write
    # role — is refused inside run_subagent before any Tree/child work. The CLI layer
    # makes --issue optional (so reviewer spawns aren't rejected), so this write-run
    # requirement is enforced here, not at the Click boundary.
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
        {
            "number": 5,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": "TRE03/umbrella",
        },
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
    # one (origin/E/umbrella -> "TRE03/umbrella"). Reporting back against the wrong
    # base is invalid, so the spawn is a clean exit-1.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0)
    _patch_pr(
        monkeypatch,
        {"number": 9, "state": "OPEN", "isDraft": True, "baseRefName": "main"},
    )

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, issue=1, role="implementer", launcher=runner
    )

    assert rc == 1
    out = capsys.readouterr()
    assert "targets base 'main'" in out.err
    assert "not the intended 'TRE03/umbrella'" in out.err
    assert "SPAWNED" not in out.out


def _fake_create_readonly(monkeypatch, tree_dir: Path) -> dict:
    """Replace the read-only orchestrator with a spy that 'creates' a Tree at ``tree_dir``.

    Mirrors :func:`_fake_create` for the reviewer path: records the plan/args and makes
    the dir on disk so the launcher has a real cwd to act on. Returns the capture dict.
    """
    captured: dict = {}

    def fake(plan, *, source_repo, github_url):
        captured["plan"] = plan
        captured["source_repo"] = source_repo
        captured["github_url"] = github_url
        tree_dir.mkdir(parents=True, exist_ok=True)
        return Tree(
            path=str(tree_dir), branch=plan.branch, base=f"origin/{plan.branch}"
        )

    monkeypatch.setattr(spawn_verb, "create_readonly", fake)
    return captured


def test_run_subagent_reviewer_provisions_readonly_tree_and_posts_review(
    tmp_path, monkeypatch, capsys
):
    # Acceptance #157: --role reviewer takes the read-only path (create_readonly, NOT
    # the write create), launches with --agent reviewer + the read-only --tools
    # allow-list, and reports SPAWNED. No sentinel is required — the review lands in
    # the PR (the fake launcher writes none, yet the Run still succeeds).
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "review"
    _patch_identity(monkeypatch, root=str(parent))
    captured = _fake_create_readonly(monkeypatch, tree_dir)
    # If the WRITE path were taken this would fire — the reviewer must not use it.
    monkeypatch.setattr(
        spawn_verb,
        "create",
        lambda *a, **k: pytest.fail("reviewer must not create a write Tree"),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    runner, calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=3, role="reviewer", launcher=runner
    )

    assert rc == 0
    # The read-only plan is shared per (repo, branch): the WS PR head, no agent hash.
    plan = captured["plan"]
    assert plan.branch == "TRE03/WS03"
    # The leaf is the sanitized branch plus a stable branch-name hash disambiguator.
    assert plan.dir.name.startswith("tre03-ws03-")
    assert plan.dir.parent.name == "review"
    assert captured["source_repo"] == str(parent)
    # Launch contract for a reviewer: cwd = the read-only Tree, --agent reviewer, the
    # read-only --tools allow-list (no Write), key scrubbed.
    assert calls["cwd"] == str(tree_dir)
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "reviewer"
    allowlist = calls["cmd"][calls["cmd"].index("--tools") + 1]
    assert "Write" not in allowlist and "Edit" not in allowlist
    assert "ANTHROPIC_API_KEY" not in calls["env"]
    # SPAWNED summary: role reviewer, and NO sentinel key (the Run reports via the PR).
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["role"] == "reviewer"
    assert payload["branch"] == "TRE03/WS03"
    assert "sentinel" not in payload


def test_run_subagent_codex_reviewer_launches_with_read_only_posture(
    tmp_path, monkeypatch, capsys
):
    # #185 bullet 1: a non-Claude reviewer (--backend codex --role reviewer) takes the
    # SAME shared read-only Tree path and launches with the codex reviewer posture —
    # the network-capable workspace-write sandbox, NOT the write bypass flag. The verb
    # passes read_only=True; the adapter builds the argv. The chmod'd Tree (asserted in
    # tests/test_tree_readonly.py) is the FS guard.
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "review"
    _patch_identity(monkeypatch, root=str(parent))
    _fake_create_readonly(monkeypatch, tree_dir)
    monkeypatch.setattr(
        spawn_verb,
        "create",
        lambda *a, **k: pytest.fail("reviewer must not create a write Tree"),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "stale")
    runner, calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE03",
        ws=3,
        role="reviewer",
        backend="codex",
        launcher=runner,
    )

    assert rc == 0
    cmd = calls["cmd"]
    assert cmd[:2] == ["codex", "exec"]
    # Reviewer posture: network-capable sandbox, no write bypass, no --tools.
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--tools" not in cmd
    # Rooted in the read-only Tree, codex auth scrubbed.
    assert calls["cwd"] == str(tree_dir)
    assert "OPENAI_API_KEY" not in calls["env"]
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["role"] == "reviewer"
    assert payload["backend"] == "codex"


def test_run_subagent_antigravity_reviewer_drops_skip_permissions(
    tmp_path, monkeypatch, capsys
):
    # The agy reviewer path: read_only=True drops --dangerously-skip-permissions and is
    # rooted in the read-only Tree via --add-dir <Tree> (agy ignores the process cwd).
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "review"
    _patch_identity(monkeypatch, root=str(parent))
    _fake_create_readonly(monkeypatch, tree_dir)
    monkeypatch.setenv("GEMINI_API_KEY", "stale")
    runner, calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="widget",
        epic="TRE03",
        ws=3,
        role="reviewer",
        backend="antigravity",
        launcher=runner,
    )

    assert rc == 0
    cmd = calls["cmd"]
    assert cmd[0] == "agy"
    assert "--dangerously-skip-permissions" not in cmd
    # agy is rooted ONLY by --add-dir <Tree> (it ignores the process cwd).
    assert cmd[cmd.index("--add-dir") + 1] == str(tree_dir)
    assert "GEMINI_API_KEY" not in calls["env"]


def test_run_subagent_reviewer_readonly_tree_failure_is_clean_exit_1(
    tmp_path, monkeypatch, capsys
):
    # Fail-closed for the reviewer path too: a read-only-Tree error exits 1 loud, and
    # the launcher is never reached.
    _patch_identity(monkeypatch)

    def boom(plan, *, source_repo, github_url):
        raise gh.GhError("clone failed")

    monkeypatch.setattr(spawn_verb, "create_readonly", boom)
    launched: dict = {}

    def runner(cmd, *, cwd, env):
        launched["called"] = True
        return launch.LaunchResult(0, "", "")

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=3, role="reviewer", launcher=runner
    )

    assert rc == 1
    assert "read-only tree creation failed" in capsys.readouterr().err
    assert "called" not in launched


def test_run_subagent_reviewer_child_nonzero_exit_is_exit_1(
    tmp_path, monkeypatch, capsys
):
    tree_dir = tmp_path / "review"
    _patch_identity(monkeypatch)
    _fake_create_readonly(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=3)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=3, role="reviewer", launcher=runner
    )

    assert rc == 1
    assert "claude child exited 3" in capsys.readouterr().err


def test_spawn_subagent_help_documents_the_verb():
    from click.testing import CliRunner

    result = CliRunner().invoke(spawn_verb.spawn, ["subagent", "--help"])

    assert result.exit_code == 0
    for token in ("--repo", "--epic", "--ws", "--issue", "--role", "--backend"):
        assert token in result.output
    assert "Tree" in result.output


def test_cli_reviewer_spawn_without_issue_reaches_run_subagent(monkeypatch):
    # The bug: --issue was required=True at the Click layer, so a reviewer spawn (which
    # needs no issue — the dogfood harness invokes `spawn subagent --role reviewer`
    # WITHOUT --issue) was rejected with a usage error (exit 2) before run_subagent ran.
    # The CLI must now accept it and hand issue=None to run_subagent.
    from click.testing import CliRunner

    seen: dict = {}

    def fake_run_subagent(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(spawn_verb, "run_subagent", fake_run_subagent)

    result = CliRunner().invoke(
        spawn_verb.spawn,
        [
            "subagent",
            "--repo",
            "widget",
            "--epic",
            "TRE03",
            "--ws",
            "3",
            "--role",
            "reviewer",
        ],
    )

    assert result.exit_code == 0  # NOT a click usage error (2)
    assert seen["role"] == "reviewer"
    assert seen["issue"] is None  # no --issue reached run_subagent as None


def test_cli_write_spawn_without_issue_is_not_a_usage_error(monkeypatch):
    # A write role with no --issue must NOT be a Click usage error (exit 2) either: the
    # requirement moved into run_subagent, which fails it cleanly (exit 1) with the
    # positive-integer message. The CLI boundary just forwards issue=None.
    from click.testing import CliRunner

    seen: dict = {}

    def fake_run_subagent(**kwargs):
        seen.update(kwargs)
        return 1

    monkeypatch.setattr(spawn_verb, "run_subagent", fake_run_subagent)

    result = CliRunner().invoke(
        spawn_verb.spawn,
        [
            "subagent",
            "--repo",
            "widget",
            "--epic",
            "TRE03",
            "--ws",
            "3",
            "--role",
            "implementer",
        ],
    )

    assert result.exit_code == 1  # run_subagent's clean exit, NOT click's usage exit 2
    assert seen["role"] == "implementer"
    assert seen["issue"] is None
