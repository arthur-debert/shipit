from __future__ import annotations

import io
import json
import logging

import pytest
from click.testing import CliRunner

from shipit.harness import breakglass
from shipit.harness.policy import (
    COORDINATOR_DENY_REASON,
    SPAWN_ISOLATION_DENY_REASON,
    WORKTREE_DENY_REASON,
)
from shipit.verbs.hook import bashguard, hook, pretooluse
from shipit.verbs.hook.pretooluse import run


@pytest.fixture(autouse=True)
def _scrub_ambient_role_env(monkeypatch):
    monkeypatch.delenv("SHIPIT_LOG_CTX_ROLE", raising=False)
    monkeypatch.delenv("SHIPIT_LOG_CTX_AGENT", raising=False)


def _run(payload_text: str) -> tuple[int, str]:
    out = io.StringIO()
    code = run(stdin=io.StringIO(payload_text), stdout=out)
    return code, out.getvalue()


def test_coordinator_code_edit_is_denied():
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/shipit/cli.py"}}
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == COORDINATOR_DENY_REASON


def test_codex_apply_patch_code_edit_is_denied():
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/shipit/cli.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == COORDINATOR_DENY_REASON


def test_codex_apply_patch_strips_patch_header_paths_before_classifying():
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\r\n"
                "*** Update File: src/shipit/cli.py  \r\n"
                "@@\r\n"
                "-old\r\n"
                "+new\r\n"
                "*** End Patch\r\n"
            ),
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_codex_apply_patch_denies_if_any_patched_file_is_code():
    payload = json.dumps(
        {
            "tool_name": "functions.apply_patch",
            "tool_input": {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: README.md\n"
                    "@@\n"
                    "-docs\n"
                    "+docs\n"
                    "*** Update File: src/shipit/cli.py\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch\n"
                )
            },
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_codex_apply_patch_code_edit_with_spawned_env_role_is_allowed(monkeypatch):
    monkeypatch.setenv("SHIPIT_LOG_CTX_AGENT", "deadbeef")
    monkeypatch.setenv("SHIPIT_LOG_CTX_ROLE", "implementer")
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/shipit/cli.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_codex_apply_patch_code_edit_with_ambient_env_role_is_denied(monkeypatch):
    monkeypatch.delenv("SHIPIT_LOG_CTX_AGENT", raising=False)
    monkeypatch.setenv("SHIPIT_LOG_CTX_ROLE", "implementer")
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/shipit/cli.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == COORDINATOR_DENY_REASON


def test_codex_apply_patch_code_edit_without_spawned_env_role_is_denied(monkeypatch):
    monkeypatch.delenv("SHIPIT_LOG_CTX_AGENT", raising=False)
    monkeypatch.delenv("SHIPIT_LOG_CTX_ROLE", raising=False)
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/shipit/cli.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_codex_apply_patch_extracts_rename_and_move_paths():
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Rename File: docs/old.md\n"
                "*** Update File: README.md\n"
                "*** Move to: src/shipit/cli.py\n"
                "*** End Patch\n"
            ),
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_break_glass_logs_the_code_path_from_a_multi_file_codex_patch(
    monkeypatch, caplog
):
    monkeypatch.setenv(breakglass.ENV, "1")
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: README.md\n"
                "@@\n"
                "-docs\n"
                "+docs\n"
                "*** Update File: src/shipit/cli.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
    )
    with caplog.at_level(logging.WARNING, logger="shipit.hook"):
        code, out = _run(payload)
    assert code == 0
    assert out == ""
    assert any(
        "break-glass" in r.message and "src/shipit/cli.py" in r.message
        for r in caplog.records
    )
    assert not any(
        "break-glass" in r.message and "README.md" in r.message for r in caplog.records
    )


