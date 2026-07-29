from __future__ import annotations

from shipit.spawn import backends
from shipit.spawn.backends import claude as claude_backend

CLAUDE = claude_backend.ClaudeAdapter()


def test_build_command_is_the_adr_contract():
    cmd = CLAUDE.build_command("do the thing", "implementer")

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
    cmd = CLAUDE.build_command("t", "shepherd")
    assert cmd[cmd.index("--agent") + 1] == "shepherd"


def test_build_command_omits_tools_for_a_write_run():
    cmd = CLAUDE.build_command("t", "implementer")
    assert "--tools" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"


def test_build_command_adds_readonly_tools_for_a_reviewer():
    cmd = CLAUDE.build_command("t", "reviewer", read_only=True)
    allowlist = cmd[cmd.index("--tools") + 1]
    assert allowlist == "Read,Grep,Glob,Bash"
    assert "Write" not in allowlist and "Edit" not in allowlist
    assert cmd.index("--tools") < cmd.index("--output-format")


def test_reviewer_tools_constant_is_the_readonly_posture():
    assert claude_backend.REVIEWER_TOOLS == ("Read", "Grep", "Glob", "Bash")
    assert "Write" not in claude_backend.REVIEWER_TOOLS
    assert "Edit" not in claude_backend.REVIEWER_TOOLS


def test_child_env_scrubs_anthropic_api_key():
    parent = {"PATH": "/bin", "ANTHROPIC_API_KEY": "stale-key", "HOME": "/home/a"}

    env = CLAUDE.child_env(parent)

    assert "ANTHROPIC_API_KEY" not in env
    assert env == {"PATH": "/bin", "HOME": "/home/a"}


def test_child_env_without_key_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = CLAUDE.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = CLAUDE.child_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


def test_registry_includes_claude_codex_and_antigravity():
    assert backends.supported_backends() == ("claude", "codex", "antigravity")


def test_resolve_returns_the_claude_adapter():
    adapter = backends.resolve("claude")
    assert isinstance(adapter, claude_backend.ClaudeAdapter)
    assert adapter.name == "claude"


def test_resolve_unknown_backend_raises():
    import pytest

    with pytest.raises(KeyError):
        backends.resolve("nonexistent")


def test_output_schema_path_is_accepted_and_ignored():
    cmd = CLAUDE.build_command(
        "t", "reviewer", read_only=True, output_schema_path="/tmp/s.json"
    )
    assert "--output-schema" not in cmd
    assert "/tmp/s.json" not in cmd


def test_build_command_pins_a_model_when_the_instance_carries_one():
    cmd = claude_backend.ClaudeAdapter(model="opus-x").build_command("task", "reviewer")
    assert cmd[cmd.index("--model") + 1] == "opus-x"
    assert "--model" not in claude_backend.ClaudeAdapter().build_command(
        "task", "reviewer"
    )


def test_reasoning_level_rides_the_native_effort_flag():
    adapter = claude_backend.ClaudeAdapter(reasoning="high")
    cmd = adapter.build_command("task", "reviewer", read_only=True)
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert adapter.reasoning == "high"


def test_no_reasoning_level_means_no_effort_flag():
    cmd = CLAUDE.build_command("task", "implementer")
    assert "--effort" not in cmd
    assert CLAUDE.reasoning is None
