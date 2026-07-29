"""The ``codex`` backend adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ...agent.backend import CODEX as _IDENTITY
from ...harness.prompts import load_role_defs, render
from ...harness.role import Role
from .base import BackendAdapter

#: Scrubbed: either would shadow the ChatGPT login or flip the Run onto API billing.
AUTH_ENV_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")

#: Deliberately NOT scrubbed: headless automation with no persisted login needs it.
ACCESS_TOKEN_VAR = "CODEX_ACCESS_TOKEN"

MODEL_ALIASES = _IDENTITY.model_aliases

#: The identity types it optional; codex always pins one, so narrow to ``str``.
assert _IDENTITY.default_model is not None
DEFAULT_MODEL: str = _IDENTITY.default_model


def resolve_model(model: str) -> str:
    return _IDENTITY.resolve_model(model)


#: Re-grants the network the reviewer's ``workspace-write`` sandbox would block.
NETWORK_ACCESS_OVERRIDE = "sandbox_workspace_write.network_access=true"

REASONING_EFFORT_KEY = "model_reasoning_effort"

# Carries the generated role slice; the positional prompt stays the task brief.
DEVELOPER_INSTRUCTIONS_KEY = "developer_instructions"


def _role_preamble(role: str) -> str:
    return f"You are acting as the '{role}' role for this Run."


def _role_instructions(role: str) -> str:
    try:
        known = Role(role)
    except ValueError:
        return _role_preamble(role)
    return render(load_role_defs()).role_prompts[known]


class CodexAdapter(BackendAdapter):
    """The headless-``codex`` backend."""

    name = _IDENTITY.name

    def __init__(
        self, model: str = DEFAULT_MODEL, reasoning: str | None = None
    ) -> None:
        self.model = resolve_model(model)
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
        """The ``codex exec`` argv; a schema is honoured only for a read-only Run."""
        # A write Run needs the bypass posture: the default `workspace-write` sandbox
        # blocks `.git` writes and the network. The reviewer keeps that sandbox but
        # re-grants the network, since `read-only` blocks it outright.
        del cwd  # codex roots via the process cwd; no path belongs in its argv.
        prompt = task
        posture = (
            [
                "--ephemeral",
                "--sandbox",
                "workspace-write",
                "-c",
                NETWORK_ACCESS_OVERRIDE,
            ]
            if read_only
            else ["--dangerously-bypass-approvals-and-sandbox"]
        )
        if read_only and output_schema_path is not None:
            posture += ["--output-schema", output_schema_path]
        reasoning = (
            ["-c", f"{REASONING_EFFORT_KEY}={self.reasoning}"]
            if self.reasoning is not None
            else []
        )
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            *posture,
            *reasoning,
            "-c",
            f"{DEVELOPER_INSTRUCTIONS_KEY}={_role_instructions(role)}",
            "--model",
            self.model,
            prompt,
        ]

    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The parent's environment with :data:`AUTH_ENV_VARS` removed."""
        source = os.environ if parent_env is None else parent_env
        return {key: value for key, value in source.items() if key not in AUTH_ENV_VARS}
