"""Tests for `shipit.review.prompt` — the Tree-fetch reviewer task body.

Since TRE05-WS04b the producer no longer front-loads the diff: the task tells the
agent to fetch the scoped diff itself with `gh pr diff <n>` and to emit JSON WITHOUT
posting. The only backend-conditional part is the schema presentation — codex enforces
the JSON shape natively (`--output-schema`) so its task omits the schema; agy has no
native enforcement, so the schema is described in-prose AND followed by the #76
JSON-validity instruction.

Since RVW03-WS05 (ADR-0050) every arm — full, dimension, incremental, range — carries
ONE shared scope/context baseline: report only on the diff; read anything; run
nothing. The tests here pin that parity and the absence of the retired
"solely on the provided diff" contradiction.
"""

from __future__ import annotations

from shipit.review.instructions import default_instructions
from shipit.review.prompt import (
    build_incremental_reviewer_task,
    build_range_reviewer_task,
    build_reviewer_task,
)
from shipit.review.schema import REVIEW_SCHEMA

_INSTRUCTIONS = "Be thorough."


def _every_arm_task():
    """One task per reviewer arm: full, dimension pass, incremental, range."""
    from shipit.review.dimensions import by_name

    return {
        "full": build_reviewer_task(_INSTRUCTIONS, 42, schema_inline=False),
        "dimension": build_reviewer_task(
            _INSTRUCTIONS, 42, schema_inline=False, dimension=by_name("correctness")
        ),
        "incremental": build_incremental_reviewer_task(
            _INSTRUCTIONS, 42, "b" * 40, "c" * 40, schema_inline=False
        ),
        "range": build_range_reviewer_task(
            _INSTRUCTIONS, "b" * 40, "c" * 40, schema_inline=False
        ),
    }


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


def test_dimension_scoped_task_carries_the_focus_section():
    # RVW02-WS04: a Dimension pass's task is the SAME reviewer contract plus a
    # focus section that scopes the SEARCH — severity stays on the shared
    # ladder, and (ADR-0050) the scope rule rides the shared baseline, not the
    # focus section.
    from shipit.review.dimensions import by_name

    task = build_reviewer_task(
        "INSTR", 7, schema_inline=False, dimension=by_name("correctness")
    )
    assert "DIMENSION FOCUS — Correctness" in task
    assert "logic errors" in task
    assert "pre-existing" in task
    # The base contract is untouched: fetch the diff, emit JSON, never post.
    assert "gh pr diff 7" in task
    assert "Do NOT post the review yourself" in task


# --- the shared scope/context baseline (ADR-0050, RVW03-WS05) ----------------


def test_every_arm_carries_the_one_shared_scope_and_context_baseline():
    """The parity prerequisite (ADR-0050): full, dimension, incremental, and
    range tasks all carry the SAME canonical baseline — report only findings
    the diff INTRODUCED or EXPOSED (pre-existing routes out-of-scope, never
    posted), read the checkout for context, execute nothing."""
    for arm, task in _every_arm_task().items():
        assert "SCOPE AND CONTEXT" in task, arm
        assert "INTRODUCED or EXPOSED" in task, arm
        assert "pre-existing" in task and "must NOT be posted" in task, arm
        # Context is the checkout: reading beyond the diff is encouraged...
        assert "reading BEYOND the diff is encouraged" in task, arm
        assert "callers, definitions, usages, and neighboring code" in task, arm
        # ...while executing build/test remains forbidden.
        assert "do NOT execute build, test, or shell commands" in task, arm


def test_dimension_section_carries_no_private_scope_rule():
    """The scope rule reaches a dimension pass through the shared baseline
    ONCE — the focus section no longer restates it as a private rule (the old
    text the other arms lacked). Dimension-specific text (severity posting,
    no padding) stays."""
    from shipit.review.dimensions import by_name

    task = build_reviewer_task(
        "INSTR", 7, schema_inline=False, dimension=by_name("correctness")
    )
    assert task.count("INTRODUCED or EXPOSED") == 1
    focus = task[task.index("DIMENSION FOCUS") :]
    assert "INTRODUCED or EXPOSED" not in focus
    assert "Your stated severity is the posted severity" in focus
    assert "Do not pad with findings" in focus


def test_the_diff_only_vs_walk_checkout_contradiction_is_gone():
    """The bundled instructions no longer say "solely on the provided diff" /
    "one-shot review" — they carry the ADR-0050 split (scope = diff, context =
    checkout) — and no arm's composed task reintroduces the old text."""
    bundled = default_instructions()
    assert "solely on the provided diff" not in bundled
    assert "one-shot review" not in bundled
    assert "Scope is the diff; context is the checkout" in bundled
    assert "introduced or exposed" in bundled
    for arm, task in _every_arm_task().items():
        assert "solely on the provided diff" not in task, arm
    # The bundled default composes cleanly with every arm and stays
    # contradiction-free end to end.
    composed = build_reviewer_task(bundled, 42, schema_inline=False)
    assert "solely on the provided diff" not in composed
    assert "SCOPE AND CONTEXT" in composed


def test_dimension_section_precedes_the_inline_schema_for_agy():
    from shipit.review.dimensions import by_name

    task = build_reviewer_task(
        "INSTR", 7, schema_inline=True, dimension=by_name("test-quality")
    )
    assert task.index("DIMENSION FOCUS") < task.index("JSON Schema:")


def test_monolithic_task_carries_no_dimension_section():
    task = build_reviewer_task("INSTR", 7, schema_inline=False)
    assert "DIMENSION FOCUS" not in task


# --- incremental (round >= 2) reviewer task (RVW02-WS06, ADR-0045) ----------


def test_incremental_task_diffs_the_fix_range_not_the_full_pr():
    task = build_incremental_reviewer_task(
        _INSTRUCTIONS, 42, "b" * 40, "c" * 40, schema_inline=False
    )
    # It diffs the FIX RANGE via git, and explicitly forbids the full `gh pr diff`.
    assert f"git diff {'b' * 40}..{'c' * 40}" in task
    assert "Do NOT" in task and "gh pr diff" in task
    assert _INSTRUCTIONS in task


def test_incremental_task_mandates_dependency_neighborhood_context():
    # The load-bearing anti-regression rule: read callers/definitions/usages
    # beyond the diff, so a local fix breaking a distant invariant is still caught.
    task = build_incremental_reviewer_task(
        _INSTRUCTIONS, 42, "b" * 40, "c" * 40, schema_inline=True
    )
    assert "MANDATORY CONTEXT EXPANSION" in task
    assert "DEPENDENCY NEIGHBORHOOD" in task
    # It keeps the same ladder + JSON contract as the full task, and the agy prose.
    assert "critical, major, minor, or nit" in task
    assert "null for a file-level finding" in task
