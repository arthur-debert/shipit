"""Unit tests for the ``codex`` backend adapter and its registry entry (ADR-0020 §codex).

The per-backend WRITE launch contract probed by the WS00 spike — the exact ``codex exec
--dangerously-bypass-approvals-and-sandbox`` argv, the role-prepend conveyance (codex has
no ``--agent``), the ``OPENAI_API_KEY`` / ``CODEX_API_KEY`` API-billing scrub with the
``CODEX_ACCESS_TOKEN`` automation passthrough (CDX01-WS03), and the reviewer
posture — asserted exhaustively at the seam inputs (ADR-0020 §De-risking (a):
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


def test_build_command_has_no_sandbox_for_a_write_run():
    # WRITE Run must NOT carry a constraining --sandbox: the bypass flag is the whole
    # point (probe: workspace-write cannot commit). The reviewer sandbox posture is a
    # separate branch (read_only=True), never folded into the write argv.
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


def test_build_command_has_no_tools_flag_either_posture():
    # codex has no tool allow-list (the read-only signal is the read_only flag, not a
    # tool tuple): --tools never appears, write OR reviewer.
    assert "--tools" not in CODEX.build_command("t", "reviewer", read_only=True)
    assert "--tools" not in CODEX.build_command("t", "implementer")


def test_reviewer_build_command_is_network_capable_non_bypass_sandbox():
    # WS04a probe: a reviewer self-posts via `gh pr review` (needs the network), and
    # codex --sandbox read-only BLOCKS the network — so the reviewer posture is the
    # least-privilege sandbox that still grants network: workspace-write + the
    # network_access override, NOT the write Run's bypass flag. The chmod'd Tree
    # (ADR-0018) is the load-bearing FS read-only guard.
    cmd = CODEX.build_command("review it", "reviewer", read_only=True)
    assert cmd[:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert "--ephemeral" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("-c") + 1] == codex_backend.NETWORK_ACCESS_OVERRIDE
    # Crucially the reviewer does NOT carry the write/bypass posture.
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "read-only" not in cmd  # the ADR's first guess — falsified by the probe
    # The model + prompt still trail in order; prompt is the single positional.
    assert cmd[cmd.index("--model") + 1] == codex_backend.DEFAULT_MODEL
    assert cmd[-1] == f"{codex_backend._role_preamble('reviewer')}\n\nreview it"


def test_write_and_reviewer_argv_differ_in_posture():
    # The two postures are distinct argv — the reviewer must never be the write argv
    # (the whole point of read_only): write carries the bypass flag, reviewer the
    # network-capable sandbox.
    write = CODEX.build_command("t", "implementer")
    reviewer = CODEX.build_command("t", "reviewer", read_only=True)
    assert write != reviewer
    assert "--dangerously-bypass-approvals-and-sandbox" in write
    assert "--dangerously-bypass-approvals-and-sandbox" not in reviewer
    assert "--sandbox" in reviewer
    assert "--sandbox" not in write


def test_default_model_is_resolved_and_write_path_is_unchanged():
    # The registry instance uses DEFAULT_MODEL (a verbatim id), so resolve_model leaves
    # it unchanged — the write argv is byte-for-byte what it was before the model param.
    assert CODEX.model == codex_backend.DEFAULT_MODEL
    assert codex_backend.resolve_model("pro") == "gpt-5.5"
    assert (
        codex_backend.resolve_model("gpt-5.5") == "gpt-5.5"
    )  # verbatim passes through


def test_constructed_with_a_legacy_alias_resolves_the_model():
    # The funnel constructs an instance with its per-reviewer `model` (a legacy alias);
    # the adapter resolves it to the Codex model id and threads it into build_command.
    adapter = codex_backend.CodexAdapter(model="pro")
    cmd = adapter.build_command("t", "reviewer", read_only=True)
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"


def test_reviewer_output_schema_adds_the_native_schema_flag():
    # TRE05-WS04b: a capture reviewer passes the schema temp-file path; codex enforces
    # the JSON shape natively via --output-schema (the robustness win ADR-0020 keeps).
    cmd = CODEX.build_command(
        "review it", "reviewer", read_only=True, output_schema_path="/tmp/schema.json"
    )
    assert cmd[cmd.index("--output-schema") + 1] == "/tmp/schema.json"
    # It rides the reviewer posture, before the model + prompt.
    assert cmd.index("--output-schema") < cmd.index("--model")
    # The self-posting spawn-surface reviewer (no schema) omits it entirely.
    assert "--output-schema" not in CODEX.build_command("t", "reviewer", read_only=True)


def test_write_run_never_carries_output_schema_even_if_passed():
    # A write Run emits no captured JSON, so --output-schema is never added to it — the
    # flag is gated on read_only as well as a provided path.
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

    # Both auth-shadowing vars gone so the ChatGPT OAuth login wins; CODEX_HOME (where
    # the OAuth tokens live) and everything else stays.
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert env == {"PATH": "/bin", "CODEX_HOME": "/home/a/.codex", "HOME": "/home/a"}


def test_auth_scrub_list_is_exactly_the_api_billing_keys():
    # CDX01-WS03: the scrub is scoped to the two API-billing keys — CODEX_API_KEY (the
    # documented opt-in for API-billed `codex exec`) and OPENAI_API_KEY (the shared
    # OpenAI-SDK var other tools export) — so a stale key can never silently flip a Run
    # off the ChatGPT subscription. Pinned exactly: adding CODEX_ACCESS_TOKEN here
    # would break headless access-token automation (see the passthrough tests below).
    assert codex_backend.AUTH_ENV_VARS == ("OPENAI_API_KEY", "CODEX_API_KEY")
    assert codex_backend.ACCESS_TOKEN_VAR not in codex_backend.AUTH_ENV_VARS


def test_child_env_passes_the_access_token_through():
    # CDX01-WS03 probe (codex 0.139): CODEX_ACCESS_TOKEN is the subscription-token
    # trusted-automation conduit codex consumes natively from the env (it takes
    # precedence over the stored $CODEX_HOME login; a bogus one fails LOUD with
    # "invalid agent identity JWT format"). It must survive child_env — scrubbing it
    # would strand headless automation that has no persisted login.
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
    # cwd-rooting: the child runs in the Tree, never a `cd` (ADR-0020 §Decision 3).
    assert captured["cwd"] == "/trees/abc"
    # The auth scrub survives into the launched env.
    assert "OPENAI_API_KEY" not in captured["env"]
    # The bypass-posture write argv reached the runner intact.
    assert captured["cmd"][:2] == ["codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" in captured["cmd"]


def test_reasoning_level_rides_the_model_reasoning_effort_override():
    # RVW03-WS04 (#685): a pinned ReasoningLevel reaches REAL argv via codex's
    # `-c model_reasoning_effort=<level>` config override (probed on 0.139.0:
    # the run header echoes `reasoning effort: <level>`), and the adapter
    # reports the applied level for the record stamp.
    from shipit.spawn.backends import codex as codex_backend

    adapter = codex_backend.CodexAdapter(reasoning="low")
    cmd = adapter.build_command("task", "reviewer", read_only=True)
    assert "model_reasoning_effort=low" in cmd
    assert cmd[cmd.index("model_reasoning_effort=low") - 1] == "-c"
    assert adapter.reasoning == "low"
    # The override precedes the prompt (the last positional arg), like every flag.
    assert cmd[-1].endswith("task")


def test_no_reasoning_level_means_no_effort_override():
    from shipit.spawn.backends import codex as codex_backend

    adapter = codex_backend.CodexAdapter()
    cmd = adapter.build_command("task", "implementer")
    assert not any("model_reasoning_effort" in arg for arg in cmd)
    assert adapter.reasoning is None
