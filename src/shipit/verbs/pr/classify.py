"""`shipit pr classify` — record the shepherd's verdict on each review finding (#423).

The write half of the classification seam. The agent addressing a review round
has ALREADY judged every finding's weight by deciding fix-vs-reply; this verb
records that judgment — `nitpick` or `substantive`, keyed by the finding
comment's id, written ONCE into the dev-cycle event log
(:mod:`shipit.prstate.verdicts`) — so the state machine can consume it: the
all-nitpick breaker reads verdicts only, and `pr next`/`pr status` gate on an
unclassified latest round. There is no auto-classification anywhere; this verb
is the only way a verdict exists.

Two modes, ADR-0030 glue + renderers around the engine's round vocabulary:

- **list** (no ``--comment``): the LATEST round's unclassified findings —
  comment id, location, author, body excerpt — plus the literal record command.
- **record** (``--comment <id> nitpick|substantive [--reason "…"]``): validate
  the id IS a finding of the latest round, then write the verdict (write-once —
  re-classifying errors), reporting how many findings remain unclassified.

The PR target is the shared primitive (explicit number or the current branch's
PR); runtime failures map through the one :func:`~.._errors.cli_errors` shell.
Unlike `pr status`, a branch with NO PR is an error here — there is nothing to
classify.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.breakers import build_rounds, unclassified_findings
from ...prstate.errors import PrStateError
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.verdicts import VERDICTS, record_verdict
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import json_option, pr_number_argument
from .._render import emit

#: How much of a finding's body the list view shows — one scannable line per
#: finding; the full text lives on the PR thread itself.
EXCERPT_CHARS = 100


@dataclass(frozen=True)
class FindingLine:
    """One unclassified finding as the list view carries it."""

    comment_id: int
    path: str
    line: int | None
    author: str
    excerpt: str

    def to_dict(self) -> dict:
        return {
            "comment_id": self.comment_id,
            "path": self.path,
            "line": self.line,
            "author": self.author,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class ClassifyList:
    """The list mode's typed result: the latest round's classification state."""

    pr: int
    round: int | None  # latest round index; None when no round exists yet
    total: int  # findings in the latest round
    unclassified: tuple[FindingLine, ...]

    def to_dict(self) -> dict:
        return {
            "pr": self.pr,
            "round": self.round,
            "total": self.total,
            "unclassified": [f.to_dict() for f in self.unclassified],
        }


@dataclass(frozen=True)
class ClassifyRecorded:
    """The record mode's typed result: the verdict written + what remains."""

    pr: int
    comment_id: int
    verdict: str
    reason: str | None
    remaining: int  # unclassified findings left in the latest round

    def to_dict(self) -> dict:
        return {
            "pr": self.pr,
            "comment_id": self.comment_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "remaining": self.remaining,
        }


def _excerpt(body: str) -> str:
    """The finding body as ONE scannable line, clipped to :data:`EXCERPT_CHARS`."""
    line = " ".join(body.split())
    return line if len(line) <= EXCERPT_CHARS else line[: EXCERPT_CHARS - 1] + "…"


def format_list(result: ClassifyList) -> str:
    """The pure text renderer for list mode: one line per unclassified finding,
    then the literal record command — the agent never reconstructs the verb."""
    if result.round is None:
        return f"PR #{result.pr}: no review round yet — nothing to classify"
    if not result.total:
        return (
            f"PR #{result.pr}: the latest round (round {result.round}) has no "
            "findings — nothing to classify"
        )
    if not result.unclassified:
        return (
            f"PR #{result.pr}: all {result.total} finding(s) of round "
            f"{result.round} are classified"
        )
    lines = [
        f"PR #{result.pr} — round {result.round}: "
        f"{len(result.unclassified)} unclassified finding(s) of {result.total}"
    ]
    for f in result.unclassified:
        where = f"{f.path}:{f.line}" if f.line is not None else f.path
        lines.append(f"  {f.comment_id}  {where}  ({f.author})  {f.excerpt}")
    lines.append(
        f"record each: `shipit pr classify {result.pr} --comment <id> "
        'nitpick|substantive [--reason "…"]`'
    )
    return "\n".join(lines)


