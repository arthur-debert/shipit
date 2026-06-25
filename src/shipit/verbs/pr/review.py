"""`shipit pr review request` — request (or re-request) reviewer(s), verifying
the request actually attached.

`pr review` is a SUBGROUP under `pr` (so the invocation is
`shipit pr review request [PR] [--reviewer NAME]`), leaving room for sibling
review verbs later without crowding the top-level `pr` group. This module owns
only the thin CLI: parse args -> resolve the PR -> pick the adapter scope ->
call the shared `_request.request_reviewers` helper -> render its outcomes ->
map to an exit code. All the request/verify logic (the #614 attach poll, the
bare-run skip) lives in `_request`, which WS06's `pr next` reuses.

Scope:
  * bare run (no `--reviewer`): the REQUIRED reviewer set, SKIPPING any reviewer
    already done on the current head (don't re-poke a finished reviewer).
  * `--reviewer NAME`: force that one reviewer regardless of state (the manual
    re-run escape hatch); the name resolves through the adapter registry.

Errors map like `status.py`: a `gh`/auth failure (resolving the branch's PR, or
inside the request/verify) prints a clean stderr line and exits non-zero. A
local-agent reviewer (`codex-local`/`agy-local`) surfaces the foundation's guard
— a clean `GhError` ("not yet available"), never a crash. A silently-dropped
remote request is a hard failure (non-zero exit), never a silent park.
"""

from __future__ import annotations

import sys

import click

from ...prstate import ghapi
from ...prstate.reviewers import REGISTRY, ReviewerAdapter, by_name, required_reviewers
from ._request import RequestResult, request_reviewers
from ._resolve import resolve_pr


@click.group(
    name="review",
    help=(
        "Reviewer acts — request (or re-request) review(s) on a PR.\n\n"
        "`request` places the required reviewers' requests and verifies each "
        "actually attached."
    ),
)
def cmd() -> None:
    """Root of the ``pr review`` subgroup; verbs attach below."""


@cmd.command(name="request")
@click.argument("pr", required=False, type=int)
@click.option(
    "--reviewer",
    "reviewer",
    default=None,
    help=(
        "Force one reviewer (an adapter registry name) regardless of state. "
        "Omitted: request every required reviewer still pending on the head."
    ),
)
def request_cmd(pr: int | None, reviewer: str | None) -> None:
    """Request (or re-request) review(s) on PR and verify the request attached.

    PR is the number; omitted, it resolves the current branch's PR. With no
    ``--reviewer`` the scope is the required set, skipping reviewers already done
    on the current head; ``--reviewer NAME`` forces that one. A dropped request
    (no ``review_requested`` edge created) or a ``gh`` failure exits non-zero.
    """
    raise SystemExit(run(pr, reviewer=reviewer))


def run(pr: int | None = None, *, reviewer: str | None = None) -> int:
    """Resolve -> select scope -> request + verify -> render. Returns an exit code.

    0 when every request placed AND verified (or was a recorded no-op / skip);
    non-zero on a bad reviewer name, an unresolvable PR, a `gh`/auth failure
    (including the local-agent guard), or a silently-dropped remote request.
    """
    adapters = _select(reviewer)
    if adapters is None:
        return 1

    try:
        resolved = resolve_pr(pr)
        if resolved is None:
            print(
                "error: no PR for the current branch — open a draft PR first, "
                "or pass a PR number",
                file=sys.stderr,
            )
            return 1
        result = request_reviewers(resolved, adapters, force=reviewer is not None)
    except ghapi.GhError as exc:
        # A real gh/auth failure OR the local-agent guard (requesting
        # codex-local/agy-local raises a clean GhError, not a crash). Both are
        # surfaced as a clean stderr line + non-zero exit.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _emit(resolved, result)
    return 0 if result.ok else 1


def _select(reviewer: str | None) -> list[ReviewerAdapter] | None:
    """The adapters to act on: the required set, or the one named.

    Returns None (a usage failure) when ``--reviewer`` names an unknown reviewer,
    after printing the known names — a typo never silently drops a request.
    """
    if reviewer is None:
        return required_reviewers()
    adapter = by_name(reviewer)
    if adapter is None:
        known = ", ".join(r.name for r in REGISTRY)
        print(f"error: unknown reviewer {reviewer!r} (known: {known})", file=sys.stderr)
        return None
    return [adapter]


def _emit(pr: int, result: RequestResult) -> None:
    """Render each reviewer's outcome; dropped lines go to stderr."""
    for name in result.skipped:
        print(f"{name}: already reviewed #{pr} (review-once) — skip")
    for name in result.no_op:
        print(f"{name}: auto-triggers, no request mechanism — no-op")
    for name in result.posted:
        print(f"posted review: {name} on #{pr}")
    for name in result.verified:
        print(f"verified: {name} request attached on #{pr}")
    # A bare run that skipped every reviewer placed nothing — say so explicitly
    # rather than exit silently.
    acted = result.no_op + result.posted + result.verified + result.dropped
    if result.skipped and not acted:
        print(f"all required reviewers already reviewed #{pr} — nothing to request")
    for name in result.dropped:
        print(
            f"{name}: request dropped by GitHub: no review_requested edge "
            "created (service stall / quota) — retry later",
            file=sys.stderr,
        )
