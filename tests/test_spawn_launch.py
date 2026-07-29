from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from shipit import execrun, workenv
from shipit.identity import repo_from_slug
from shipit.spawn import launch


def test_shepherd_task_shell_quotes_the_untrusted_head_ref():
    task = launch.shepherd_task(
        pr_number=321,
        branch="topic;echo-pwned",
        base_branch="main",
    )

    assert "git push origin 'HEAD:refs/heads/topic;echo-pwned'" in task


def test_write_task_forbids_ending_the_turn_with_background_work_in_flight():
    task = launch.write_task(
        "implementer",
        issue=663,
        branch="issues/663/work",
        base_branch="main",
        closes=True,
    )
    assert "headless" in task.lower()
    assert "ending your turn exits" in task.lower()
    assert "background" in task.lower()
    assert "killed" in task.lower()
    assert "foreground" in task.lower()


def test_write_task_background_rule_is_shape_independent():
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main", closes=False
    )
    assert "ending your turn exits" in task.lower()
    assert "foreground" in task.lower()


def test_launch_routes_through_the_injected_runner():
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["env"] = env
        seen["timeout"] = timeout
        return launch.LaunchResult(returncode=0, stdout="{}", stderr="")

    result = launch.launch(
        ["claude", "-p", "t"],
        cwd="/trees/x",
        env={"PATH": "/bin"},
        runner=fake_runner,
    )

    assert result.returncode == 0
    assert seen["cmd"] == ["claude", "-p", "t"]
    assert seen["cwd"] == "/trees/x"
    assert seen["env"] == {"PATH": "/bin"}
    assert seen["timeout"] is launch.LAUNCH_TIMEOUT


def test_launch_threads_an_explicit_timeout_to_the_runner():
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["timeout"] = timeout
        return launch.LaunchResult(0, "{}", "")

    launch.launch(["codex"], cwd="/trees/x", env={}, timeout=600.0, runner=fake_runner)

    assert seen["timeout"] == 600.0


def test_launch_stringifies_a_path_cwd():
    from pathlib import Path

    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["cwd"] = cwd
        return launch.LaunchResult(0, "", "")

    launch.launch([], cwd=Path("/trees/y"), env={}, runner=fake_runner)

    assert seen["cwd"] == "/trees/y"
    assert isinstance(seen["cwd"], str)


def test_exec_runner_is_a_consumer_view_over_the_exec_runner(monkeypatch):
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
    assert kwargs["cwd"] == "/trees/x"
    assert kwargs["env"] == {"PATH": "/bin"}
    assert kwargs["replace_env"] is True
    assert kwargs["check"] is False
    assert "timeout" in kwargs
    assert kwargs["timeout"] is None
    assert launch.LAUNCH_TIMEOUT is None


def test_exec_runner_passes_an_explicit_deadline_to_the_exec_runner(monkeypatch):
    captured: dict = {}

    def fake_exec_run(argv, **kwargs):
        captured["kwargs"] = kwargs
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="out", stderr="", duration_ms=1
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    launch._exec_runner(["codex"], cwd="/x", env={}, timeout=600.0)

    assert captured["kwargs"]["timeout"] == 600.0


