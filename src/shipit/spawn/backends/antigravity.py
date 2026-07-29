"""The ``antigravity`` backend adapter, which shells out to the ``agy`` binary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ...agent.backend import ANTIGRAVITY as _IDENTITY
from .base import BackendAdapter

#: ``pro`` MUST stay pinned to a capable model: agy resolves a bare ``pro`` to
#: Flash, which in ``--print`` goes agentic instead of answering.
MODEL_ALIASES = _IDENTITY.model_aliases

#: The identity types it optional; antigravity always pins one, so narrow to ``str``.
assert _IDENTITY.default_model is not None
DEFAULT_MODEL: str = _IDENTITY.default_model

#: agy's own 5m ``--print`` default truncates a big write Run; this gives headroom.
DEFAULT_TIMEOUT = "600s"

# Stay under Linux's per-string exec ceiling (MAX_ARG_STRLEN, commonly 128 KiB).
MAX_PRINT_ARG_BYTES = 120 * 1024

SCRUBBED_AUTH_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def resolve_model(model: str) -> str:
    return _IDENTITY.resolve_model(model)


def role_prompt(task: str, role: str) -> str:
    return f"You are acting as the '{role}' role for this Run.\n\n{task}"


def _enforce_print_arg_limit(prompt: str) -> None:
    """Fail before ``execve`` if agy's single ``--print`` argument is too large."""
    size = len(prompt.encode("utf-8"))
    if size <= MAX_PRINT_ARG_BYTES:
        return
    raise ValueError(
        "antigravity (agy) --print prompt is too large for portable argv "
        f"delivery ({size} bytes > {MAX_PRINT_ARG_BYTES} bytes). agy exposes no "
        "documented stdin or prompt-file transport for print mode; reduce the "
        "review diff, split the range, or use a backend with non-argv prompt "
        "delivery."
    )


class AntigravityAdapter(BackendAdapter):
    """The headless-``agy`` backend; it has no reasoning knob, so it takes no level."""

    name = _IDENTITY.name

    def __init__(
        self, model: str = DEFAULT_MODEL, timeout: str = DEFAULT_TIMEOUT
    ) -> None:
        self.model = resolve_model(model)
        self.timeout = timeout

    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The ``agy --print`` argv; ``cwd`` is REQUIRED and a schema is ignored."""
        # agy ignores its process cwd: `--add-dir` is the only thing rooting it.
        del (
            output_schema_path
        )  # agy has no native schema flag; schema rides the prompt.
        if cwd is None:
            raise ValueError(
                "antigravity (agy) build_command requires cwd (the Tree path): agy "
                "ignores its process cwd and roots only via `--add-dir <Tree>`; without "
                "it the Run's writes land in agy's scratch dir, not the Tree."
            )
        # Not agy's native custom-agent def: it made the model explore, not answer.
        prompt = role_prompt(task, role)
        _enforce_print_arg_limit(prompt)
        permission = [] if read_only else ["--dangerously-skip-permissions"]
        return [
            "agy",
            "--new-project",
            "--add-dir",
            str(cwd),
            f"--model={self.model}",
            f"--print-timeout={self.timeout}",
            *permission,
            "--print",
            prompt,
        ]

    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The parent's environment with :data:`SCRUBBED_AUTH_ENV` removed."""
        source = os.environ if parent_env is None else parent_env
        return {
            key: value for key, value in source.items() if key not in SCRUBBED_AUTH_ENV
        }
