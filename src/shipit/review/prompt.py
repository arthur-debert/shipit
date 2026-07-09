"""prompt — the shared review-task bodies the review producers launch with.

`build_reviewer_task` composes the PR (Tree-fetch) task — full-scope, or narrowed
to ONE **Dimension pass** via ``dimension=`` (RVW02-WS04: the fan-out launches it
once per configured dimension; the focus section scopes the SEARCH, never the
severity ladder); `build_range_reviewer_task`
composes its offline commit-range sibling (RVW02-WS03 replay: the diff comes from
`git diff <base>..<head>`, no PR, nothing posted). This module is the ONE place a
review (finder) task is composed — the Calibrator's JUDGE task is a different
contract and lives with its boundary (:mod:`shipit.review.calibrator`). Since
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

from .dimensions import Dimension

# Human-readable description of the expected JSON, embedded for backends without
# native schema enforcement (agy). Kept in sync with schema.REVIEW_SCHEMA.
_SCHEMA_PROSE = """\
JSON Schema:
{
  "summary": {
    "status": "APPROVED" | "REQUEST_CHANGES" | "COMMENT",
    "overall_feedback": "Overall summary of findings and recommendations.",
    "coverage": {
      "reviewed": ["files or file:hunk ranges you actually reviewed"],
      "skipped": [{"file": "path", "reason": "why it was skipped"}]
    }
  },
  "comments": [
    {
      "file": "path/relative/to/repo/root",
      "line": 42,
      "text": "Review comment text",
      "severity": "critical" | "major" | "minor" | "nit",
      "category": "e.g. correctness, cross-file invariants, security, tests",
      "confidence": 0.9,
      "evidence": "the quoted code the finding rests on",
      "fix": "the suggested remedy (may be empty)"
    }
  ]
}

"line" may be null for a file-level finding not tied to a specific line — use \
null rather than inventing a line number to fill the field."""

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
    instructions: str,
    pr_number: int,
    *,
    schema_inline: bool,
    dimension: Dimension | None = None,
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

    ``dimension`` narrows the task to ONE **Dimension pass** (RVW02-WS04,
    ADR-0045): a focus section scopes the SEARCH to that dimension — severity is
    still stated per the shared ladder, but final severity is assigned at
    calibration, and the pass is told overlap/pre-existing findings are fine
    (the Calibrator dedups and routes; prompt-mandated silence would hide the
    routing decision from the record). ``None`` keeps the monolithic full-scope
    task.

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
3. The severity, on the 4-tier ladder: critical, major, minor, or nit. The \
major/minor boundary is the MERGE-BLOCK TEST: would a competent reviewer hold the \
merge for this? critical = merging would be actively harmful (security hole, data \
loss, crash, broken build); major = a concrete correctness or behavioral defect \
worth blocking the merge on; minor = worth doing, not worth holding the merge; \
nit = wording, naming, or style with no correctness, behavioral, or security impact.
4. The category that best describes it (e.g. correctness, cross-file invariants, \
security, tests) and your confidence in the finding from 0.0 to 1.0 — both are \
informational only; nothing routes on them.
5. A descriptive comment explaining the issue and recommending a fix
6. The quoted code the finding rests on (evidence), and the suggested fix

Order the comments array highest severity first: every critical, then every major, \
then minor, then nit.

In the summary, attest your coverage: list what you actually reviewed (files, or \
file:hunk ranges) and anything you skipped with the reason — so silence means \
"clean", not "skipped".

You must output your complete review strictly as a single JSON object on stdout. Do \
NOT wrap the JSON in markdown blocks (e.g. do not use ```json) and do NOT write any \
text before or after the JSON. Do NOT post the review yourself — do not run \
`gh pr review` or otherwise comment on the PR; just emit the JSON and stop. shipit \
captures your output and posts the review."""

    if dimension is not None:
        body = f"{body}\n\n{_dimension_section(dimension)}"
    if schema_inline:
        body = f"{body}\n\n{_SCHEMA_PROSE}\n\n{_JSON_VALIDITY_INSTRUCTION}"

    return body


