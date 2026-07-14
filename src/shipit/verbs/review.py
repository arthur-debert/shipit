"""`shipit review` — the top-level, backend-agnostic review utilities group.

`review validate` (#826) is the reviewer AGENT's self-check surface: an agent
pipes its own JSON output through it to verify the shape against `REVIEW_SCHEMA` —
the SAME tolerant parse the funnel uses, plus the severity-enum check — BEFORE
handing it back, so an unparseable/ill-typed payload dies at the agent instead of
burning the round. It lives at the TOP LEVEL, NOT under `pr` (the #826 design): a
schema check touches no PR state and is agent-facing, so nesting it under the
PR-flow group would misfile it. The PR-scoped reviewer acts (`request`, `replay`)
stay under `pr review`; only this agent-facing validator is top-level.

This module owns only the thin CLI: read FILE (or stdin) -> tolerant parse ->
schema check -> print. Errors route through the one
:func:`~.._errors.cli_errors` shell as a uniform ``error: …`` stderr line + exit 1.
"""

from __future__ import annotations

import sys

import click

from ._errors import cli_errors


@click.group(
    name="review",
    help=(
        "Review utilities.\n\n"
        "`validate` checks a review JSON payload against the review schema so a "
        "reviewer agent can self-verify its output before handing it back — no PR "
        "is touched."
    ),
)
def review() -> None:
    """Root of the top-level ``review`` group; verbs attach below."""


@review.command(name="validate")
@click.argument("path", metavar="FILE", required=False)
def validate_cmd(path: str | None) -> None:
    """Validate a review JSON payload against the review schema — for agent self-check.

    FILE is a path to the JSON; omitted or `-` reads stdin, so a reviewer agent
    can pipe its own output straight in (`… | shipit review validate`) before
    handing it back. The payload is parsed the SAME tolerant way the funnel parses
    an agent's stdout (fences and wrapper prose are stripped), then checked against
    `REVIEW_SCHEMA`: the `{summary, comments}` envelope, every field's type, and —
    the check this exists for (#826) — each finding's `severity` must be one of
    `critical | major | minor | nit`. Prints `valid` and exits 0 when it conforms;
    prints every problem (one per line, JSON-path prefixed) and exits 1 otherwise;
    exits 1 with a clean error when the input is not parseable JSON at all.
    """
    raise SystemExit(run_validate(path))


@cli_errors
def run_validate(path: str | None) -> int:
    """Read FILE (or stdin) -> tolerant parse -> schema check -> print. Exit code.

    0 when the payload parses AND conforms to `REVIEW_SCHEMA`; 1 when it has any
    schema problem or nothing JSON-shaped can be extracted at all. Reads stdin when
    ``path`` is ``None`` or ``"-"`` so the reviewer agent's stdout pipes in
    directly. An unreadable FILE raises into the :func:`~.._errors.cli_errors`
    shell as one clean ``error: …`` line.

    Parse strategy (#826): try the funnel's own review-shaped selection FIRST
    (``want=is_review_shaped``) so the real ``{summary, comments}`` object is
    picked out of noisy stdout. When NO review-shaped object is found, fall back
    to extracting ANY JSON object and run the schema check on THAT — because a
    syntactically valid but OFF-SHAPE payload (the #825 ``{"findings": …}``,
    ``{}``, ``{"summary": {}, "comments": {}}``) is exactly what this command
    exists to diagnose, and the actionable path-anchored problems (missing
    ``summary``/``comments``, unexpected key ``findings``, …) come from
    ``validate_review``, not a generic "no JSON" bounce. Only when NOTHING parses
    as a JSON object at all (truncation / prose / fences with no object) is the
    clean no-JSON failure reported.
    """
    from ..review.diff import ReviewError
    from ..review.schema import extract_json, is_review_shaped, validate_review

    if path is None or path == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise ReviewError(f"cannot read review JSON {path!r}: {exc}") from exc

    try:
        # Prefer the review-shaped object among noisy stdout — the funnel's own
        # selection, so a real {summary, comments} review wins over stray blobs.
        payload = extract_json(raw, want=is_review_shaped)
    except ValueError:
        try:
            # No review-shaped object, but a valid OFF-SHAPE object still gets the
            # schema check — that is the failure this command exists to diagnose.
            payload = extract_json(raw)
        except ValueError:
            # Nothing parses as a JSON object at all — the funnel would fail to
            # parse this too. Report it as a validation failure (exit 1), not a
            # crash, so the agent learns its output never even reached the schema.
            print(
                "invalid: no JSON object could be extracted (expected a single "
                "{summary, comments} review object) — check for truncation, prose, "
                "or markdown fences around the JSON",
                file=sys.stderr,
            )
            return 1

    problems = validate_review(payload)
    if not problems:
        print("valid")
        return 0
    print(f"invalid: {len(problems)} schema problem(s):", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1
