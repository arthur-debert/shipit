"""Hook boundary: a `PreToolUse` payload on stdin -> the decision JSON on stdout.

A thin integration test (not broad coverage): the deny path emits the Claude
Code `hookSpecificOutput`, the allow path emits nothing, and ANY malformed input
fails OPEN (no output, exit 0) — the dogfooding-safety contract.
"""

from __future__ import annotations

import io
import json

import pytest
from shipit.harness.policy import COORDINATOR_DENY_REASON
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