def test_exec_runner_returns_nonzero_without_raising(monkeypatch):
    def fake_exec_run(argv, **kwargs):
        return execrun.ExecResult(
            argv=tuple(argv), rc=7, stdout="", stderr="boom", duration_ms=3
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    result = launch._exec_runner(["claude"], cwd="/x", env={})

    assert result.returncode == 7
    assert result.stderr == "boom"


def test_exec_runner_normalizes_a_missing_binary_into_execerror(tmp_path):
    with pytest.raises(execrun.ExecError) as excinfo:
        launch._exec_runner(
            ["definitely-not-a-real-backend-binary"],
            cwd=str(tmp_path),
            env={"PATH": str(tmp_path)},
        )

    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_exec_runner_emits_the_exec_record_with_duration(monkeypatch, caplog):
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(execrun.subprocess, "run", fake_run)

    with caplog.at_level("DEBUG", logger="shipit.exec"):
        launch._exec_runner(["claude", "-p", "t"], cwd="/trees/x", env={})

    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "claude -p '<redacted: prompt sha256=" in message
    assert " -p t" not in message
    assert "cwd=/trees/x" in message
    assert "rc=0" in message
    assert "ms" in message


def _write_env(*, pixi_provisioned: bool):
    return workenv.resolve_write_run_env(
        repo=repo_from_slug("acme/widget"),
        tree_path="/nonexistent/trees/acme/widget/E/WS01-abc123",
        branch="E/WS01",
        base="origin/E/umbrella",
        pixi_provisioned=pixi_provisioned,
    )


def test_route_argv_carries_out_the_pixi_run_decision():
    argv = ["claude", "-p", "do the thing", "--agent", "implementer"]

    routed = launch.route_argv(argv, _write_env(pixi_provisioned=True))

    assert routed == [
        "pixi",
        "run",
        "--manifest-path",
        "/nonexistent/trees/acme/widget/E/WS01-abc123/pixi.toml",
        "--",
        *argv,
    ]


def test_route_argv_leaves_an_ambient_routed_work_env_bare():
    argv = ["claude", "-p", "do the thing"]

    assert launch.route_argv(argv, _write_env(pixi_provisioned=False)) == argv


def test_route_argv_refuses_an_activation_snapshot_context():
    env = replace(
        _write_env(pixi_provisioned=False),
        routing=workenv.ExecutionRouting.ACTIVATION_SNAPSHOT,
    )

    with pytest.raises(ValueError, match="activation-snapshot"):
        launch.route_argv(["claude"], env)


def test_route_argv_records_its_routing_decision_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        launch.route_argv(["claude"], _write_env(pixi_provisioned=True))
        launch.route_argv(["claude"], _write_env(pixi_provisioned=False))

    decisions = [r.pixi_wrapped for r in caplog.records if hasattr(r, "pixi_wrapped")]
    assert decisions == [True, False]


def test_scrub_tree_env_drops_leaked_pixi_and_conda_activation_keeps_the_rest():
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
    env = {"PIXI_CACHE_DIR": "/cache/pixi", "RATTLER_CACHE_DIR": "/cache/rattler"}

    assert launch.scrub_tree_env(env) == env


def test_scrub_tree_env_returns_a_fresh_dict():
    env = {"PATH": "/bin"}
    assert launch.scrub_tree_env(env) is not env


def test_launch_scrubs_the_env_it_hands_the_runner():
    # #1153: the scrub is applied BY the launch seam, not by each caller. Every
    # child launched here is rooted at a Tree `cwd`, so a caller that forgot the
    # scrub handed its child an env naming the PARENT's project — the exact
    # cross-Tree leak rooting exists to prevent. Making it structural means the
    # next call site cannot reintroduce it.
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["env"] = env
        return launch.LaunchResult(0, "", "")

    launch.launch(
        ["claude"],
        cwd="/trees/child",
        env={
            "PATH": "/bin",
            "PIXI_PROJECT_MANIFEST": "/trees/parent/pixi.toml",
            "CONDA_PREFIX": "/trees/parent/.pixi/envs/default",
        },
        runner=fake_runner,
    )

    assert seen["env"] == {"PATH": "/bin"}


# --- a failed child's reason (#1153) -----------------------------------------


def _failed(stdout: str = "", stderr: str = "", rc: int = 1) -> launch.LaunchResult:
    return launch.LaunchResult(returncode=rc, stdout=stdout, stderr=stderr)


def _detail(result: launch.LaunchResult) -> str:
    return launch.child_failure_detail(
        result, backend="claude", tree_path="/trees/child", duration_ms=388_000
    )


def test_child_failure_detail_reports_stdout_before_stderr():
    # stdout leads because a headless `claude -p` writes its errors there; reading
    # stderr alone (the pre-#1153 behaviour) is blind to the common failure.
    detail = _detail(_failed(stdout="out-text", stderr="err-text"))

    assert detail.index("out-text") < detail.index("err-text")
    assert "--- child stdout (tail) ---" in detail
    assert "--- child stderr (tail) ---" in detail


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "only-stderr", "only-stderr"),
        ("only-stdout", "", "only-stdout"),
    ],
)
def test_child_failure_detail_surfaces_whichever_stream_spoke(stdout, stderr, expected):
    detail = _detail(_failed(stdout=stdout, stderr=stderr))

    assert expected in detail
    # The silent stream contributes NO empty labelled section.
    assert detail.count("(tail)") == 1


def test_child_failure_detail_always_names_the_exit_tree_and_duration():
    detail = _detail(_failed(stdout="x", rc=137))

    assert "claude child exited 137" in detail
    assert "/trees/child" in detail
    assert "388000ms" in detail


def test_child_failure_detail_calls_out_a_wholly_silent_child():
    # Both streams empty is the DOC01 shape: no text to quote, so the refusal must
    # say the child produced no account rather than emit a bare exit code.
    detail = _detail(_failed(stdout="  ", stderr="\n\t"))

    assert "wrote NOTHING to either stdout or stderr" in detail
    assert "(tail)" not in detail
    assert "/trees/child" in detail
    assert "388000ms" in detail


def test_stream_tail_keeps_a_short_stream_whole_and_stripped():
    assert launch.stream_tail("\n  hello  \n") == "hello"


def test_stream_tail_bounds_a_runaway_stream_to_its_end():
    # The TAIL is what matters — a crashing child's reason is its last output, and
    # an unbounded stream would bury the headline it is attached to.
    tail = launch.stream_tail("A" * 500 + "THE-REASON", limit=10)

    assert tail.endswith("THE-REASON")
    assert "A" * 20 not in tail
    assert "500 earlier chars elided" in tail


def test_stream_tail_default_limit_is_the_declared_cap():
    assert len(launch.stream_tail("z" * 10_000)) > launch.STREAM_TAIL_CHARS
    assert launch.stream_tail("z" * 10_000).count("z") == launch.STREAM_TAIL_CHARS


def test_write_task_names_the_role_issue_and_branch():
    task = launch.write_task(
        "implementer", issue=156, branch="TRE03/WS02", base_branch="main", closes=False
    )
    assert "implementer" in task
    assert "#156" in task
    assert "TRE03/WS02" in task
    assert "main" in task


def test_write_task_instructs_a_draft_pr_and_to_stop():
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main", closes=False
    )
    assert "draft" in task.lower()
    assert "for #42" in task
    assert "stop" in task.lower()
    assert "shipit pr next" in task
    assert "request reviews" not in task.lower()
    assert "address review rounds" in task


def test_write_task_links_closes_for_the_standalone_issue_shape():
    task = launch.write_task(
        "implementer",
        issue=649,
        branch="issues/649/work",
        base_branch="main",
        closes=True,
    )
    assert "closes #649" in task
    assert "for #649" not in task


def test_write_task_links_for_on_the_epic_work_stream_shape():
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main", closes=False
    )
    assert "for #42" in task
    assert "closes #42" not in task


def test_write_task_carries_the_bank_state_protocol():
    task = launch.write_task(
        "implementer",
        issue=587,
        branch="issues/587/work",
        base_branch="main",
        closes=True,
    )
    assert "`WIP:`" in task
    assert "bank your state" in task.lower()
    assert "push the branch" in task
    assert "'issues/587/work'" in task
    assert "git push -u origin issues/587/work" in task
