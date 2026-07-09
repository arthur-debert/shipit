"""Hook boundary: a `PreToolUse` payload on stdin -> the decision JSON on stdout.

A thin integration test (not broad coverage): the deny path emits the Claude
Code `hookSpecificOutput`, the allow path emits nothing, and ANY malformed input
fails OPEN (no output, exit 0) — the dogfooding-safety contract.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from shipit.harness import breakglass
from shipit.harness.policy import COORDINATOR_DENY_REASON, WORKTREE_DENY_REASON
from shipit.verbs.hook.pretooluse import run


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
    assert out == ""  # allow == no decision; normal permission flow proceeds


def test_coordinator_doc_edit_is_allowed_silently():
    payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": "docs/prd/har01.md"}}
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_non_edit_tool_is_allowed_silently():
    # The hook fires for any matched tool; a non-edit one never reaches the
    # verdict and is allowed (no output), even for the coordinator on a code path.
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "src/shipit/cli.py"}}
    )
    code, out = _run(payload)
    assert code == 0
    assert out == ""


def test_enter_worktree_is_denied():
    # The native-worktree guard fires even though EnterWorktree is not an edit
    # tool, and regardless of role (no agent_type ⇒ coordinator).
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
        # A leading global option must NOT smuggle `worktree add` past the wall.
        "git -C /repo worktree add ../t b",
        "git --no-pager worktree add ../t b",
    ],
)
def test_bash_git_worktree_add_is_denied(command):
    payload = json.dumps(
        {
            "agent_type": "implementer",  # role-independent: a subagent is blocked too
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


def test_break_glass_permits_the_edit_and_logs_it(monkeypatch, caplog):
    monkeypatch.setenv(breakglass.ENV, "1")
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/shipit/cli.py"}}
    )
    with caplog.at_level(logging.WARNING, logger="shipit.hook"):
        code, out = _run(payload)
    assert code == 0
    assert out == ""  # break-glass converts the would-be deny into a silent allow
    # The use is recorded LOUD (an HAR02 frequency signal), with role/tool/path.
    assert any(
        "break-glass" in r.message
        and "coordinator" in r.message
        and "src/shipit/cli.py" in r.message
        for r in caplog.records
    )


def test_break_glass_does_not_log_when_no_edit_would_be_blocked(monkeypatch, caplog):
    # Break-glass armed, but a subagent edit was never going to be denied — so
    # there is nothing to break through and nothing to log.
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
        "",  # empty stdin
        "not json at all",
        "{",  # truncated json
        "[]",  # valid json, wrong shape
        json.dumps({"tool_name": "Edit"}),  # missing tool_input
        json.dumps({"tool_input": {"file_path": "src/x.py"}}),  # missing tool_name
        json.dumps({"tool_name": "Edit", "tool_input": "not-a-dict"}),
    ],
)
def test_fails_open_on_malformed_input(garbage):
    code, out = _run(garbage)
    assert code == 0
    assert out == ""  # no block on bad input
