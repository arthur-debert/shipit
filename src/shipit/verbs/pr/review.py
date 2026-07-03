"""`shipit pr review request` — request (or re-request) reviewer(s), verifying
the request actually attached. Glue + renderers.

`pr review` is a SUBGROUP under `pr` (so the invocation is
`shipit pr review request [PR] [--reviewer NAME]`), leaving room for sibling
review verbs later without crowding the top-level `pr` group. This module owns
only the thin CLI: parse args -> resolve the typed PR target -> pick the
adapter scope -> call the engine's reviewer-request service
(:func:`shipit.prstate.request.request_reviewers` — CLI01-WS03 promoted it out
of this package) -> render its outcomes through the shared emit. All the
request/verify logic (the #614 attach poll, the bare-run skip) and its durable
per-outcome logging live in the service, which `pr next`'s request act reuses.

Scope:
  * bare run (no `--reviewer`): the REQUIRED reviewer set, SKIPPING any reviewer
    already done on the current head (don't re-poke a finished reviewer).
  * `--reviewer NAME`: force that one reviewer regardless of state (the manual
    re-run escape hatch); the name resolves through the adapter registry
    (:func:`shipit.prstate.reviewers.resolve_reviewer`, which also accepts the
    PRD spelling `codex-local`/`agy-local` of a local-agent reviewer).

Errors route through the one :func:`~.._errors.cli_errors` shell: a `gh`/auth
failure (resolving the branch's PR, or inside the request/verify), an unknown
reviewer name, a branch with no PR, the local-agent guard's clean refusal, and
a silently-dropped remote request all surface as one uniform ``error: …``
stderr line + exit 1 — never a crash, never a silent park.
"""

from __future__ import annotations

import sys

import click

from ...agent import backend as _agent_backend
from ...gh import resolve_pr
from ...identity import Repo
from ...pr import PrId
from ...prstate.errors import PrStateError
from ...prstate.request import RequestResult, request_reviewers
from ...prstate.reviewers import required_adapters, resolve_reviewer
from ...prstate.reviewers_config import load_roster
from .._context import ambient_identity
from .._errors import cli_errors
from .._params import pr_number_argument
from .._render import emit


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
@pr_number_argument
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
    from ...identity import repo_from_slug
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

    # The child's own entry point (ADR-0030): it does NOT read the root
    # context — its repo arrives deterministic and explicit (`--repo`), and the
    # PrId is minted HERE, at the process boundary, through the one canonical
    # slug parser.
    try:
        target = PrId(repo=repo_from_slug(repo), number=pr)
    except ValueError as exc:
        print(
            f"error: invalid --repo/--pr for the review child: {exc}", file=sys.stderr
        )
        raise SystemExit(1) from None
    service.run_detached_review(
        backend,
        target,
        run_id=run_id,
        model=model,
        timeout=timeout,
        instructions_path=instructions,
        as_app=as_app,
    )


@cli_errors
def run(
    pr: int | None = None,
    *,
    reviewer: str | None = None,
    repo: Repo | None = None,
) -> int:
    """Resolve -> select scope -> request + verify -> render. Returns an exit code.

    ``repo`` is the identity half of the PR target: omitted (the CLI path), the
    root context's ambient repo — resolved once per invocation (ADR-0030); a
    direct caller (a test) injects it as a value.

    0 when every request placed AND verified (or was a recorded no-op / skip).
    A bad reviewer name, an unresolvable PR, a `gh`/auth failure (including the
    local-agent guard), and a silently-dropped remote request all raise into
    the :func:`~shipit.verbs._errors.cli_errors` shell — one ``error: …``
    stderr line, exit 1.
    """
    # The ONE reviewer-config read of this invocation (CLI01-WS04): the Roster
    # feeds the bare-run required set AND rides into the request path, where a
    # local reviewer's run options are read off its entry — never re-resolved.
    roster = load_roster()
    adapters = (
        required_adapters(roster) if reviewer is None else [resolve_reviewer(reviewer)]
    )
    target = resolve_pr(pr, *ambient_identity(repo))
    if target is None:
        raise PrStateError(
            "no PR for the current branch — open a draft PR first, or pass a PR number"
        )
    result = request_reviewers(target, adapters, roster, force=reviewer is not None)
    emit(result, lambda outcome: format_request(target.number, outcome))
    if not result.ok:
        # A remote request edge was silently dropped (#614) — a hard runtime
        # failure through the shell, never a silent park at reviews-pending.
        raise PrStateError(
            "review request dropped by GitHub (no review_requested edge "
            f"created): {', '.join(result.dropped)} (service stall / quota) — "
            "retry later"
        )
    return 0


def format_request(pr: int, result: RequestResult) -> str:
    """The pure text renderer: each reviewer's outcome, one line apiece.

    A plain string function (the render seam owns the terminal; the durable
    per-outcome records live with the service, ADR-0029). A bare run that
    skipped every reviewer placed nothing — said explicitly rather than
    rendering silence.
    """
    lines = [
        *(
            f"{name}: already reviewed #{pr} (review-once) — skip"
            for name in result.skipped
        ),
        *(
            f"{name}: auto-triggers, no request mechanism — no-op"
            for name in result.no_op
        ),
        *(
            f"review in flight: {name} on #{pr} (detached — poll the PR for the outcome)"
            for name in result.in_flight
        ),
        *(f"verified: {name} request attached on #{pr}" for name in result.verified),
        *(
            f"{name}: request dropped by GitHub — no review_requested edge created"
            for name in result.dropped
        ),
    ]
    acted = result.no_op + result.in_flight + result.verified + result.dropped
    if result.skipped and not acted:
        lines.append(
            f"all required reviewers already reviewed #{pr} — nothing to request"
        )
    return "\n".join(lines)
