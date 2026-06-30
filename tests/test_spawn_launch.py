"""Unit tests for the backend-agnostic launcher (:mod:`shipit.spawn.launch`).

The per-backend argv/auth-env/read-only-posture moved behind the ADR-0020 adapter
seam (covered in ``test_spawn_backend_claude.py``); what stays here is the *shared*
launch machinery: ``cwd`` = the Tree, ``stdin`` from ``/dev/null`` (the subprocess
seam), and the English PR-contract prompts. These tests pin each piece WITHOUT
spawning a real child — the prompts directly, and the subprocess seam by patching
``subprocess.run`` / injecting a fake runner.
"""

from __future__ import annotations

import subprocess

from shipit.spawn import launch


def test_reviewer_task_names_the_branch_and_posts_a_review():
    task = launch.reviewer_task("TRE03/WS03")
    assert "TRE03/WS03" in task
    assert "gh pr review" in task
    # The read-only posture is stated: no edits/build/push/merge.
    assert "READ-ONLY" in task


def test_reviewer_task_reads_the_diff_with_gh_pr_diff_not_a_hardcoded_base():
    # The diff instruction must use `gh pr diff` (the PR's actual base/head), NOT a baked
    # `git diff origin/main...HEAD` — an epic/umbrella PR has a non-main base, so a
    # hardcoded base would compute the wrong range.
    task = launch.reviewer_task("TRE03/WS03")
    assert "gh pr diff" in task
    assert "origin/main" not in task


def test_launch_routes_through_the_injected_runner():
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["env"] = env
        return launch.LaunchResult(returncode=0, stdout="{}", stderr="")

    result = launch.launch(
        ["claude", "-p", "t"],
        cwd="/trees/x",
        env={"PATH": "/bin"},
        runner=fake_runner,
    )

    assert result.returncode == 0
    assert seen["cmd"] == ["claude", "-p", "t"]
    assert seen["cwd"] == "/trees/x"  # rooting is the OS process cwd = the Tree
    assert seen["env"] == {"PATH": "/bin"}


def test_launch_stringifies_a_path_cwd():
    from pathlib import Path

    seen: dict = {}

    def fake_runner(cmd, *, cwd, env):
        seen["cwd"] = cwd
        return launch.LaunchResult(0, "", "")

    launch.launch([], cwd=Path("/trees/y"), env={}, runner=fake_runner)

    assert seen["cwd"] == "/trees/y"
    assert isinstance(seen["cwd"], str)


def test_subprocess_runner_redirects_stdin_from_devnull(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = launch._subprocess_runner(
        ["claude", "-p", "t"], cwd="/trees/x", env={"PATH": "/bin"}
    )

    assert result == launch.LaunchResult(returncode=0, stdout="out", stderr="err")
    kwargs = captured["kwargs"]
    # The TTY-less child must not block on stdin (ADR-0019 §1).
    assert kwargs["stdin"] is subprocess.DEVNULL
    # Rooted in the Tree, with the (already-scrubbed) env passed as-is.
    assert kwargs["cwd"] == "/trees/x"
    assert kwargs["env"] == {"PATH": "/bin"}
    # Captured text, and a nonzero child is a result not an exception.
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


def test_subprocess_runner_returns_nonzero_without_raising(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 7, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = launch._subprocess_runner(["claude"], cwd="/x", env={})

    assert result.returncode == 7
    assert result.stderr == "boom"


def test_pixi_wrap_routes_through_pixi_for_a_provisioned_tree(tmp_path):
    # A WRITE Tree is `pixi install`-provisioned → it carries `.pixi/envs/default`, so the
    # backend argv is re-expressed to run THROUGH the Tree's pixi env (ADR-0019 amendment):
    # `pixi run --manifest-path <tree>/pixi.toml -- <argv>`. The `--` separates pixi's args
    # from the child argv, and the manifest path is the Tree's own pixi.toml.
    (tmp_path / ".pixi" / "envs" / "default").mkdir(parents=True)
    argv = ["claude", "-p", "do the thing", "--agent", "implementer"]

    wrapped = launch.pixi_wrap(argv, tmp_path)

    assert wrapped == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "--",
        "claude",
        "-p",
        "do the thing",
        "--agent",
        "implementer",
    ]


def test_pixi_wrap_stays_bare_for_an_unprovisioned_tree(tmp_path):
    # A reviewer's READ-ONLY Tree (ADR-0018, clone+checkout, no provision) and a non-pixi
    # repo carry no `.pixi/envs/default`, so the argv is returned UNCHANGED — routing those
    # through `pixi run` would force a solve into a chmod'd tree or fail outright.
    argv = ["claude", "-p", "review", "--agent", "reviewer"]

    assert launch.pixi_wrap(argv, tmp_path) == argv


def test_pixi_wrap_accepts_a_str_tree_path(tmp_path):
    # The call site passes `tree.path` (a str); the gate probe must work on a str too.
    (tmp_path / ".pixi" / "envs" / "default").mkdir(parents=True)

    wrapped = launch.pixi_wrap(["claude"], str(tmp_path))

    assert wrapped[:4] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
    ]


