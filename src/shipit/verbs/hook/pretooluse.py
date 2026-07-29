"""`shipit hook pretooluse` — the `PreToolUse` coordinator-guard boundary."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from typing import TextIO

import click

from ... import logcontext
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
    """Decide a `PreToolUse` tool call: deny a coordinator code edit, else allow."""
    raise SystemExit(run())


def run(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Returns 0 always — fail-open: an undecidable call is never denied."""
    out = stdout if stdout is not None else sys.stdout
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        tool_name = str(payload.get("tool_name", ""))
        worktree = decide_worktree(
            tool_name, _extract_command(payload.get("tool_input"))
        )
        if worktree.permission is Permission.DENY:
            logger.debug("pretooluse DENY: native worktree blocked tool=%s", tool_name)
            _emit_deny(worktree.reason, out)
            return 0
        if not is_edit_tool(tool_name):
            return 0
        role = resolve_role(payload, fallback_role=_spawned_role_from_env(os.environ))
        paths = _extract_paths(payload.get("tool_input"))
        code_paths = tuple(p for p in paths if is_code_path(p))
        path = _display_path(paths, code_paths)
        is_code = bool(code_paths)
        break_glass = _break_glass_armed()
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
        logger.warning("pretooluse hook failed open (allowing)", exc_info=True)
        decision = Decision(permission=Permission.ALLOW)

    if decision.permission is Permission.DENY:
        logger.debug(
            "pretooluse DENY: role=coordinator op=edit reason=%r", decision.reason
        )
        _emit_deny(decision.reason, out)
    return 0


_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete|Rename) File:|Move to:) (.+)$",
    re.MULTILINE,
)


def _clean_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for raw in paths if (p := raw.strip()))


def _display_path(paths: tuple[str, ...], code_paths: tuple[str, ...]) -> str:
    selected = code_paths if code_paths else paths
    return ", ".join(selected)


def _spawned_role_from_env(env: Mapping[str, str]) -> str | None:
    """The spawned role, only when the env carries a spawned Run marker."""
    if not (env.get(logcontext.ENV_PREFIX + "AGENT") or "").strip():
        return None
    return logcontext.role_from_env(env)


def _extract_paths(tool_input: object) -> tuple[str, ...]:
    """The edited paths on a `tool_input` payload, or ``()`` if absent."""
    if isinstance(tool_input, str):
        return _clean_paths(tuple(_PATCH_FILE_RE.findall(tool_input)))
    if not isinstance(tool_input, dict):
        return ()
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if path:
        return _clean_paths((str(path),))
    for key in ("patch", "input", "text"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths = _clean_paths(tuple(_PATCH_FILE_RE.findall(value)))
            if paths:
                return paths
    return ()


def _extract_command(tool_input: object) -> str:
    """The shell command on a Bash `tool_input` payload, or `""` if absent."""
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("command") or "")


def _break_glass_armed() -> bool:
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
