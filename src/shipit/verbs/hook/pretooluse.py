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
import sys
from typing import TextIO

import click

from ...harness.policy import Decision, Permission, decide
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
        role = resolve_role(payload)
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input")
        path = (
            str(tool_input.get("file_path", "")) if isinstance(tool_input, dict) else ""
        )
        decision = decide(role, tool_name, path)
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        logger.debug("pretooluse hook failed open (allowing)", exc_info=True)
        decision = Decision(permission=Permission.ALLOW)

    if decision.permission is Permission.DENY:
        logger.debug(
            "pretooluse DENY: role=coordinator op=edit reason=%r", decision.reason
        )
        _emit_deny(decision.reason, out)
    return 0


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
