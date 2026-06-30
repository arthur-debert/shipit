"""Tests for `shipit.review.prompt` — the Tree-fetch reviewer task body.

Since TRE05-WS04b the producer no longer front-loads the diff: the task tells the
agent to fetch the scoped diff itself with `gh pr diff <n>` and to emit JSON WITHOUT
posting. The only backend-conditional part is the schema presentation — codex enforces
the JSON shape natively (`--output-schema`) so its task omits the schema; agy has no
native enforcement, so the schema is described in-prose AND followed by the #76
JSON-validity instruction.
"""

from __future__ import annotations

from shipit.review.prompt import build_reviewer_task

_INSTRUCTIONS = "Be thorough."


def test_task_tells_agent_to_fetch_the_diff_itself_and_not_post():
    """The task never embeds a diff: it directs the agent to `gh pr diff <n>` (with the
    PR's real base, not an assumed `main`) and to emit JSON without self-posting."""
    task = build_reviewer_task(_INSTRUCTIONS, 42, schema_inline=False)
    assert "gh pr diff 42" in task
    assert "do not assume the base is" in task.lower()
    assert "do not run" in task.lower() and "gh pr review" in task
    # The custom instructions ride the task.
    assert _INSTRUCTIONS in task


def test_agy_task_includes_schema_and_json_validity_instruction():
    """The agy path (`schema_inline=True`) embeds the in-prose schema AND the #76
    JSON-validity hardening telling the agent its ENTIRE response must be valid JSON."""
    task = build_reviewer_task(_INSTRUCTIONS, 7, schema_inline=True)
    assert "JSON Schema:" in task  # the in-prose schema
    assert "ENTIRE response must be a single, complete, valid JSON object" in task
    assert "valid JSON that a strict parser accepts on the first try" in task


def test_codex_task_omits_schema_and_validity_instruction():
    """The codex path (`schema_inline=False`) enforces the shape out of band, so it
    embeds NEITHER the in-prose schema NOR the agy-specific JSON-validity block."""
    task = build_reviewer_task(_INSTRUCTIONS, 7, schema_inline=False)
    assert "JSON Schema:" not in task
    assert "ENTIRE response must be a single, complete, valid JSON object" not in task
