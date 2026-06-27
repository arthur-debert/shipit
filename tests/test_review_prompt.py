"""Tests for `shipit.review.prompt` — the shared review prompt body.

Both backends send the SAME prompt body; the only backend-conditional part is the
schema presentation. codex enforces the JSON shape natively (`--output-schema`) so
its prompt omits the schema; agy has no native enforcement, so the schema is
described in-prose AND (since #76) followed by an emphatic JSON-validity
instruction — agy is unreliable at emitting a single valid JSON object on a large
diff.
"""

from __future__ import annotations

from shipit.review.prompt import build_prompt

_INSTRUCTIONS = "Be thorough."
_DIFF = "diff --git a/x b/x\n+y\n"


def test_agy_prompt_includes_schema_and_json_validity_instruction():
    """The agy path (`schema_inline=True`) embeds the in-prose schema AND the #76
    JSON-validity hardening telling the agent its ENTIRE response must be valid JSON."""
    prompt = build_prompt(_INSTRUCTIONS, _DIFF, schema_inline=True)
    assert "JSON Schema:" in prompt  # the in-prose schema
    assert "ENTIRE response must be a single, complete, valid JSON object" in prompt
    assert "valid JSON that a strict parser accepts on the first try" in prompt


def test_codex_prompt_omits_schema_and_validity_instruction():
    """The codex path (`schema_inline=False`) enforces the shape out of band, so it
    embeds NEITHER the in-prose schema NOR the agy-specific JSON-validity block."""
    prompt = build_prompt(_INSTRUCTIONS, _DIFF, schema_inline=False)
    assert "JSON Schema:" not in prompt
    assert "ENTIRE response must be a single, complete, valid JSON object" not in prompt
