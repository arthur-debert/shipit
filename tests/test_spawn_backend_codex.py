"""Unit tests for the ``codex`` backend adapter and its registry entry (ADR-0020 §codex).

The per-backend WRITE launch contract probed by the WS00 spike — the exact ``codex exec
--dangerously-bypass-approvals-and-sandbox`` argv, the role-prepend conveyance (codex has
no ``--agent``), the ``OPENAI_API_KEY`` / ``CODEX_API_KEY`` scrub, and the ``None``
reviewer posture — asserted exhaustively at the seam inputs (ADR-0020 §De-risking (a):
``build_command`` + ``child_env`` are the cheap, high-value things to pin). No real codex
is spawned. Mirrors ``tests/test_spawn_backend_claude.py``.
"""

from __future__ import annotations

from shipit.spawn import backends
from shipit.spawn.backends import codex as codex_backend
from shipit.spawn.launch import launch, write_task

CODEX = codex_backend.CodexAdapter()


def test_build_command_is_the_adr_write_contract():
    cmd = CODEX.build_command("do the thing", "implementer")

    # The literal ADR-0020 §codex WRITE invocation, in order. The bypass flag is
    # load-bearing: the default workspace-write sandbox blocks .git writes + network,
    # so a Run that commits + opens a PR needs the unsandboxed posture.
    assert cmd[:5] == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
    ]
    assert cmd[5] == codex_backend.DEFAULT_MODEL
    # The prompt is the single trailing positional arg.
    assert cmd[6] == cmd[-1]
    assert len(cmd) == 7


def test_build_command_has_no_workspace_write_or_readonly_sandbox():
    # WRITE Run must NOT carry a constraining --sandbox: the bypass flag is the whole
    # point (probe: workspace-write cannot commit). The reviewer --sandbox read-only
    # path is WS04's, not built here.
    cmd = CODEX.build_command("t", "implementer")
    assert "--sandbox" not in cmd
    assert "read-only" not in cmd
    assert "workspace-write" not in cmd


def test_build_command_prepends_the_role_to_the_prompt():
    # codex has NO --agent flag (ADR-0020 §codex): the role is conveyed by prepending
    # it to the task prompt, the only native mechanism the spike validated.
    cmd = CODEX.build_command("implement issue #42", "implementer")
    prompt = cmd[-1]
    assert "implementer" in prompt
    assert prompt.endswith("implement issue #42")
    # The role rides as a flag value nowhere — it lives in the prompt text only.
    assert "--agent" not in cmd


def test_build_command_carries_the_role_verbatim():
    cmd = CODEX.build_command("t", "shepherd")
    assert "shepherd" in cmd[-1]


def test_build_command_ignores_tools_seam_parity():
    # codex has no tool allow-list; `tools` is accepted for seam parity but ignored
    # (no --tools flag in the argv whatever is passed).
    with_tools = CODEX.build_command("t", "reviewer", tools=("Read", "Grep"))
    without = CODEX.build_command("t", "reviewer")
    assert with_tools == without
    assert "--tools" not in with_tools


def test_reviewer_tools_is_none():
    # codex's read-only posture is a --sandbox flag, NOT an allow-list, so there is no
    # tuple to hand build_command — read-only rides the chmod'd Tree (ADR-0018) + WS04.
    assert CODEX.reviewer_tools is None


def test_child_env_scrubs_codex_auth_vars():
    parent = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "stale",
        "CODEX_API_KEY": "also-stale",
        "CODEX_HOME": "/home/a/.codex",
        "HOME": "/home/a",
    }

    env = CODEX.child_env(parent)

    # Both auth-shadowing vars gone so the ChatGPT OAuth login wins; CODEX_HOME (where
    # the OAuth tokens live) and everything else stays.
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert env == {"PATH": "/bin", "CODEX_HOME": "/home/a/.codex", "HOME": "/home/a"}


def test_child_env_without_keys_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = CODEX.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent  # a copy, never the caller's dict


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-os-environ")
    monkeypatch.setenv("CODEX_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = CODEX.child_env()

    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


def test_registry_resolves_codex():
    adapter = backends.resolve("codex")
    assert isinstance(adapter, codex_backend.CodexAdapter)
    assert adapter.name == "codex"


def test_codex_is_a_supported_backend():
    # Registering the adapter makes --backend codex selectable automatically (the
    # Choice derives from the registry — ADR-0020 §Decision 2).
    assert "codex" in backends.supported_backends()


def test_launch_roots_codex_in_the_tree_with_scrubbed_env():
    # End-to-end seam wiring (ADR-0020 invariants): the codex argv launches with cwd =
    # the Tree and the scrubbed env, via the injectable runner — no real codex spawned.
    captured: dict = {}

    def fake_runner(cmd, *, cwd, env):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        from shipit.spawn.launch import LaunchResult

        return LaunchResult(returncode=0, stdout="", stderr="")

    task = write_task("implementer", issue=42, branch="TRE05/WS02", base_branch="main")
    cmd = CODEX.build_command(task, "implementer")
    env = CODEX.child_env({"PATH": "/bin", "OPENAI_API_KEY": "stale"})

    result = launch(cmd, cwd="/trees/abc", env=env, runner=fake_runner)

    assert result.returncode == 0
    # cwd-rooting: the child runs in the Tree, never a `cd` (ADR-0020 §Decision 3).
    assert captured["cwd"] == "/trees/abc"
    # The auth scrub survives into the launched env.
    assert "OPENAI_API_KEY" not in captured["env"]
    # The bypass-posture write argv reached the runner intact.
    assert captured["cmd"][:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in captured["cmd"]
