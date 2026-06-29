"""`shipit hook pretooluse` — the `PreToolUse` coordinator-guard boundary.

THIN by design (ADR-0012): read the Claude Code `PreToolUse` payload on stdin →
resolve the acting role + decide (`shipit.harness`) → emit the
`hookSpecificOutput` decision on stdout. No logic beyond I/O marshalling; the
verdict lives in the pure core.

**Fail-open is the contract.** This hook runs on EVERY matching tool call in
real sessions — including this repo's own dev loop — so ANY unexpected internal
error (bad stdin, malformed JSON, a missing field, an exception) must result in
*no block*: the boundary swallows it, logs at DEBUG, and falls through to ALLOW.
Only the one intended path — a `coordinator` `edit` on a code path — emits a
`deny`. Exit code is 0 in all normal cases.

An ALLOW emits NOTHING (empty stdout): the hook declines to decide, so Claude
Code's normal permission flow proceeds unchanged. The guard never auto-approves
a tool — it only ever *blocks* the coordinator's code edits — which is what
keeps it safe to run on every call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import TextIO

import click

from ...harness import breakglass
from ...harness.codepath import is_code_path
from ...harness.policy import (
    Decision,
    Permission,
    decide,
    decide_worktree,
    is_edit_tool,
)
from ...harness.role import resolve_role

logger = logging.getLogger("shipit.hook")


@click.command(name="pretooluse")
def cmd() -> None:
    """Decide a `PreToolUse` tool call: deny a coordinator code edit, else allow.

    Reads the hook payload as JSON on stdin and writes the Claude Code decision
    JSON to stdout. Always exits 0; fails OPEN (allow) on any malformed input.
    """
    raise SystemExit(run())


def run(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Parse stdin → decide → emit. Returns 0 always (fail-open).

    Wraps the entire parse/resolve/decide path so a bad payload can never crash
    or spuriously deny — any exception falls through to an ALLOW.
    """
    out = stdout if stdout is not None else sys.stdout
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        tool_name = str(payload.get("tool_name", ""))
        # First gate: the native-worktree deny (TRE01 / ADR-0014). Independent of
        # role and break-glass, and checked BEFORE the edit-tool gate because the
        # worktree-creating calls (EnterWorktree / Bash) are not edit tools and
        # would otherwise be allowed straight through.
        worktree = decide_worktree(
            tool_name, _extract_command(payload.get("tool_input"))
        )
        if worktree.permission is Permission.DENY:
            logger.debug("pretooluse DENY: native worktree blocked tool=%s", tool_name)
            _emit_deny(worktree.reason, out)
            return 0
        if not is_edit_tool(tool_name):
            return 0  # not an edit operation — allow silently, never block.
        role = resolve_role(payload)
        path = _extract_path(payload.get("tool_input"))
        is_code = is_code_path(path)
        break_glass = _break_glass_armed()
        # Log every break-glass use that would otherwise have been a deny — its
        # frequency is the HAR02 signal for whether to tighten the policy. The
        # pure verdict (with break_glass off) is the single source of truth for
        # "would this have blocked?", so the log can never drift from the rule.
        if (
            break_glass
            and decide(role, path, is_code, False).permission is Permission.DENY
        ):
            logger.warning(
                "break-glass: coordinator code edit PERMITTED role=%s tool=%s path=%s",
                role.value,
                tool_name,
                path,
            )
        decision = decide(role, path, is_code, break_glass)
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        logger.debug("pretooluse hook failed open (allowing)", exc_info=True)
        decision = Decision(permission=Permission.ALLOW)

    if decision.permission is Permission.DENY:
        logger.debug(
            "pretooluse DENY: role=coordinator op=edit reason=%r", decision.reason
        )
        _emit_deny(decision.reason, out)
    return 0


def _extract_path(tool_input: object) -> str:
    """Pull the edited path off a `tool_input` payload, or `""` if absent.

    Edit/Write/MultiEdit carry `file_path`; NotebookEdit carries `notebook_path`.
    A non-dict input (malformed, or a tool with no path) yields `""`, which the
    classifier treats as non-code — fail-open.
    """
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")


def _extract_command(tool_input: object) -> str:
    """Pull the shell command off a Bash `tool_input` payload, or `""` if absent.

    The native-worktree guard inspects this for `git worktree add`. A non-dict
    input (a non-Bash tool, or malformed) yields `""`, which matches no rule.
    """
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("command") or "")


def _break_glass_armed() -> bool:
    """Read the break-glass env marker — a BOUNDARY (impure) concern.

    Kept out of `decide()` so the verdict stays pure: the boolean is passed in.
    The env name + falsey spellings live in :mod:`shipit.harness.breakglass`, shared
    with the eval break-glass grep so the two cannot drift.
    """
    return breakglass.is_armed(os.environ.get(breakglass.ENV, ""))


def _emit_deny(reason: str, out: TextIO) -> None:
    """Write the Claude Code `deny` decision JSON (overrides `bypassPermissions`)."""
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        out,
    )
    out.write("\n")
