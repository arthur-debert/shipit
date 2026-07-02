"""Unit tests for the backend-agnostic launcher (:mod:`shipit.spawn.launch`).

The per-backend argv/auth-env/read-only-posture moved behind the ADR-0020 adapter
seam (covered in ``test_spawn_backend_claude.py``); what stays here is the *shared*
launch machinery: ``cwd`` = the Tree, the Exec-runner consumer view (PROC01-WS04:
the real runner is one Exec through :func:`shipit.execrun.run`, with the launch
contract's semantics pinned as explicit parameters), and the English PR-contract
prompts. These tests pin each piece WITHOUT spawning a real child — the prompts
directly, and the Exec seam by faking ``execrun.run`` / injecting a fake runner.
"""

from __future__ import annotations

import pytest

from shipit import execrun
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


def test_exec_runner_is_a_consumer_view_over_the_exec_runner(monkeypatch):
    # PROC01-WS04: the real launch seam is ONE Exec through execrun.run (ADR-0028),
    # with every launch-contract semantic pinned as an explicit parameter.
    captured: dict = {}

    def fake_exec_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="out", stderr="err", duration_ms=12
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    result = launch._exec_runner(
        ["claude", "-p", "t"], cwd="/trees/x", env={"PATH": "/bin"}
    )

    assert result == launch.LaunchResult(returncode=0, stdout="out", stderr="err")
    assert captured["argv"] == ["claude", "-p", "t"]
    kwargs = captured["kwargs"]
    # Rooted in the Tree, with the (already-scrubbed) env REPLACING the child's
    # environment — a scrubbed key must not creep back in via the runner's merge.
    assert kwargs["cwd"] == "/trees/x"
    assert kwargs["env"] == {"PATH": "/bin"}
    assert kwargs["replace_env"] is True
    # A nonzero child is a result, not an exception (ADR-0019 §6).
    assert kwargs["check"] is False
    # The EXPLICIT timeout override: no default (5m or otherwise) may kill a Run.
    assert "timeout" in kwargs
    assert kwargs["timeout"] is None
    assert launch.LAUNCH_TIMEOUT is None


def test_exec_runner_returns_nonzero_without_raising(monkeypatch):
    # check=False rides the Exec: a nonzero agent child comes back as a
    # LaunchResult the verb reports — never an ExecError (ADR-0019/0020).
    def fake_exec_run(argv, **kwargs):
        return execrun.ExecResult(
            argv=tuple(argv), rc=7, stdout="", stderr="boom", duration_ms=3
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    result = launch._exec_runner(["claude"], cwd="/x", env={})

    assert result.returncode == 7
    assert result.stderr == "boom"


def test_exec_runner_normalizes_a_missing_binary_into_execerror(tmp_path):
    # The transport itself failing (backend binary not on PATH) IS an ExecError —
    # the runner normalizes the raw FileNotFoundError (ADR-0028), and the spawn
    # verb maps it to a clean exit-1. Driven against the REAL runner: the exec
    # never happens, so this is fast and hermetic.
    with pytest.raises(execrun.ExecError) as excinfo:
        launch._exec_runner(
            ["definitely-not-a-real-backend-binary"],
            cwd=str(tmp_path),
            env={"PATH": str(tmp_path)},
        )

    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_exec_runner_emits_the_exec_record_with_duration(monkeypatch, caplog):
    # Acceptance (PROC01-WS04): a launch Exec appears in the structured record with
    # duration like any other Exec. Fake the subprocess layer INSIDE execrun so the
    # real runner produces its one record for the launch.
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(execrun.subprocess, "run", fake_run)

    with caplog.at_level("DEBUG", logger="shipit.exec"):
        launch._exec_runner(["claude", "-p", "t"], cwd="/trees/x", env={})

    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1  # exactly one record per Exec
    message = records[0].getMessage()
    assert "claude -p t" in message
    assert "cwd=/trees/x" in message
    assert "rc=0" in message
    assert "ms" in message  # the duration rides the record


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


def test_scrub_tree_env_drops_leaked_build_env_keeps_sccache_backend_vars():
    # agy ERROR: the launch env feeds a child that runs `cargo` via the Tree's own pixi
    # activation (pixi_wrap → `pixi run`). A leaked PARENT CARGO_TARGET_DIR / SCCACHE_BASEDIRS
    # would shadow the Tree's own `[activation.env]` value, so the child writes artifacts to
    # the PARENT Tree. Scrub the three per-Tree build vars; KEEP the sccache binary pointer
    # and cache credential (not per-Tree; the child needs them to reach the shared cache).
    env = {
        "PATH": "/bin",
        "CARGO_TARGET_DIR": "/parent/tree/target",
        "SCCACHE_BASEDIRS": "/parent/tree",
        "CARGO_INCREMENTAL": "0",
        "RUSTC_WRAPPER": "/usr/bin/sccache",
        "SCCACHE_GCS_KEY": "creds",
    }

    scrubbed = launch.scrub_tree_env(env)

    assert scrubbed == {
        "PATH": "/bin",
        "RUSTC_WRAPPER": "/usr/bin/sccache",
        "SCCACHE_GCS_KEY": "creds",
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