def test_scrub_tree_env_drops_leaked_pixi_and_conda_activation_keeps_the_rest():
    # On top of the adapter's auth scrub, the launch path drops parent-project PIXI_*
    # pointers and Conda ACTIVATION vars (the #167 leak class) so the child re-resolves
    # from the Tree — but installation-level CONDA_* (CONDA_EXE / CONDA_PYTHON_EXE) is
    # KEPT, since scrubbing all CONDA_* could break `pixi run` in a Conda-managed shell.
    env = {
        "HOME": "/home/a",
        "PATH": "/bin",
        "PIXI_PROJECT_MANIFEST": "/parent/pixi.toml",
        "PIXI_PROJECT_NAME": "parent",
        "CONDA_PREFIX": "/parent/.pixi/envs/default",
        "CONDA_PREFIX_1": "/parent/.pixi/envs/stacked",
        "CONDA_DEFAULT_ENV": "default",
        "CONDA_SHLVL": "2",
        "CONDA_PROMPT_MODIFIER": "(default) ",
        "CONDA_EXE": "/opt/conda/bin/conda",
        "CONDA_PYTHON_EXE": "/opt/conda/bin/python",
    }

    scrubbed = launch.scrub_tree_env(env)

    assert scrubbed == {
        "HOME": "/home/a",
        "PATH": "/bin",
        "CONDA_EXE": "/opt/conda/bin/conda",
        "CONDA_PYTHON_EXE": "/opt/conda/bin/python",
    }


def test_scrub_tree_env_keeps_pixi_cache_vars():
    # The cache-location vars are user-level (not project-bound), so they are KEPT to
    # preserve cross-Tree package-cache sharing — the same carve-out provisioning uses
    # (reused via `is_leaked_env_var`, so the two paths cannot drift).
    env = {"PIXI_CACHE_DIR": "/cache/pixi", "RATTLER_CACHE_DIR": "/cache/rattler"}

    assert launch.scrub_tree_env(env) == env


def test_scrub_tree_env_returns_a_fresh_dict():
    env = {"PATH": "/bin"}
    assert launch.scrub_tree_env(env) is not env


def test_write_task_names_the_role_issue_and_branch():
    task = launch.write_task(
        "implementer", issue=156, branch="TRE03/WS02", base_branch="main"
    )
    # The Run learns its role, which issue to implement, and the exact branch its
    # draft PR must come from (the head shipit later resolves the PR↔Run link by).
    assert "implementer" in task
    assert "#156" in task
    assert "TRE03/WS02" in task
    assert "main" in task


def test_write_task_instructs_a_draft_pr_and_to_stop():
    # WS02 (acceptance #156): the Run reports back through a DRAFT PR and STOPS at
    # PR-open — never flips ready or merges. Both are load-bearing in the prompt.
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main"
    )
    assert "draft" in task.lower()
    assert "for #42" in task
    assert "stop" in task.lower()
