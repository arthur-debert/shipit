"""Unit tests for the ``antigravity`` (``agy``) backend adapter (ADR-0020 §Decision-per-backend).

The per-backend WRITE launch contract recorded by the WS00 spike — the exact
``agy --new-project --add-dir … --print`` argv (with the load-bearing ``--add-dir``
cwd-rooting quirk and ``--dangerously-skip-permissions``), the ``GEMINI_API_KEY`` /
``GOOGLE_API_KEY`` scrub, the native role-prepend, and the ``None`` read-only posture —
asserted through the adapter and the injectable seam, mirroring
``test_spawn_backend_claude.py``. No real ``agy`` binary is ever invoked.
"""

from __future__ import annotations

import pytest

from shipit.spawn import backends
from shipit.spawn.backends import antigravity as agy_backend

AGY = agy_backend.AntigravityAdapter()
TREE = "/trees/widget/TRE05-WS03"


def test_build_command_is_the_adr_write_contract():
    cmd = AGY.build_command("do the thing", "implementer", cwd=TREE)

    # The literal ADR-0020 §Decision-per-backend WRITE invocation, in order. The model
    # is the resolved verbatim agy name (alias `pro` -> capable, non-agentic model).
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
    # The cwd-rooting invariant (ADR-0020 §Decision 3): agy IGNORES its process cwd, so
    # the Tree path must appear in the argv as `--add-dir <Tree>` or writes land in agy's
    # scratch dir. `--new-project` immediately precedes it (establishes the workspace).
    cmd = AGY.build_command("t", "implementer", cwd=TREE)
    add_dir = cmd.index("--add-dir")
    assert cmd[add_dir + 1] == TREE
    assert cmd[add_dir - 1] == "--new-project"


def test_build_command_requires_cwd_for_the_tree_root():
    # Fail-closed on the cwd-rooting invariant: without the Tree path agy would silently
    # write to its scratch dir, so a missing cwd is a loud error, never a degraded Run.
    with pytest.raises(ValueError, match="requires cwd"):
        AGY.build_command("t", "implementer")


def test_build_command_carries_bypass_permissions_for_a_write_run():
    # `--dangerously-skip-permissions` is agy's bypassPermissions equivalent (ADR-0020):
    # a non-interactive --print WRITE Run stalls on permission prompts without it.
    cmd = AGY.build_command("t", "implementer", cwd=TREE)
    assert "--dangerously-skip-permissions" in cmd


def test_build_command_prepends_the_role_natively():
    # agy has NO --agent flag, so the role rides in the --print text (prompt-prepend,
    # ADR-0020). The role name appears, and the original task is preserved verbatim.
    cmd = AGY.build_command("implement #7", "shepherd", cwd=TREE)
    print_text = cmd[cmd.index("--print") + 1]
    assert (
        print_text
        == "You are acting as the 'shepherd' role for this Run.\n\nimplement #7"
    )
    assert "shepherd" in print_text
    assert print_text.endswith("implement #7")


def test_build_command_ignores_tools_no_native_allowlist():
    # agy has no native tool allow-list (reviewer_tools is None); passing `tools` must
    # NOT inject any --tools flag — read-only rides the chmod'd Tree (ADR-0018).
    cmd = AGY.build_command("t", "reviewer", cwd=TREE, tools=("Read", "Grep"))
    assert "--tools" not in cmd
    assert "Read,Grep" not in cmd


def test_build_command_honours_construction_model_and_timeout():
    # A consumer can pin a different model/timeout; both flow into the argv. A bare-name
    # model is passed verbatim (only aliases are resolved).
    adapter = agy_backend.AntigravityAdapter(
        model="Gemini 3.5 Flash (High)", timeout="900s"
    )
    cmd = adapter.build_command("t", "implementer", cwd=TREE)
    assert "--model=Gemini 3.5 Flash (High)" in cmd
    assert "--print-timeout=900s" in cmd


def test_default_model_resolves_pro_to_a_capable_non_agentic_name():
    # The default alias `pro` MUST NOT resolve to Flash (which goes agentic in --print
    # and never answers): it is pinned to the capable Gemini 3.1 Pro (High).
    assert AGY.model == "Gemini 3.1 Pro (High)"
    assert agy_backend.resolve_model("pro") == "Gemini 3.1 Pro (High)"
    # An already-verbatim name passes through untouched.
    assert agy_backend.resolve_model("Gemini 3.1 Pro (High)") == "Gemini 3.1 Pro (High)"


def test_child_env_scrubs_agy_auth_vars():
    parent = {
        "PATH": "/bin",
        "GEMINI_API_KEY": "stale",
        "GOOGLE_API_KEY": "also-stale",
        "HOME": "/home/a",
    }

    env = AGY.child_env(parent)

    # Both auth vars are gone so agy's Antigravity OAuth login wins; everything else stays.
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env == {"PATH": "/bin", "HOME": "/home/a"}


def test_child_env_without_keys_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = AGY.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent  # a copy, never the caller's dict


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-os-environ")
    monkeypatch.setenv("GOOGLE_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = AGY.child_env()

    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


def test_reviewer_tools_is_none_no_native_allowlist():
    # agy has no native read-only allow-list, so the posture is None: read-only rides
    # SOLELY on the chmod'd shared Tree (ADR-0018), the load-bearing guard.
    assert AGY.reviewer_tools is None


def test_registry_resolves_the_antigravity_adapter():
    adapter = backends.resolve("antigravity")
    assert isinstance(adapter, agy_backend.AntigravityAdapter)
    assert adapter.name == "antigravity"
    assert "antigravity" in backends.supported_backends()
