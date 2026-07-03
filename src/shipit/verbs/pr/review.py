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
— a clean `PrStateError` ("not yet available"), never a crash. A silently-dropped
remote request is a hard failure (non-zero exit), never a silent park.
"""

from __future__ import annotations

import logging
import sys

import click

from ... import execrun
from ...agent import backend as _agent_backend
from ...prstate.errors import PrStateError
from ...prstate.reviewers import (
    REGISTRY,
    ReviewerAdapter,
    by_name,
    required_adapters,
)
from ...prstate.reviewers_config import load_roster
from ...prstate.roster import Roster
from ._request import RequestResult, request_reviewers
from ._resolve import resolve_pr

#: The `pr` verbs' logger (LOG02 spray, ADR-0029): each reviewer's request
#: outcome is a lifecycle fact — before this, the prints were its only record.
logger = logging.getLogger("shipit.pr")


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


@cmd.command(name="_run", hidden=True)
@click.option("--agent", "agent", required=True)
@click.option("--pr", "pr", required=True, type=int)
@click.option("--repo", "repo", required=True)
@click.option("--run-id", "run_id", default=None, type=int)
@click.option("--model", "model", default="pro")
@click.option("--timeout", "timeout", default="600s")
@click.option("--instructions", "instructions", default=None)
@click.option("--as-app/--no-as-app", "as_app", default=True)
def run_internal_cmd(
    agent: str,
    pr: int,
    repo: str,
    run_id: int | None,
    model: str,
    timeout: str,
    instructions: str | None,
    as_app: bool,
) -> None:
    """INTERNAL — the detached local-review child entrypoint (hidden, not a verb).

    Spawned by `_LocalReviewAdapter.request()` as a new-session subinvocation
    (`python -m shipit pr review _run …`); it carries everything it needs as
    arguments and shares NO state with the parent. It runs the agent over the PR
    diff, posts the review as the bot, and closes the SAME funnel `run_id` the
    parent opened — so there is exactly ONE check run. Hidden so it never shows in
    `--help`: humans use `pr review request`, which detaches this.

    Logging: the root group callback already configured logging, but it resolves
    the ambient identity best-effort off cwd (the ADR-0030 root context) — which can degrade
    in a terminal-less child and leave the run with NO file sink. This child KNOWS
    its repo deterministically (the `--repo` arg), so it re-wires the OBS01 file
    sink from that slug (via the canonical `identity.repo_from_slug` parser) —
    best-effort — so the detached run's diagnostics normally reach
    `<logdir>/<owner>/<name>/shipit.log` (OBS03 story 5). A malformed slug or
    logging-setup failure is swallowed (returns False) and never crashes the review.
    """
    from ...logsetup import configure_logging_for_slug
    from ...review import service

    configure_logging_for_slug(repo)

    # `--agent` carries the funnel-agent alias (`codex` / `agy`); resolve it back
    # to the ONE registry identity (ADR-0025) at this process boundary — the rest
    # of the funnel path threads the Backend, never a bare name string.
    try:
        backend = _agent_backend.by_funnel_agent(agent)
    except KeyError:
        known = ", ".join(
            b.funnel_agent or "" for b in _agent_backend.funnel_backends()
        )
        print(
            f"error: unknown review agent {agent!r} (known: {known})",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    service.run_detached_review(
        backend,
        pr,
        repo=repo,
        run_id=run_id,
        model=model,
        timeout=timeout,
        instructions_path=instructions,
        as_app=as_app,
    )


def run(pr: int | None = None, *, reviewer: str | None = None) -> int:
    """Resolve -> select scope -> request + verify -> render. Returns an exit code.

    0 when every request placed AND verified (or was a recorded no-op / skip);
    non-zero on a bad reviewer name, an unresolvable PR, a `gh`/auth failure
    (including the local-agent guard), or a silently-dropped remote request.
    """
    # The ONE reviewer-config read of this invocation (CLI01-WS04): the Roster
    # feeds the bare-run scope AND rides into the request path, where a local
    # reviewer's run options are read off its entry — never re-resolved.
    roster = load_roster()
    adapters = _select(reviewer, roster)
    if adapters is None:
        return 1

    resolved: int | None = None
    try:
        resolved = resolve_pr(pr)
        if resolved is None:
            print(
                "error: no PR for the current branch — open a draft PR first, "
                "or pass a PR number",
                file=sys.stderr,
            )
            return 1
        result = request_reviewers(
            resolved, adapters, roster, force=reviewer is not None
        )
    except (execrun.ExecError, PrStateError) as exc:
        # A real gh/auth failure OR the local-agent guard (requesting
        # codex-local/agy-local raises a clean PrStateError, not a crash). Both are
        # surfaced as a clean stderr line + non-zero exit.
        logger.error("pr review request failed", exc_info=True, extra={"pr": resolved})
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _emit(resolved, result)
    return 0 if result.ok else 1


def _select(reviewer: str | None, roster: Roster) -> list[ReviewerAdapter] | None:
    """The adapters to act on: the Roster's required set, or the one named.

    Returns None (a usage failure) when ``--reviewer`` names an unknown reviewer,
    after printing the known names — a typo never silently drops a request.

    The local-agent reviewers are spelled ``codex-local`` / ``agy-local`` in the
    PRD/glossary (and in the foundation guard's own message), but their adapter
    registry names are the bare ``codex`` / ``agy``. So a ``-local`` suffix is
    accepted as an alias for the base adapter: ``--reviewer codex-local`` resolves
    the ``codex`` adapter and therefore surfaces the local-agent guard, rather than
    being rejected as an unknown name.
    """
    if reviewer is None:
        return required_adapters(roster)
    adapter = by_name(reviewer) or _resolve_local_alias(reviewer)
    if adapter is None:
        known = ", ".join(r.name for r in REGISTRY)
        print(f"error: unknown reviewer {reviewer!r} (known: {known})", file=sys.stderr)
        return None
    return [adapter]


def _resolve_local_alias(reviewer: str) -> ReviewerAdapter | None:
    """Resolve a funnel reviewer name (``codex-local``) to its base adapter, else None.

    A REGISTRY LOOKUP, not a string parse (COR02-WS03): the name resolves through
    :func:`shipit.agent.backend.by_check_run_name` — the inverse of the registry's
    ``check_run_name`` alias — so only a real funnel backend's reviewer name is
    reachable this way (``copilot-local`` matches no registry entry and does not
    alias to ``copilot``: the alias names the local-agent reviewer family
    specifically).
    """
    try:
        backend = _agent_backend.by_check_run_name(reviewer)
    except KeyError:
        return None
    return by_name(backend.funnel_agent or backend.name)


def _emit(pr: int, result: RequestResult) -> None:
    """Render each reviewer's outcome; dropped lines go to stderr.

    Each outcome ALSO logs (LOG02 convergence — the prints were the only record
    of the request act): the transitions that moved something (verified,
    in-flight) are INFO milestones, the deliberate non-acts (skip, no-op) are
    DEBUG mechanics, and a dropped request is a WARNING — degraded, surfaced
    to the caller via the non-zero exit rather than an exception.
    """
    for name in result.skipped:
        logger.debug(
            "reviewer %s already reviewed pr#%s (review-once) — skipped",
            name,
            pr,
            extra={"pr": pr, "reviewer": name},
        )
        print(f"{name}: already reviewed #{pr} (review-once) — skip")
    for name in result.no_op:
        logger.debug(
            "reviewer %s auto-triggers on pr#%s — no request mechanism, no-op",
            name,
            pr,
            extra={"pr": pr, "reviewer": name},
        )
        print(f"{name}: auto-triggers, no request mechanism — no-op")
    for name in result.in_flight:
        logger.info(
            "review in flight from %s on pr#%s (detached)",
            name,
            pr,
            extra={"pr": pr, "reviewer": name},
        )
        print(
            f"review in flight: {name} on #{pr} (detached — poll the PR for the "
            "outcome)"
        )
    for name in result.verified:
        logger.info(
            "review request from %s attached on pr#%s (verified)",
            name,
            pr,
            extra={"pr": pr, "reviewer": name},
        )
        print(f"verified: {name} request attached on #{pr}")
    # A bare run that skipped every reviewer placed nothing — say so explicitly
    # rather than exit silently.
    acted = result.no_op + result.in_flight + result.verified + result.dropped
    if result.skipped and not acted:
        print(f"all required reviewers already reviewed #{pr} — nothing to request")
    for name in result.dropped:
        logger.warning(
            "review request from %s dropped by GitHub on pr#%s — no "
            "review_requested edge created",
            name,
            pr,
            extra={"pr": pr, "reviewer": name},
        )
        print(
            f"{name}: request dropped by GitHub: no review_requested edge "
            "created (service stall / quota) — retry later",
            file=sys.stderr,
        )
