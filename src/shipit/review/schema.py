"""schema — the single-repo review JSON schema + a tolerant JSON extractor.

`REVIEW_SCHEMA` is the JSON-schema the codex backend enforces natively
(`--output-schema`) and the agy backend describes in-prose.

Each finding carries the shared 4-tier :class:`~shipit.finding.Severity` ladder
(``critical | major | minor | nit`` — the enum values come FROM the domain
module, never a second copy) plus an informational-only ``category`` and
``confidence``, its quoted ``evidence``, and a suggested ``fix``. The review
summary carries a **coverage attestation** — what was reviewed and what was
skipped with reasons — so silence means "clean," not "skipped." The attestation
is human-facing, not engine data.

`extract_json` is the three-fallback parse: direct, fenced (```json …```), then a
greedy `{...}` regex — agents wrap their JSON output inconsistently, so the parser
is deliberately forgiving.
"""

from __future__ import annotations

import json
import re

from ..finding import Severity

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["APPROVED", "REQUEST_CHANGES", "COMMENT"],
                },
                "overall_feedback": {"type": "string"},
                "coverage": {
                    "type": "object",
                    "properties": {
                        "reviewed": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "skipped": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["file", "reason"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["reviewed", "skipped"],
                    "additionalProperties": False,
                },
            },
            "required": ["status", "overall_feedback", "coverage"],
            "additionalProperties": False,
        },
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    # Nullable: a file-level finding has no specific line, and the
                    # posting path folds a null-line finding in unanchored. `line`
                    # STAYS in `required` — codex's strict `--output-schema` needs
                    # every property required; optionality rides the null type.
                    "line": {"type": ["integer", "null"]},
                    "text": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": [severity.value for severity in Severity],
                    },
                    "category": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "evidence": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": [
                    "file",
                    "line",
                    "text",
                    "severity",
                    "category",
                    "confidence",
                    "evidence",
                    "fix",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "comments"],
    "additionalProperties": False,
}


def extract_json(text: str) -> dict:
    """Parse a JSON object out of an agent's stdout, tolerating wrapping.

    Tries, in order: a direct parse of the stripped text; stripping ```json …```
    code fences; a greedy ``{.*}`` regex. Raises :class:`ValueError` if none
    yield valid JSON.
    """
    text_clean = text.strip()
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text_clean, flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\})", text_clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse valid JSON from output:\n{text}")
