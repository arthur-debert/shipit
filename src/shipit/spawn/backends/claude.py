"""The ``claude`` backend adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ...agent.backend import CLAUDE as _IDENTITY
from .base import BackendAdapter

#: MUST be removed from the child env: a stale value takes precedence over the
#: claude.ai OAuth/keychain login and breaks auth.
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

#: The read-only ``--tools`` allow-list for a reviewer Run: ``Bash`` is in it so the
#: reviewer can run ``gh pr diff``; ``Write`` / ``Edit`` are deliberately out.
REVIEWER_TOOLS = ("Read", "Grep", "Glob", "Bash")


class ClaudeAdapter(BackendAdapter):
    """The headless-``claude`` backend; a ``None`` model or reasoning omits its flag."""

    name = _IDENTITY.name

    def __init__(self, model: str | None = None, reasoning: str | None = None) -> None:
        self.model = model
        self.reasoning = reasoning

    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The ``claude`` print-mode argv."""
        # `--agent <role>` populates the hook payload's `agent_type`; without it the
        # guard reads the child as a coordinator and forbids its own edits.
        # claude roots via the process cwd and never carries an output schema.
        del cwd, output_schema_path
        cmd = [
            "claude",
            "-p",
            task,
            "--agent",
            role,
            "--permission-mode",
            "bypassPermissions",
        ]
        if self.model is not None:
            cmd += ["--model", self.model]
        if self.reasoning is not None:
            cmd += ["--effort", self.reasoning]
        if read_only:
            cmd += ["--tools", ",".join(REVIEWER_TOOLS)]
        cmd += ["--output-format", "json"]
        return cmd

    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The parent's environment with ``ANTHROPIC_API_KEY`` removed."""
        source = os.environ if parent_env is None else parent_env
        return {key: value for key, value in source.items() if key != ANTHROPIC_API_KEY}