def _dimension_section(dimension: Dimension) -> str:
    """The focus section that narrows a reviewer task to ONE dimension pass.

    Scopes the SEARCH, not the severity: the pass still states severity on the
    shared ladder (a useful prior), but the Calibrator assigns the final one
    (ADR-0045 — dimensions scope the search; severity is assigned at
    calibration). The pass is explicitly released from budgeting across other
    concerns (that anchoring is what the fan-out removes) and from suppressing
    pre-existing issues (routing them out is the Calibrator's job, recorded —
    not prompt-mandated silence).
    """
    return f"""\
DIMENSION FOCUS — {dimension.title}: this review is ONE scoped pass of a \
parallel fan-out; other passes cover the other dimensions, and a calibration \
stage dedups the union, verifies each finding, and assigns final severity. \
Hunt EXHAUSTIVELY and ONLY for: {dimension.focus}
Report a finding even if it looks pre-existing (it may predate this PR) — a \
later stage routes out-of-scope findings explicitly; do not silently drop \
them. Do not pad with findings outside this dimension's focus."""


def build_range_reviewer_task(
    instructions: str, base_sha: str, head_sha: str, *, schema_inline: bool
) -> str:
    """Compose the COMMIT-RANGE reviewer task — the offline-replay sibling of
    :func:`build_reviewer_task` (RVW02-WS03).

    Same review contract, different scope source: there is NO pull request, so the
    agent gets the diff from git itself — ``git diff <base>..<head>`` over two
    pre-resolved commit shas (the replay boundary resolved and validated them, so
    the task never carries a user-typed rev that could miss) — and is told it is
    OFFLINE: no ``gh`` calls, nothing posted, output captured from stdout exactly
    like the PR path. The checkout it runs in provides the surrounding-code
    context. ``schema_inline`` follows the same backend split as the PR task.
    """
    body = f"""\
You are an expert AI code reviewer. You are running in a checkout of a repository. \
Your task is to perform a detailed, rigorous OFFLINE code review of one commit \
range of this repository — there is NO pull request involved.

FIRST, get the changes: run `git diff {base_sha}..{head_sha}` to read the range's \
unified diff. Read the surrounding code in this checkout for any context you need. \
Do NOT call `gh` — this review is offline and touches nothing on GitHub.

Here are the custom review instructions you must follow:
{instructions}

Identify bugs, code quality issues, style violations, potential crashes, logic \
errors, or missing tests. For each finding, determine:
1. The file path (relative to the repository root)
2. The specific line number (if applicable)
3. The severity, on the 4-tier ladder: critical, major, minor, or nit. The \
major/minor boundary is the MERGE-BLOCK TEST: would a competent reviewer hold the \
merge for this? critical = merging would be actively harmful (security hole, data \
loss, crash, broken build); major = a concrete correctness or behavioral defect \
worth blocking the merge on; minor = worth doing, not worth holding the merge; \
nit = wording, naming, or style with no correctness, behavioral, or security impact.
4. The category that best describes it (e.g. correctness, cross-file invariants, \
security, tests) and your confidence in the finding from 0.0 to 1.0 — both are \
informational only; nothing routes on them.
5. A descriptive comment explaining the issue and recommending a fix
6. The quoted code the finding rests on (evidence), and the suggested fix

Order the comments array highest severity first: every critical, then every major, \
then minor, then nit.

In the summary, attest your coverage: list what you actually reviewed (files, or \
file:hunk ranges) and anything you skipped with the reason — so silence means \
"clean", not "skipped".

You must output your complete review strictly as a single JSON object on stdout. Do \
NOT wrap the JSON in markdown blocks (e.g. do not use ```json) and do NOT write any \
text before or after the JSON. Do NOT post the review anywhere — do not run `gh` or \
otherwise publish it; just emit the JSON and stop. shipit captures your output and \
records it locally."""

    if schema_inline:
        body = f"{body}\n\n{_SCHEMA_PROSE}\n\n{_JSON_VALIDITY_INSTRUCTION}"

    return body
