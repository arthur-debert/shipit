"""Unit tests for the headless-``claude`` launcher (:mod:`shipit.spawn.launch`).

The launch contract is ADR-0019: the exact argv, ``cwd`` = the Tree, ``stdin`` from
``/dev/null``, and ``ANTHROPIC_API_KEY`` scrubbed from the child env. These tests
pin each piece WITHOUT spawning a real ``claude`` — the pure builders directly, and
the subprocess seam by patching ``subprocess.run`` / injecting a fake runner.
"""

from __future__ import annotations

import subprocess

from shipit.spawn import launch


def test_build_command_is_the_adr_contract():
    cmd = launch.build_command("do the thing", "implementer")

    # The literal ADR-0019 §1 invocation, in order.
    assert cmd == [
        "claude",
        "-p",
        "do the thing",
        "--agent",
        "implementer",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
    ]


def test_build_command_carries_the_role_verbatim():
    # --agent <role> is load-bearing (ADR-0019 §2): it conveys the role to the
    # harness so the guard allows the Run's own edits. The role rides through as-is.
    cmd = launch.build_command("t", "shepherd")
    assert cmd[cmd.index("--agent") + 1] == "shepherd"


def test_child_env_scrubs_anthropic_api_key():
    parent = {"PATH": "/bin", "ANTHROPIC_API_KEY": "stale-key", "HOME": "/home/a"}

    env = launch.child_env(parent)

    # The hard contract requirement (ADR-0019 §3): the key is gone, the rest stays.
    assert "ANTHROPIC_API_KEY" not in env
    assert env == {"PATH": "/bin", "HOME": "/home/a"}


def test_child_env_without_key_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = launch.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent  # a copy, never the caller's dict


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = launch.child_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


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


def test_skeleton_task_names_the_sentinel_and_the_role():
    task = launch.skeleton_task("implementer")
    assert launch.SENTINEL_NAME in task
    assert "implementer" in task


def test_sentinel_path_is_under_the_tree(tmp_path):
    path = launch.sentinel_path(tmp_path)
    assert path == tmp_path / launch.SENTINEL_NAME


def test_sentinel_present_reflects_the_file(tmp_path):
    assert launch.sentinel_present(tmp_path) is False
    (tmp_path / launch.SENTINEL_NAME).write_text(launch.SENTINEL_BODY)
    assert launch.sentinel_present(tmp_path) is True


def test_sentinel_present_rejects_wrong_or_empty_content(tmp_path):
    # Existence is not enough (acceptance #155): a child that writes empty, truncated,
    # or wrong contents did not do the work the skeleton task specified, so it must
    # NOT be reported as a present sentinel.
    sentinel = tmp_path / launch.SENTINEL_NAME
    for bad in ("", "spawned by shipit", "garbage\n", launch.SENTINEL_BODY + "extra\n"):
        sentinel.write_text(bad)
        assert launch.sentinel_present(tmp_path) is False


def test_sentinel_present_false_when_path_is_a_directory(tmp_path):
    # A directory at the sentinel path is unreadable as text — treated as absent,
    # never an escaping OSError.
    (tmp_path / launch.SENTINEL_NAME).mkdir()
    assert launch.sentinel_present(tmp_path) is False