def format_recorded(result: ClassifyRecorded) -> str:
    """The pure text renderer for record mode: the verdict + what remains."""
    line = (
        f"classified finding {result.comment_id} as {result.verdict} on PR #{result.pr}"
    )
    if result.reason:
        line += f" ({result.reason})"
    if result.remaining:
        line += f" — {result.remaining} unclassified finding(s) remaining"
    else:
        line += " — the latest round is fully classified"
    return line


@click.command(name="classify")
@pr_number_argument
@click.option(
    "--comment",
    "comment",
    default=None,
    metavar="<id> {nitpick|substantive}",
    type=(click.IntRange(min=1), click.Choice(VERDICTS)),
    help=(
        "Record the verdict for finding comment <id>: nitpick (cosmetic — "
        "nothing that changes correctness or behaviour) or substantive. "
        "Written once; re-classifying is an error."
    ),
)
@click.option(
    "--reason",
    default=None,
    help="Optional one-line reason recorded with the verdict.",
)
@json_option
def cmd(
    pr: int | None,
    comment: tuple[int, str] | None,
    reason: str | None,
    as_json: bool,
) -> None:
    """List or record verdicts for the latest review round's findings.

    With no flags: list the latest round's UNCLASSIFIED findings (comment id +
    body excerpt). With --comment <id> nitpick|substantive: record that verdict
    into the dev-cycle event log — once per finding, immutable. PR is the
    number; omitted, it resolves the current branch's PR.
    """
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
    """Resolve → gather → list the unclassified findings, or record ONE verdict.

    ``repo`` is the identity half of the PR target: omitted (the CLI path), the
    root context's ambient repo — resolved once per invocation (ADR-0030); a
    direct caller (a test) injects it as a value.

    Returns 0 on a rendered list or a recorded verdict. A branch with no PR is
    an ERROR here (unlike `pr status` — classification needs a PR), as are: a
    ``--reason`` without ``--comment``, a comment id that is not a finding of
    the LATEST round (verdicts key the round the loop turns on — a stale or
    mistyped id must fail loud, not poison the log), and a re-classification
    (write-once, from the store). All map to ``error: …`` + exit 1 via the
    shared shell.
    """
    if comment is None and reason is not None:
        raise PrStateError("--reason records with a verdict — pass --comment too")
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError(
            "no PR for this branch — nothing to classify (pass a PR number, "
            "or run from the PR's branch)"
        )
    # The ONE reviewer-config read of this invocation (CLI01-WS04): the Roster
    # rides the snapshot; `build_rounds` reads the required set off it, and the
    # gather folds the already-recorded verdicts onto `ctx.verdicts`.
    ctx = gather(target, load_roster())
    rounds = build_rounds(ctx)
    latest = rounds[-1] if rounds else None

    if comment is None:
        pending = (
            unclassified_findings(latest, ctx.verdicts) if latest is not None else ()
        )
        result = ClassifyList(
            pr=target.number,
            round=latest.index if latest is not None else None,
            total=len(latest.findings) if latest is not None else 0,
            unclassified=tuple(
                FindingLine(
                    comment_id=f.comment_id,
                    path=f.path,
                    line=f.line,
                    author=f.author,
                    excerpt=_excerpt(f.body),
                )
                for f in pending
            ),
        )
        emit(result, format_list, as_json=as_json)
        return 0

    comment_id, verdict = comment
    if latest is None or comment_id not in {f.comment_id for f in latest.findings}:
        raise PrStateError(
            f"comment {comment_id} is not a finding of the latest review round "
            f"on PR #{target.number} — list the round's findings with "
            "`shipit pr classify`"
        )
    record_verdict(target.repo, target.number, comment_id, verdict, reason=reason)
    remaining = sum(
        1
        for f in unclassified_findings(latest, ctx.verdicts)
        if f.comment_id != comment_id
    )
    emit(
        ClassifyRecorded(
            pr=target.number,
            comment_id=comment_id,
            verdict=verdict,
            reason=reason,
            remaining=remaining,
        ),
        format_recorded,
        as_json=as_json,
    )
    return 0
