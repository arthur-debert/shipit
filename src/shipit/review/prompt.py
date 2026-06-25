"""prompt — the single shared review prompt body.

`build_prompt` is the one place the review prompt is composed. Both backends
send the SAME body so dry-run output (and the semantic payload the agent sees)
is comparable regardless of backend; the only backend-conditional part is the
schema presentation:

  * codex enforces the JSON shape natively via ``--output-schema`` and so does
    NOT embed the schema in the prompt (``schema_inline=False``);
  * agy has no native schema enforcement, so the expected JSON shape is
    described in-prose inside the prompt (``schema_inline=True``).

The diff is a plain string argument — single-repo, no git/PR logic. A later
phase computes it from a PR.
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


def build_prompt(instructions: str, diff: str, *, schema_inline: bool) -> str:
    """Compose the shared review prompt from ``instructions`` and a unified ``diff``.

    The body is identical for every backend. When ``schema_inline`` is True a
    human-readable description of the expected JSON shape is appended (for
    backends without native schema enforcement); otherwise it is omitted (the
    backend enforces the schema out of band).
    """
    body = f"""\
You are an expert AI code reviewer. Your task is to perform a detailed, rigorous \
code review of the changes in the following pull request.

The complete set of changes is provided below as a unified diff.

Here are the custom review instructions you must follow:
{instructions}

Identify bugs, code quality issues, style violations, potential crashes, logic \
errors, or missing tests. For each finding, determine:
1. The file path (relative to the repository root)
2. The specific line number (if applicable)
3. The severity (ERROR, WARNING, or INFO)
4. A descriptive comment explaining the issue and recommending a fix
5. A snippet of the relevant code

You must output your complete review strictly as a single JSON object. Do not \
wrap the JSON in markdown blocks (e.g. do not use ```json) and do not write any \
text before or after the JSON.

Here is the unified diff to review:
{diff}"""

    if schema_inline:
        body = f"{body}\n\n{_SCHEMA_PROSE}"

    return body
