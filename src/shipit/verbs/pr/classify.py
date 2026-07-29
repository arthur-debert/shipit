"""`shipit pr classify` — the write-once Severity-override verb.

See docs/adr/0044-findings-arrive-classified.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from ...finding import Severity
from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.breakers import build_rounds
from ...prstate.errors import PrStateError
from ...prstate.fetch import gather
from ...prstate.overrides import record_override
from ...prstate.reviewers_config import load_roster
from ...prstate.severity import resolve_finding_severity
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import json_option, pr_number_argument
from .._render import emit

EXCERPT_CHARS = 100

SEVERITY_CHOICES = tuple(s.value for s in Severity)


@dataclass(frozen=True)
class FindingLine:
    """One finding as the list view carries it: identity plus resolved severity."""

    comment_id: int
    severity: str
    source: str
    path: str
    line: int | None
    author: str
    excerpt: str

    def to_dict(self) -> dict:
        return {
            "comment_id": self.comment_id,
            "severity": self.severity,
            "source": self.source,
            "path": self.path,
            "line": self.line,
            "author": self.author,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class ClassifyList:
    """The list mode's typed result: the latest round's resolved severities."""

    pr: int
    round: int | None
    findings: tuple[FindingLine, ...]

    def to_dict(self) -> dict:
        return {
            "pr": self.pr,
            "round": self.round,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class OverrideRecorded:
    """The record mode's typed result: the override written."""

    pr: int
    comment_id: int
    severity: str
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "pr": self.pr,
            "comment_id": self.comment_id,
            "severity": self.severity,
            "reason": self.reason,
        }


def _excerpt(body: str) -> str:
    """The finding body as ONE scannable line, clipped to :data:`EXCERPT_CHARS`."""
    line = " ".join(body.split())
    return line if len(line) <= EXCERPT_CHARS else line[: EXCERPT_CHARS - 1] + "…"


def format_list(result: ClassifyList) -> str:
    """One line per finding, resolved severity first."""
    if result.round is None:
        return f"PR #{result.pr}: no review round yet — no findings to list"
    if not result.findings:
        return (
            f"PR #{result.pr}: the latest round (round {result.round}) has no findings"
        )
    lines = [
        f"PR #{result.pr} — round {result.round}: "
        f"{len(result.findings)} finding(s), severity-resolved"
    ]
    for f in result.findings:
        where = f"{f.path}:{f.line}" if f.line is not None else f.path
        lines.append(
            f"  {f.comment_id}  {f.severity} ({f.source})  {where}  "
            f"({f.author})  {f.excerpt}"
        )
    lines.append(
        f"override one (write-once): `shipit pr classify {result.pr} "
        f'--comment <id> {{{"|".join(SEVERITY_CHOICES)}}} [--reason "…"]`'
    )
    return "\n".join(lines)


def format_recorded(result: OverrideRecorded) -> str:
    line = (
        f"severity of finding {result.comment_id} on PR #{result.pr} "
        f"overridden to {result.severity}"
    )
    if result.reason:
        line += f" ({result.reason})"
    return line


@click.command(name="classify")
@pr_number_argument
@click.option(
    "--comment",
    "comment",
    default=None,
    metavar="<id> {critical|major|minor|nit}",
    type=(click.IntRange(min=1), click.Choice(SEVERITY_CHOICES)),
    help=(
        "Record a write-once Severity override for finding comment <id> — it "
        "beats the finding's marker/adapter/default severity. Re-overriding "
        "is an error."
    ),
)
@click.option(
    "--reason",
    default=None,
    help="Optional one-line reason recorded with the override.",
)
@json_option
def cmd(
    pr: int | None,
    comment: tuple[int, str] | None,
    reason: str | None,
    as_json: bool,
) -> None:
    """List the latest round's resolved severities, or override ONE finding's."""
    raise SystemExit(run(pr, comment=comment, reason=reason, as_json=as_json))


@cli_errors
def run(
    pr: int | None = None,
    *,
    comment: tuple[int, str] | None = None,
    reason: str | None = None,
    as_json: bool = False,
    repo: Repo | None = None,
) -> int:
    """List the round's severities, or record ONE override; returns an exit code."""
    if comment is None and reason is not None:
        raise PrStateError("--reason records with an override — pass --comment too")
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError(
            "no PR for this branch — nothing to override (pass a PR number, "
            "or run from the PR's branch)"
        )
    ctx = gather(target, load_roster())
    rounds = build_rounds(ctx)
    latest = rounds[-1] if rounds else None

    if comment is None:
        lines: list[FindingLine] = []
        if latest is not None:
            for f in latest.findings:
                resolution = resolve_finding_severity(f, ctx.overrides)
                lines.append(
                    FindingLine(
                        comment_id=f.comment_id,
                        severity=resolution.severity.value,
                        source=resolution.source,
                        path=f.path,
                        line=f.line,
                        author=f.author,
                        excerpt=_excerpt(f.body),
                    )
                )
        result = ClassifyList(
            pr=target.number,
            round=latest.index if latest is not None else None,
            findings=tuple(lines),
        )
        emit(result, format_list, as_json=as_json)
        return 0

    comment_id, severity_value = comment
    if latest is None or comment_id not in {f.comment_id for f in latest.findings}:
        raise PrStateError(
            f"comment {comment_id} is not a finding of the latest review round "
            f"on PR #{target.number} — list the round's findings with "
            "`shipit pr classify`"
        )
    severity = Severity(severity_value)
    record_override(target.repo, target.number, comment_id, severity, reason=reason)
    emit(
        OverrideRecorded(
            pr=target.number,
            comment_id=comment_id,
            severity=severity.value,
            reason=reason,
        ),
        format_recorded,
        as_json=as_json,
    )
    return 0
