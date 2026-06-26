"""agy — the Antigravity (agy) CLI review backend.

Invokes ``agy --model=<m> --print "Please read the file <tempfile> and follow
its instructions exactly. Output only the requested JSON."`` The full review
prompt is written to a temp file and agy is pointed at it; agy has no native
schema enforcement, so the prompt is built upstream with ``schema_inline=True``
(the expected JSON shape is described in-prose inside it).

``MODEL_ALIASES`` maps the legacy review aliases to agy's verbatim model names
(``agy models``). The default ``pro`` MUST resolve to a capable, non-agentic
model: agy silently resolves a bare ``pro`` to Gemini 3.5 Flash, which in
``--print`` mode goes agentic (runs shell/build commands instead of reviewing
the diff) and never returns JSON — so ``pro`` is pinned to ``Gemini 3.1 Pro
(High)`` here. Spaces/parens in the resolved name are safe: the invocation is a
plain argv list (the shared ``proc`` helper never uses a shell), so no quoting
is needed.
"""

from __future__ import annotations

import os
import tempfile

from ... import proc
from .base import Backend, parse_review_output

MODEL_ALIASES = {
    "pro": "Gemini 3.1 Pro (High)",
    "flash": "Gemini 3.5 Flash (High)",
    "flash_lite": "Gemini 3.5 Flash (Low)",
}


def resolve_model(model: str) -> str:
    """Map a legacy review alias to its agy model name (pass-through otherwise)."""
    return MODEL_ALIASES.get(model, model)


def _print_instruction(prompt_path: str) -> str:
    return (
        f"Please read the file {prompt_path} and follow its instructions exactly. "
        f"Output only the requested JSON."
    )


class AgyBackend(Backend):
    name = "agy"
    binary = "agy"

    def __init__(self, model: str = "pro", timeout: str = "600s") -> None:
        self.model = resolve_model(model)
        # agy's `--print` timeout defaults to 5m; a large review can exceed that
        # and return a TRUNCATED JSON + "timed out waiting for response" (the live
        # agy failure). The default here (600s = 10m) gives big diffs headroom; a
        # consumer with consistently large diffs raises it via the per-reviewer
        # `timeout` option in `.shipit.toml` (normalized to a `<N>s` string).
        self.timeout = timeout

    def _argv(self, prompt_path: str) -> list[str]:
        return [
            "agy",
            f"--model={self.model}",
            f"--print-timeout={self.timeout}",
            "--print",
            _print_instruction(prompt_path),
        ]

    def build_command(self, prompt: str, schema: dict) -> dict:
        placeholder = "<prompt-tempfile>.md"
        return {
            "argv": self._argv(placeholder),
            "stdin": None,
            "files": {placeholder: prompt},
        }

    def run(self, prompt: str, schema: dict, *, cwd: str | None = None) -> dict:
        prompt_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".md",
                prefix=".review_prompt_",
                delete=False,
            ) as prompt_file:
                prompt_file.write(prompt)
                prompt_path = prompt_file.name

            result = proc.run(self._argv(prompt_path), cwd=cwd)
            return parse_review_output(result.stdout, backend_name=self.name)
        finally:
            if prompt_path and os.path.exists(prompt_path):
                os.remove(prompt_path)
