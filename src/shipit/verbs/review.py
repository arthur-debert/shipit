"""`shipit review` — the top-level, backend-agnostic review utilities group."""

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
    """Validate a review JSON payload against the review schema — for agent self-check."""
    raise SystemExit(run_validate(path))


@cli_errors
def run_validate(path: str | None) -> int:
    """Schema-check FILE (or stdin) and print the verdict; returns an exit code."""
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
        payload = extract_json(raw, want=is_review_shaped)
    except ValueError:
        try:
            payload = extract_json(raw)
        except ValueError:
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
