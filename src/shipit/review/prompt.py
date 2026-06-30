"""prompt — the single shared review-task body for the Tree-fetch funnel producer.

`build_reviewer_task` is the one place the review task is composed. Since
TRE05-WS04b the producer no longer **front-loads** the diff into the prompt
(ADR-0020 §Reviewer-path reconciliation — "REPLACE"): the agent runs in a shared
read-only Tree (ADR-0018) at the PR's true head and **fetches the scoped diff
itself** with ``gh pr diff <n>``, so the body tells it *how to get the diff* and
*what to emit*, never the diff text. This is the load-bearing difference from the
retired front-loaded backends: the agent walks the whole codebase lazily instead
of reviewing a context-free pasted diff.

The agent is told to emit its review as a single JSON object on stdout and to
**NOT** post it — shipit captures that stdout and posts it via the existing
App-identity ``post`` path onto the existing ``review: <agent>-local`` check-run
(the funnel keeps App-identity posting; the agent never runs ``gh pr review``).
The only backend-conditional part is the schema presentation:

  * codex enforces the JSON shape natively via ``--output-schema`` and so does
    NOT embed the schema in the prompt (``schema_inline=False``);
  * agy has no native schema enforcement, so the expected JSON shape is described
    in-prose inside the prompt (``schema_inline=True``).
"""

from __future__ import annotations

# Human-readable description of the expected JSON, embedded for backends without
# native schema enforcement (agy). Kept in sync with schema.REVIEW_SCHEMA.
_SCHEMA_PROSE = """\
JSON Schema:
{
  "summary": {
    "status": "APPROVED" | "REQUEST_CHANGES" | "COMMENT",
    "overall_feedback": "Overall summary of findings and recommendations."
  },
  "comments": [
    {
      "file": "path/relative/to/repo/root",
      "line": 42,
      "text": "Review comment text",
      "severity": "ERROR" | "WARNING" | "INFO",
      "code_snippet": "..."
    }
  ]
}"""

# Appended ONLY for backends without native schema enforcement (agy): an emphatic
# restatement that the ENTIRE response must be one complete, valid JSON object and
# nothing else. agy has no `--output-schema`, so it tends to emit prose, markdown
# fences, or JSON truncated mid-object (the live #76 failure); this reduces — does
# not eliminate — that. codex enforces the shape out of band and never sees this.
_JSON_VALIDITY_INSTRUCTION = """\
CRITICAL OUTPUT REQUIREMENT: Your ENTIRE response must be a single, complete, \
valid JSON object matching the schema above — and NOTHING else. Do not write any \
prose, explanation, or markdown code fences (no ```) before, after, or around the \
JSON. Do not stop early or truncate: every brace and bracket must be closed so the \
output is syntactically valid JSON that a strict parser accepts on the first try. \
If you have many findings, keep each comment concise rather than emitting an \
incomplete object."""


def build_reviewer_task(
    instructions: str, pr_number: int, *, schema_inline: bool
) -> str:
    """Compose the Tree-fetch reviewer task from ``instructions`` and ``pr_number``.

    The body — identical for every backend except the schema presentation — tells
    the agent, running in a shared read-only checkout of the PR head, to:

    1. fetch the PR's scoped diff itself with ``gh pr diff <pr_number>`` (which uses
       the PR's REAL base and head — it must NOT assume the base is ``main``, since a
       work-stream / epic PR targets its umbrella branch), reading the surrounding
       code in the Tree for context;
    2. review it against ``instructions`` and the repo's conventions; and
    3. emit the review as a SINGLE JSON object on stdout and **NOT** post it — shipit
       captures stdout and posts it as the bot through the funnel's check-run gate.

    When ``schema_inline`` is True the expected JSON shape is appended in prose (for
    a backend without native schema enforcement — agy); otherwise it is omitted (codex
    enforces the schema out of band via ``--output-schema``).
    """
    body = f"""\
You are an expert AI code reviewer. You are running in a shared, READ-ONLY checkout \
of a pull request's head commit. Your task is to perform a detailed, rigorous code \
review of that pull request (#{pr_number}).

FIRST, get the changes: run `gh pr diff {pr_number}` to read the pull request's \
unified diff. It uses the PR's ACTUAL base and head — do NOT assume the base is \
`main` (a work-stream or epic PR targets its umbrella branch). Read the surrounding \
code in this checkout for any context you need.

Here are the custom review instructions you must follow:
{instructions}

Identify bugs, code quality issues, style violations, potential crashes, logic \
errors, or missing tests. For each finding, determine:
1. The file path (relative to the repository root)
2. The specific line number (if applicable)
3. The severity (ERROR, WARNING, or INFO)
4. A descriptive comment explaining the issue and recommending a fix
5. A snippet of the relevant code

You must output your complete review strictly as a single JSON object on stdout. Do \
NOT wrap the JSON in markdown blocks (e.g. do not use ```json) and do NOT write any \
text before or after the JSON. Do NOT post the review yourself — do not run \
`gh pr review` or otherwise comment on the PR; just emit the JSON and stop. shipit \
captures your output and posts the review."""

    if schema_inline:
        body = f"{body}\n\n{_SCHEMA_PROSE}\n\n{_JSON_VALIDITY_INSTRUCTION}"

    return body
