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
from shipit.review.schema import REVIEW_SCHEMA

_INSTRUCTIONS = "Be thorough."


def test_schema_line_is_nullable_but_stays_required():
    """A file-level finding has no line, so `line` accepts null — but it STAYS in
    `required` because codex's strict `--output-schema` needs every property
    required (optionality rides the null type, not omission)."""
    item = REVIEW_SCHEMA["properties"]["comments"]["items"]
    assert item["properties"]["line"]["type"] == ["integer", "null"]
    assert "line" in item["required"]


def test_agy_prose_notes_line_may_be_null():
    """The in-prose schema (agy's only guide) tells the agent a file-level finding
    uses null rather than a fabricated line number."""
    task = build_reviewer_task(_INSTRUCTIONS, 7, schema_inline=True)
    assert "null for a file-level finding" in task


def test_task_tells_agent_to_fetch_the_diff_itself_and_not_post():
    """The task never embeds a diff: it directs the agent to `gh pr diff <n>` (with the
    PR's real base, not an assumed `main`) and to emit JSON without self-posting."""
    task = build_reviewer_task(_INSTRUCTIONS, 42, schema_inline=False)
    assert "gh pr diff 42" in task
    assert "do not assume the base is" in task.lower()
    assert "do not run" in task.lower() and "gh pr review" in task
    # The custom instructions ride the task.
    assert _INSTRUCTIONS in task


def test_task_instructs_the_severity_ladder_and_merge_block_boundary():
    """Every reviewer task (both backends) carries the 4-tier ladder, the
    merge-block test as the major/minor boundary, severity-first ordering, the
    informational-only status of category/confidence, and the coverage
    attestation. The retired ERROR/WARNING/INFO triple is gone."""
    for schema_inline in (False, True):
        task = build_reviewer_task(_INSTRUCTIONS, 42, schema_inline=schema_inline)
        assert "critical, major, minor, or nit" in task
        assert "MERGE-BLOCK TEST" in task
        assert "would a competent reviewer hold the merge" in task
        assert "highest severity first" in task
        assert "informational only" in task
        assert "attest your coverage" in task
        assert "ERROR" not in task and "WARNING" not in task


def test_agy_task_includes_schema_and_json_validity_instruction():
    """The agy path (`schema_inline=True`) embeds the in-prose schema AND the #76
    JSON-validity hardening telling the agent its ENTIRE response must be valid JSON."""
    task = build_reviewer_task(_INSTRUCTIONS, 7, schema_inline=True)
    assert "JSON Schema:" in task  # the in-prose schema
    assert "ENTIRE response must be a single, complete, valid JSON object" in task
    assert "valid JSON that a strict parser accepts on the first try" in task
    # The prose schema mirrors REVIEW_SCHEMA's new shape: the 4-tier severity
    # enum, informational category/confidence, evidence/fix, coverage attestation.
    assert '"critical" | "major" | "minor" | "nit"' in task
    assert '"category"' in task and '"confidence"' in task
    assert '"evidence"' in task and '"fix"' in task
    assert '"coverage"' in task


def test_codex_task_omits_schema_and_validity_instruction():
    """The codex path (`schema_inline=False`) enforces the shape out of band, so it
    embeds NEITHER the in-prose schema NOR the agy-specific JSON-validity block."""
    task = build_reviewer_task(_INSTRUCTIONS, 7, schema_inline=False)
    assert "JSON Schema:" not in task
    assert "ENTIRE response must be a single, complete, valid JSON object" not in task
