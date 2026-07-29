from __future__ import annotations

import pytest

from shipit.spawn import backends
from shipit.spawn.backends import antigravity as agy_backend

AGY = agy_backend.AntigravityAdapter()
TREE = "/trees/widget/TRE05-WS03"


def test_build_command_is_the_adr_write_contract():
    cmd = AGY.build_command("do the thing", "implementer", cwd=TREE)

    assert cmd == [
        "agy",
        "--new-project",
        "--add-dir",
        TREE,
        "--model=Gemini 3.1 Pro (High)",
        "--print-timeout=600s",
        "--dangerously-skip-permissions",
        "--print",
        "You are acting as the 'implementer' role for this Run.\n\ndo the thing",
    ]


def test_build_command_roots_in_the_tree_via_add_dir():
    cmd = AGY.build_command("t", "implementer", cwd=TREE)
    add_dir = cmd.index("--add-dir")
    assert cmd[add_dir + 1] == TREE
    assert cmd[add_dir - 1] == "--new-project"


def test_build_command_requires_cwd_for_the_tree_root():
    with pytest.raises(ValueError, match="requires cwd"):
        AGY.build_command("t", "implementer")


def test_build_command_carries_bypass_permissions_for_a_write_run():
    cmd = AGY.build_command("t", "implementer", cwd=TREE)
    assert "--dangerously-skip-permissions" in cmd


def test_reviewer_build_command_uses_prompt_prepend():
    cmd = AGY.build_command("review it", "reviewer", cwd=TREE, read_only=True)
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd == [
        "agy",
        "--new-project",
        "--add-dir",
        TREE,
        "--model=Gemini 3.1 Pro (High)",
        "--print-timeout=600s",
        "--print",
        "You are acting as the 'reviewer' role for this Run.\n\nreview it",
    ]


def test_reviewer_build_command_still_requires_cwd():
    with pytest.raises(ValueError, match="requires cwd"):
        AGY.build_command("t", "reviewer", read_only=True)


def test_write_and_reviewer_argv_differ_by_posture():
    write = AGY.build_command("t", "reviewer", cwd=TREE)
    reviewer = AGY.build_command("t", "reviewer", cwd=TREE, read_only=True)
    assert write != reviewer
    assert "--dangerously-skip-permissions" in write
    assert "--agent" not in write
    assert write[-1] == "You are acting as the 'reviewer' role for this Run.\n\nt"
    assert reviewer[-1] == "You are acting as the 'reviewer' role for this Run.\n\nt"


def test_read_only_non_reviewer_role_keeps_prompt_prepend():
    cmd = AGY.build_command("look around", "explorer", cwd=TREE, read_only=True)
    assert "--agent" not in cmd
    assert (
        cmd[-1] == "You are acting as the 'explorer' role for this Run.\n\nlook around"
    )


def test_build_command_prepends_the_role_natively_for_a_write_run():
    cmd = AGY.build_command("implement #7", "shepherd", cwd=TREE)
    print_text = cmd[cmd.index("--print") + 1]
    assert "--agent" not in cmd
    assert (
        print_text
        == "You are acting as the 'shepherd' role for this Run.\n\nimplement #7"
    )
    assert "shepherd" in print_text
    assert print_text.endswith("implement #7")


def test_build_command_never_emits_a_tools_flag():
    assert "--tools" not in AGY.build_command("t", "reviewer", cwd=TREE, read_only=True)
    assert "--tools" not in AGY.build_command("t", "implementer", cwd=TREE)


def test_build_command_honours_construction_model_and_timeout():
    adapter = agy_backend.AntigravityAdapter(
        model="Gemini 3.5 Flash (High)", timeout="900s"
    )
    cmd = adapter.build_command("t", "implementer", cwd=TREE)
    assert "--model=Gemini 3.5 Flash (High)" in cmd
    assert "--print-timeout=900s" in cmd


def test_build_command_rejects_oversized_print_prompt_before_exec():
    task = "x" * agy_backend.MAX_PRINT_ARG_BYTES
    with pytest.raises(ValueError, match="too large for portable argv delivery"):
        AGY.build_command(task, "reviewer", cwd=TREE, read_only=True)


def test_default_model_resolves_pro_to_a_capable_non_agentic_name():
    assert AGY.model == "Gemini 3.1 Pro (High)"
    assert agy_backend.resolve_model("pro") == "Gemini 3.1 Pro (High)"
    assert agy_backend.resolve_model("Gemini 3.1 Pro (High)") == "Gemini 3.1 Pro (High)"


def test_child_env_scrubs_agy_auth_vars():
    parent = {
        "PATH": "/bin",
        "GEMINI_API_KEY": "stale",
        "GOOGLE_API_KEY": "also-stale",
        "HOME": "/home/a",
    }

    env = AGY.child_env(parent)

    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env == {"PATH": "/bin", "HOME": "/home/a"}


def test_child_env_without_keys_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = AGY.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-os-environ")
    monkeypatch.setenv("GOOGLE_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = AGY.child_env()

    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


def test_registry_resolves_the_antigravity_adapter():
    adapter = backends.resolve("antigravity")
    assert isinstance(adapter, agy_backend.AntigravityAdapter)
    assert adapter.name == "antigravity"
    assert "antigravity" in backends.supported_backends()


def test_output_schema_path_is_accepted_and_ignored():
    cmd = AGY.build_command(
        "t", "reviewer", read_only=True, cwd="/tree", output_schema_path="/tmp/s.json"
    )
    assert "--output-schema" not in cmd
    assert "/tmp/s.json" not in cmd


def test_agy_has_no_reasoning_knob_and_reports_none():
    from shipit.spawn.backends import antigravity as agy_backend

    adapter = agy_backend.AntigravityAdapter()
    assert adapter.reasoning is None
    cmd = adapter.build_command("task", "reviewer", read_only=True, cwd="/tree")
    assert not any("effort" in arg or "reasoning" in arg for arg in cmd)
