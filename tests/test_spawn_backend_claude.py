"""Unit tests for the ``claude`` backend adapter and the registry (ADR-0019 / ADR-0020).

The per-backend launch contract — the exact ``claude -p … --agent`` argv, the
``ANTHROPIC_API_KEY`` scrub, and the read-only reviewer allow-list — moved from
``shipit.spawn.launch`` to the :class:`~shipit.spawn.backends.claude.ClaudeAdapter`
behind the WS01 seam, with **zero behaviour change**. These are the same assertions
that pinned that contract, now driven through the adapter, plus registry coverage for
the adapter-driven ``SUPPORTED_BACKENDS`` (ADR-0020 §Decision 2).
"""

from __future__ import annotations

from shipit.spawn import backends
from shipit.spawn.backends import claude as claude_backend

CLAUDE = claude_backend.ClaudeAdapter()


def test_build_command_is_the_adr_contract():
    cmd = CLAUDE.build_command("do the thing", "implementer")

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
    cmd = CLAUDE.build_command("t", "shepherd")
    assert cmd[cmd.index("--agent") + 1] == "shepherd"


def test_build_command_omits_tools_when_none():
    # A write Run passes no allow-list: the --tools flag must be absent so the role
    # inherits its full toolset.
    cmd = CLAUDE.build_command("t", "implementer")
    assert "--tools" not in cmd


def test_build_command_adds_readonly_tools_for_a_reviewer():
    # A reviewer narrows tool access (ADR-0019 §4): --tools carries the read-only
    # allow-list as a comma-joined string, and crucially excludes Write/Edit.
    cmd = CLAUDE.build_command("t", "reviewer", tools=CLAUDE.reviewer_tools)
    allowlist = cmd[cmd.index("--tools") + 1]
    assert allowlist == "Read,Grep,Glob,Bash"
    assert "Write" not in allowlist and "Edit" not in allowlist
    # The flag sits before --output-format, preserving the envelope arg at the tail.
    assert cmd.index("--tools") < cmd.index("--output-format")


def test_reviewer_tools_is_the_readonly_posture():
    # claude HAS a native allow-list, so the read-only posture is a concrete tuple
    # (defense-in-depth atop the chmod'd Tree), not None.
    assert CLAUDE.reviewer_tools == ("Read", "Grep", "Glob", "Bash")
    assert "Write" not in CLAUDE.reviewer_tools
    assert "Edit" not in CLAUDE.reviewer_tools


def test_child_env_scrubs_anthropic_api_key():
    parent = {"PATH": "/bin", "ANTHROPIC_API_KEY": "stale-key", "HOME": "/home/a"}

    env = CLAUDE.child_env(parent)

    # The hard contract requirement (ADR-0019 §3): the key is gone, the rest stays.
    assert "ANTHROPIC_API_KEY" not in env
    assert env == {"PATH": "/bin", "HOME": "/home/a"}


def test_child_env_without_key_is_a_plain_copy():
    parent = {"PATH": "/bin"}
    env = CLAUDE.child_env(parent)
    assert env == {"PATH": "/bin"}
    assert env is not parent  # a copy, never the caller's dict


def test_child_env_defaults_to_os_environ_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-os-environ")
    monkeypatch.setenv("SHIPIT_SPAWN_MARKER", "present")

    env = CLAUDE.child_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("SHIPIT_SPAWN_MARKER") == "present"


def test_registry_includes_claude_and_codex():
    # SUPPORTED_BACKENDS is adapter-driven (ADR-0020 §Decision 2): derived from the
    # registry. claude (adapter #0) and codex (WS02) are wired; antigravity lands in
    # WS03. Registration order is preserved, claude first.
    assert backends.supported_backends() == ("claude", "codex")


def test_resolve_returns_the_claude_adapter():
    adapter = backends.resolve("claude")
    assert isinstance(adapter, claude_backend.ClaudeAdapter)
    assert adapter.name == "claude"


def test_resolve_unknown_backend_raises():
    # resolve() is reached only after the verb's explicit SUPPORTED_BACKENDS guard;
    # an unregistered key is a belt-and-braces KeyError, never a silent claude default.
    # "nonexistent" is a permanently-unregistered token (never a real/planned backend),
    # so this guardrail stays meaningful as more adapters land (e.g. antigravity in WS03).
    import pytest

    with pytest.raises(KeyError):
        backends.resolve("nonexistent")
