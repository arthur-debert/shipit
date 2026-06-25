"""codex — the Codex CLI review backend.

Invokes ``codex exec --skip-git-repo-check --ephemeral --sandbox read-only
--model <id> --output-schema <schemafile> -`` with the prompt on STDIN. Codex
enforces the JSON shape natively via ``--output-schema``, so the prompt is built
with ``schema_inline=False`` upstream; here we just write the schema to a temp
file and point ``--output-schema`` at it.

``MODEL_ALIASES`` maps the legacy review aliases to Codex model ids.
"""

from __future__ import annotations

import json
import os
import tempfile

from ... import proc
from .base import Backend, parse_review_output

MODEL_ALIASES = {
    "pro": "gpt-5.5",
    "flash": "gpt-5.4-mini",
    "flash_lite": "gpt-5.4-mini",
}


def resolve_model(model: str) -> str:
    """Map a legacy review alias to its Codex model id (pass-through otherwise)."""
    return MODEL_ALIASES.get(model, model)


class CodexBackend(Backend):
    name = "codex"
    binary = "codex"

    def __init__(self, model: str = "pro") -> None:
        self.model = resolve_model(model)

    def _argv(self, schema_path: str) -> list[str]:
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--model",
            self.model,
            "--output-schema",
            schema_path,
            "-",
        ]

    def build_command(self, prompt: str, schema: dict) -> dict:
        placeholder = "<schema-tempfile>.json"
        return {
            "argv": self._argv(placeholder),
            "stdin": prompt,
            "files": {placeholder: json.dumps(schema, indent=2)},
        }

    def run(self, prompt: str, schema: dict, *, cwd: str | None = None) -> dict:
        schema_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                prefix=".review_schema_",
                delete=False,
            ) as schema_file:
                json.dump(schema, schema_file)
                schema_path = schema_file.name

            result = proc.run(self._argv(schema_path), input=prompt, cwd=cwd)
            return parse_review_output(result.stdout, backend_name=self.name)
        finally:
            if schema_path and os.path.exists(schema_path):
                os.remove(schema_path)