def test_break_glass_logs_all_code_paths_from_a_multi_file_codex_patch(
    monkeypatch, caplog
):
    monkeypatch.setenv(breakglass.ENV, "1")
    payload = json.dumps(
        {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/shipit/cli.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** Update File: src/shipit/session/bootstrap.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        }
    )
    with caplog.at_level(logging.WARNING, logger="shipit.hook"):
        code, out = _run(payload)
    assert code == 0
    assert out == ""
    assert any(
        "break-glass" in r.message
        and "src/shipit/cli.py, src/shipit/session/bootstrap.py" in r.message
        for r in caplog.records
    )


def test_subagent_code_edit_is_allowed_silently():
    payload = json.dumps(
        {
            "agent_type": "implementer",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/shipit/cli.py"},
        }
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_coordinator_doc_edit_is_allowed_silently():
    payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": "docs/spec/har01.md"}}
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_non_edit_tool_is_allowed_silently():
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "src/shipit/cli.py"}}
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_enter_worktree_is_denied():
    payload = json.dumps({"tool_name": "EnterWorktree", "tool_input": {}})
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == WORKTREE_DENY_REASON


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add ../tree-x my-branch",
        "git -C /repo worktree add ../t b",
        "git --no-pager worktree add ../t b",
    ],
)
def test_bash_git_worktree_add_is_denied(command):
    payload = json.dumps(
        {
            "agent_type": "implementer",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == WORKTREE_DENY_REASON


@pytest.mark.parametrize(
    "command",
    ["git status", "git checkout -b x", "git fetch origin", "git worktree list"],
)
def test_ordinary_git_bash_is_allowed_silently(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_codex_shell_git_worktree_add_is_denied():
    payload = json.dumps(
        {
            "tool_name": "exec_command",
            "cwd": "/repo",
            "tool_input": {"cmd": "git worktree add ../t b", "workdir": "/repo"},
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecisionReason"] == WORKTREE_DENY_REASON


def test_un_isolated_tree_backed_spawn_is_denied():
    payload = json.dumps(
        {
            "tool_name": "Agent",
            "cwd": "/trees/coordinator",
            "tool_input": {
                "description": "d",
                "prompt": "p",
                "subagent_type": "implementer",
            },
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == SPAWN_ISOLATION_DENY_REASON


@pytest.mark.parametrize(
    "tool_input",
    [
        {"subagent_type": "implementer", "isolation": "worktree"},
        {"subagent_type": "explorer"},
        {"subagent_type": "fork"},
        {"subagent_type": "general-purpose"},
        {"subagent_type": "claude"},
        {"subagent_type": "Explore"},
        {"subagent_type": "Plan"},
        {},
    ],
)
def test_allowable_spawns_are_allowed_silently(tool_input):
    payload = json.dumps({"tool_name": "Agent", "tool_input": tool_input})
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_the_deny_log_line_carries_the_callers_cwd(caplog):
    payload = json.dumps(
        {
            "tool_name": "Agent",
            "cwd": "/trees/coordinator",
            "tool_input": {"subagent_type": "implementer"},
        }
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.hook"):
        _run(payload)
    assert "/trees/coordinator" in caplog.text


def test_bashguard_is_the_same_decider_under_a_second_name():
    """The two verbs differ only in name, so the host can wire two failure modes."""
    assert bashguard.run is pretooluse.run
    assert hook.commands["bashguard"] is bashguard.cmd
    assert hook.commands["pretooluse"] is pretooluse.cmd


def test_bashguard_denies_an_un_isolated_spawn_through_the_cli():
    payload = json.dumps(
        {"tool_name": "Agent", "tool_input": {"subagent_type": "implementer"}}
    )
    result = CliRunner().invoke(bashguard.cmd, [], input=payload)
    assert result.exit_code == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecisionReason"] == SPAWN_ISOLATION_DENY_REASON


def test_break_glass_permits_the_edit_and_logs_it(monkeypatch, caplog):
    monkeypatch.setenv(breakglass.ENV, "1")
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/shipit/cli.py"}}
    )
    with caplog.at_level(logging.WARNING, logger="shipit.hook"):
        code, out = _run(payload)
    assert code == 0
    assert out == ""
    assert any(
        "break-glass" in r.message
        and "coordinator" in r.message
        and "src/shipit/cli.py" in r.message
        for r in caplog.records
    )


def test_break_glass_does_not_log_when_no_edit_would_be_blocked(monkeypatch, caplog):
    monkeypatch.setenv(breakglass.ENV, "1")
    payload = json.dumps(
        {
            "agent_type": "implementer",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/shipit/cli.py"},
        }
    )
    with caplog.at_level(logging.WARNING, logger="shipit.hook"):
        code, out = _run(payload)
    assert code == 0
    assert out == ""
    assert not any("break-glass" in r.message for r in caplog.records)


@pytest.mark.parametrize("falsey", ["", "0", "false", "no", "off"])
def test_falsey_break_glass_still_denies(monkeypatch, falsey):
    monkeypatch.setenv(breakglass.ENV, falsey)
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/shipit/cli.py"}}
    )
    code, out = _run(payload)
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "not json at all",
        "{",
        "[]",
        json.dumps({"tool_name": "Edit"}),
        json.dumps({"tool_input": {"file_path": "src/x.py"}}),
        json.dumps({"tool_name": "Edit", "tool_input": "not-a-dict"}),
    ],
)
def test_fails_open_on_malformed_input(garbage):
    code, out = _run(garbage)
    assert code == 0
    assert out == ""


CROSS_TREE_OWN = "shipit-claude-20260730-015230-141f2c5a-1ca0-4170-b86d-b3a10f3e8c3a"
CROSS_TREE_OTHER = "shipit-claude-20260729-233341-297e983f-54ce-4a8f-8afa-c9dea2a23c8b"


@pytest.fixture
def cross_tree(monkeypatch, tmp_path):
    """A Trees root with the caller's Tree and one other, as `(own_dir, other_dir)`."""
    root = tmp_path / "trees"
    own, other = root / CROSS_TREE_OWN, root / CROSS_TREE_OTHER
    for path in (own, other):
        path.mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(root))
    return own, other


def test_bash_write_into_another_tree_is_denied_through_the_hook(cross_tree):
    """The decider must be REACHED from `run`, not merely exist."""
    own, other = cross_tree
    payload = json.dumps(
        {
            "agent_type": "implementer",
            "tool_name": "Bash",
            "cwd": str(own),
            "tool_input": {
                "command": f"cd {other} && python3 /tmp/s.py apply src/x.py"
            },
        }
    )
    code, out = _run(payload)
    assert code == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert CROSS_TREE_OTHER in decision["permissionDecisionReason"]


def test_reading_another_tree_stays_allowed_through_the_hook(cross_tree):
    own, other = cross_tree
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "cwd": str(own),
            "tool_input": {"command": f"grep -rn foo {other}/src"},
        }
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_the_bashguard_entry_denies_a_cross_tree_write_and_still_exits_zero(cross_tree):
    """The advisory entry's contract: it emits `deny` on stdout, and its own exit stays 0."""
    own, other = cross_tree
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "cwd": str(own),
            "tool_input": {"command": f"tee {other}/src/x.py"},
        }
    )
    result = CliRunner().invoke(bashguard.cmd, input=payload)
    assert result.exit_code == 0
    assert json.loads(result.output)["hookSpecificOutput"]["permissionDecision"] == (
        "deny"
    )


def test_a_coordinator_edit_in_another_tree_still_reports_the_edit_reason(cross_tree):
    """The cross-Tree rule must not shadow the fail-closed edit guard's own verdict."""
    own, _other = cross_tree
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "cwd": str(own),
            "tool_input": {"file_path": "src/shipit/cli.py"},
        }
    )
    code, out = _run(payload)
    assert code == 0
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == COORDINATOR_DENY_REASON
