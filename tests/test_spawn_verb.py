"""Unit tests for the ``shipit spawn subagent`` verb handler (``run_subagent``).

The verb is thin glue: resolve repo identity at the gh boundary, REUSE the
tree-creation path, then launch a headless ``claude`` child rooted in the Tree
(ADR-0019). These tests mock the ``gh``/``create`` boundary and inject a fake
launcher so they pin the glue — the launch contract (cwd = Tree, ``--agent <role>``,
``ANTHROPIC_API_KEY`` scrubbed), the sentinel observation, the fail-closed Tree-
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
    launcher's sentinel write (and the verb's observation) have a real Tree to act on.
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


def _launcher(*, returncode=0, write_sentinel=True):
    """A fake launcher recording its call; optionally writes the sentinel into cwd."""
    calls: dict = {}

    def runner(cmd, *, cwd, env):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        if write_sentinel:
            (Path(cwd) / launch.SENTINEL_NAME).write_text(launch.SENTINEL_BODY)
        return launch.LaunchResult(returncode=returncode, stdout="{}", stderr="boom")

    return runner, calls


def test_run_subagent_happy_path(tmp_path, monkeypatch, capsys):
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, root=str(parent))
    captured = _fake_create(monkeypatch, tree_dir)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    runner, calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, role="implementer", launcher=runner
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
    # The SPAWNED summary reports the Run's coordinates.
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["tree"] == str(tree_dir)
    assert payload["branch"] == "TRE03/WS01"
    assert payload["base"] == "origin/main"
    assert payload["role"] == "implementer"
    assert payload["backend"] == "claude"


def test_run_subagent_work_lands_in_tree_not_parent(tmp_path, monkeypatch, capsys):
    # Acceptance #155: the child's work demonstrably happens in the Tree, not the
    # parent checkout. The fake launcher writes the sentinel into the cwd it is given;
    # we assert it lands in the Tree and never leaks to the parent root.
    parent = tmp_path / "repo"
    parent.mkdir()
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, root=str(parent))
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=2, role="implementer", launcher=runner
    )

    assert rc == 0
    assert (tree_dir / launch.SENTINEL_NAME).is_file()  # ran in the Tree
    assert not (parent / launch.SENTINEL_NAME).exists()  # no leak to the parent


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
        repo="widget", epic="TRE03", ws=1, role="implementer", launcher=runner
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
        repo="widget", epic="TRE03", ws=1, role="implementer", launcher=runner
    )

    assert rc == 1
    assert "spawn subagent: tree creation failed" in capsys.readouterr().err


def test_run_subagent_unsupported_backend_is_exit_1(monkeypatch, capsys):
    # The backend gate fires before any repo resolution or Tree creation.
    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, role="implementer", backend="codex"
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "unsupported backend" in err and "codex" in err


def test_run_subagent_non_positive_ws_is_exit_1(monkeypatch, capsys):
    rc = spawn_verb.run_subagent(repo="widget", epic="TRE03", ws=0, role="implementer")

    assert rc == 1
    assert "--ws must be a positive integer" in capsys.readouterr().err


def test_run_subagent_repo_mismatch_is_exit_1(monkeypatch, capsys):
    _patch_identity(monkeypatch, org_repo="acme/widget")

    rc = spawn_verb.run_subagent(repo="gadget", epic="TRE03", ws=1, role="implementer")

    assert rc == 1
    err = capsys.readouterr().err
    assert "--repo 'gadget'" in err and "acme/widget" in err


def test_run_subagent_repo_accepts_org_qualified_name(tmp_path, monkeypatch, capsys):
    # --repo may be given as either the bare name or the full org/repo slug.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch, org_repo="acme/widget")
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher()

    rc = spawn_verb.run_subagent(
        repo="acme/widget", epic="TRE03", ws=1, role="implementer", launcher=runner
    )

    assert rc == 0


def test_run_subagent_not_inside_checkout_is_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: None)

    rc = spawn_verb.run_subagent(repo="widget", epic="TRE03", ws=1, role="implementer")

    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_run_subagent_reports_gh_error_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")

    def boom():
        raise gh.GhError("could not resolve repo")

    monkeypatch.setattr(gh, "current_repo", boom)

    rc = spawn_verb.run_subagent(repo="widget", epic="TRE03", ws=1, role="implementer")

    assert rc == 1
    assert "spawn subagent:" in capsys.readouterr().err


def test_run_subagent_child_nonzero_exit_is_exit_1(tmp_path, monkeypatch, capsys):
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=2, write_sentinel=False)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, role="implementer", launcher=runner
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "claude child exited 2" in err
    assert "boom" in err  # the child's stderr is surfaced, not swallowed


def test_run_subagent_no_sentinel_is_exit_1(tmp_path, monkeypatch, capsys):
    # A child that exits 0 but leaves no sentinel did NOT do its work in the Tree.
    tree_dir = tmp_path / "tree"
    _patch_identity(monkeypatch)
    _fake_create(monkeypatch, tree_dir)
    runner, _calls = _launcher(returncode=0, write_sentinel=False)

    rc = spawn_verb.run_subagent(
        repo="widget", epic="TRE03", ws=1, role="implementer", launcher=runner
    )

    assert rc == 1
    assert "left no sentinel" in capsys.readouterr().err


def test_spawn_subagent_help_documents_the_verb():
    from click.testing import CliRunner

    result = CliRunner().invoke(spawn_verb.spawn, ["subagent", "--help"])

    assert result.exit_code == 0
    for token in ("--repo", "--epic", "--ws", "--role", "--backend"):
        assert token in result.output
    assert "Tree" in result.output
