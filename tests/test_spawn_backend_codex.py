from __future__ import annotations

from shipit.spawn import backends
from shipit.spawn.backends import codex as codex_backend
from shipit.spawn.launch import launch, write_task

CODEX = codex_backend.CodexAdapter()


def test_build_command_is_the_adr_write_contract():
    cmd = CODEX.build_command("do the thing", "implementer")

    assert cmd[:4] == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert cmd[cmd.index("--model") + 1] == codex_backend.DEFAULT_MODEL
    assert cmd[cmd.index("-c") + 1].startswith(
        f"{codex_backend.DEVELOPER_INSTRUCTIONS_KEY}="
    )
    assert cmd[-1] == "do the thing"


def test_build_command_has_no_sandbox_for_a_write_run():
    cmd = CODEX.build_command("t", "implementer")
    assert "--sandbox" not in cmd
    assert "read-only" not in cmd
    assert "workspace-write" not in cmd


def test_build_command_carries_generated_role_in_developer_instructions():
    cmd = CODEX.build_command("implement issue #42", "implementer")
    instructions = next(
        arg
        for arg in cmd
        if arg.startswith(f"{codex_backend.DEVELOPER_INSTRUCTIONS_KEY}=")
    )
    assert "## Your role" in instructions
    assert "implementer" in instructions
    assert cmd[-1] == "implement issue #42"
    assert "--agent" not in cmd


def test_build_command_carries_the_role_verbatim():
    cmd = CODEX.build_command("t", "shepherd")
    instructions = next(
        arg
        for arg in cmd
        if arg.startswith(f"{codex_backend.DEVELOPER_INSTRUCTIONS_KEY}=")
    )
    assert "shepherd" in instructions


def test_build_command_has_no_tools_flag_either_posture():
    assert "--tools" not in CODEX.build_command("t", "reviewer", read_only=True)
    assert "--tools" not in CODEX.build_command("t", "implementer")


def test_reviewer_build_command_is_network_capable_non_bypass_sandbox():
    cmd = CODEX.build_command("review it", "reviewer", read_only=True)
    assert cmd[:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert "--ephemeral" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("-c") + 1] == codex_backend.NETWORK_ACCESS_OVERRIDE
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "read-only" not in cmd
    assert cmd[cmd.index("--model") + 1] == codex_backend.DEFAULT_MODEL
    assert cmd[-1] == "review it"


def test_write_and_reviewer_argv_differ_in_posture():
    write = CODEX.build_command("t", "implementer")
    reviewer = CODEX.build_command("t", "reviewer", read_only=True)
    assert write != reviewer
    assert "--dangerously-bypass-approvals-and-sandbox" in write
    assert "--dangerously-bypass-approvals-and-sandbox" not in reviewer
    assert "--sandbox" in reviewer
    assert "--sandbox" not in write


def test_default_model_is_resolved_and_write_path_is_unchanged():
    assert CODEX.model == codex_backend.DEFAULT_MODEL
    assert codex_backend.resolve_model("pro") == "gpt-5.5"
    assert codex_backend.resolve_model("gpt-5.5") == "gpt-5.5"


def test_constructed_with_a_legacy_alias_resolves_the_model():
    adapter = codex_backend.CodexAdapter(model="pro")
    cmd = adapter.build_command("t", "reviewer", read_only=True)
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"


def test_reviewer_output_schema_adds_the_native_schema_flag():
    cmd = CODEX.build_command(
        "review it", "reviewer", read_only=True, output_schema_path="/tmp/schema.json"
    )
    assert cmd[cmd.index("--output-schema") + 1] == "/tmp/schema.json"
    assert cmd.index("--output-schema") < cmd.index("--model")
    assert "--output-schema" not in CODEX.build_command("t", "reviewer", read_only=True)


def test_write_run_never_carries_output_schema_even_if_passed():
    cmd = CODEX.build_command(
        "t", "implementer", read_only=False, output_schema_path="/tmp/schema.json"
    )
    assert "--output-schema" not in cmd


def test_child_env_scrubs_codex_auth_vars():
    parent = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "stale",
        "CODEX_API_KEY": "also-stale",
        "CODEX_HOME": "/home/a/.codex",
        "HOME": "/home/a",
    }

    env = CODEX.child_env(parent)

    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert env == {"PATH": "/bin", "CODEX_HOME": "/home/a/.codex", "HOME": "/home/a"}


def test_auth_scrub_list_is_exactly_the_api_billing_keys():
    assert codex_backend.AUTH_ENV_VARS == ("OPENAI_API_KEY", "CODEX_API_KEY")
    assert codex_backend.ACCESS_TOKEN_VAR not in codex_backend.AUTH_ENV_VARS


def test_child_env_passes_the_access_token_through():
    parent = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "stale",
        "CODEX_API_KEY": "also-stale",
        codex_backend.ACCESS_TOKEN_VAR: "subscription-jwt",
    }

    env = CODEX.child_env(parent)

    assert env == {"PATH": "/bin", codex_backend.ACCESS_TOKEN_VAR: "subscription-jwt"}


def test_child_env_from_os_environ_keeps_the_access_token(monkeypatch):
    monkeypatch.setenv(codex_backend.ACCESS_TOKEN_VAR, "subscription-jwt")
    monkeypatch.setenv("CODEX_API_KEY", "stale")

    env = CODEX.child_env()

    assert env.get(codex_backend.ACCESS_TOKEN_VAR) == "subscription-jwt"
    assert "CODEX_API_KEY" not in env


def test_child_env_without_keys_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = CODEX.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent


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
    assert "codex" in backends.supported_backends()


def test_launch_roots_codex_in_the_tree_with_scrubbed_env():
    captured: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        from shipit.spawn.launch import LaunchResult

        return LaunchResult(returncode=0, stdout="", stderr="")

    task = write_task(
        "implementer", issue=42, branch="TRE05/WS02", base_branch="main", closes=False
    )
    cmd = CODEX.build_command(task, "implementer")
    env = CODEX.child_env({"PATH": "/bin", "OPENAI_API_KEY": "stale"})

    result = launch(cmd, cwd="/trees/abc", env=env, runner=fake_runner)

    assert result.returncode == 0
    assert captured["cwd"] == "/trees/abc"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["cmd"][:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in captured["cmd"]


def test_reasoning_level_rides_the_model_reasoning_effort_override():
    from shipit.spawn.backends import codex as codex_backend

    adapter = codex_backend.CodexAdapter(reasoning="low")
    cmd = adapter.build_command("task", "reviewer", read_only=True)
    assert "model_reasoning_effort=low" in cmd
    assert cmd[cmd.index("model_reasoning_effort=low") - 1] == "-c"
    assert adapter.reasoning == "low"
    assert cmd[-1].endswith("task")


def test_no_reasoning_level_means_no_effort_override():
    from shipit.spawn.backends import codex as codex_backend

    adapter = codex_backend.CodexAdapter()
    cmd = adapter.build_command("task", "implementer")
    assert not any("model_reasoning_effort" in arg for arg in cmd)
    assert adapter.reasoning is None
