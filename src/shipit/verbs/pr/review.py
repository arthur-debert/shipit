"""`shipit pr review` — the review subgroup: request reviewer(s), replay a range.

`pr review` is a SUBGROUP under `pr` (so the invocation is
`shipit pr review request [PR] [--reviewer NAME]`), leaving room for sibling
review verbs without crowding the top-level `pr` group — `replay` (RVW02-WS03;
fan-out arm RVW03-WS01) is one: an OFFLINE review of an arbitrary commit range
— one monolithic pass, or with `--fanout` the full dimension fan-out (the
sanctioned experiment driver) — that writes the local review-round record and
touches no PR. This module owns
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
        "Reviewer acts — request (or re-request) review(s) on a PR, or replay "
        "a commit range offline.\n\n"
        "`request` places the required reviewers' requests and verifies each "
        "actually attached. `replay` reviews an arbitrary commit range with a "
        "local agent and writes the review-round record — no PR is touched."
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


@cmd.command(name="replay")
@click.argument("range_spec", metavar="RANGE")
@click.option(
    "--agent",
    "agent",
    default="codex",
    show_default=True,
    help="The local review backend to run (a funnel agent: codex, agy).",
)
@click.option("--model", "model", default="pro", show_default=True)
@click.option("--timeout", "timeout", default="600s", show_default=True)
@click.option(
    "--instructions",
    "instructions",
    default=None,
    help="Path to review instructions (default: the bundled instructions).",
)
@click.option(
    "--fanout",
    "fanout",
    is_flag=True,
    default=False,
    help=(
        "Run the full dimension fan-out over the range (the sanctioned offline "
        "experiment driver, RVW03-WS01) instead of one monolithic pass."
    ),
)
@click.option(
    "--dimensions",
    "dimensions",
    default=None,
    help=(
        "Comma-separated dimension pass set for --fanout (default: the concern "
        "fan-out set, ADR-0045)."
    ),
)
@click.option(
    "--calibrator-backend",
    "calibrator_backend",
    default=None,
    help="Opt the dormant calibrator ON for --fanout (a spawn backend token).",
)
@click.option("--calibrator-model", "calibrator_model", default=None)
@click.option("--calibrator-reasoning", "calibrator_reasoning", default=None)
@click.option("--calibrator-timeout", "calibrator_timeout", default=None)
def replay_cmd(
    range_spec: str,
    agent: str,
    model: str,
    timeout: str,
    instructions: str | None,
    fanout: bool,
    dimensions: str | None,
    calibrator_backend: str | None,
    calibrator_model: str | None,
    calibrator_reasoning: str | None,
    calibrator_timeout: str | None,
) -> None:
    """Review commit RANGE offline and write the review-round record — no PR touched.

    RANGE is `<base>..<head>` (review exactly that diff) or `<base>...<head>`
    (review from their merge base — the spelling that replays a historical PR's
    round 1 as `merge-base...first-round-head`). Endpoints are any revisions the
    checkout already has; replay never fetches. The review is generated by the
    local agent in THIS checkout, nothing is posted anywhere, and the round
    record (findings with dispositions, coverage, range, variant) lands in the
    harness-owned local store `shipit eval report` reads.

    `--fanout` runs the configured dimension passes in parallel over the range
    — the SAME fan-out the live PR path runs, reading the range's `git diff`
    instead of `gh pr diff` — and records one round with `round.runs` populated
    per pass: the sanctioned way to run a fan-out experiment cell. Any
    `--calibrator-*` flag opts the dormant judge on for the run (absent fields
    keep the shipped defaults; the role agent-defs it needs are provisioned
    into this checkout). All other flags apply to either arm unchanged.
    """
    raise SystemExit(
        run_replay(
            range_spec,
            agent=agent,
            model=model,
            timeout=timeout,
            instructions=instructions,
            fanout=fanout,
            dimensions=dimensions,
            calibrator_backend=calibrator_backend,
            calibrator_model=calibrator_model,
            calibrator_reasoning=calibrator_reasoning,
            calibrator_timeout=calibrator_timeout,
        )
    )


@cli_errors
def run_replay(
    range_spec: str,
    *,
    agent: str,
    model: str,
    timeout: str,
    instructions: str | None,
    fanout: bool = False,
    dimensions: str | None = None,
    calibrator_backend: str | None = None,
    calibrator_model: str | None = None,
    calibrator_reasoning: str | None = None,
    calibrator_timeout: str | None = None,
) -> int:
    """Resolve the range -> run the offline review -> write the record. Exit code.

    ``fanout`` selects the dimension fan-out arm (RVW03-WS01) over the
    monolithic single pass; ``dimensions`` (comma-joined names) and the four
    ``calibrator_*`` fields are its config surface, mirroring the detached
    child's — any calibrator field present mints the table-level
    :class:`~shipit.review.calibrator.CalibratorConfig` (absent fields keep the
    shipped defaults), all absent keeps the judge OFF (the deduped union).

    User-facing failures are DOMAIN refusals through the one
    :func:`~.._errors.cli_errors` shell (exit 1, one ``error: …`` line): a bad
    range spec / unknown revision / repo-less checkout
    (:class:`~shipit.review.diff.ReviewError` out of the replay resolver), an
    unknown ``--agent`` (normalized here), a malformed ``--timeout``, an
    unreadable ``--instructions`` file, a fan-out flag without ``--fanout``, an
    unknown ``--dimensions`` name and a malformed ``--calibrator-*`` field (all
    preflighted here, before any model run bills), and a backend that is
    missing/failed/timed out — including a fan-out whose every pass failed and
    a calibrator contract violation — normalized here from the producer's and
    fan-out's error set.
    """
    from ...review import replay as replay_mod
    from ...review.backends import BackendError, BackendUnavailable
    from ...review.calibrator import CalibratorConfig
    from ...review.diff import ReviewError
    from ...review.dimensions import known_dimension_names, resolve_dimensions
    from ...review.instructions import load_instructions
    from ...tree.cleanup import parse_duration

    try:
        backend = _agent_backend.by_funnel_agent(agent)
    except KeyError:
        known = ", ".join(
            b.funnel_agent or "" for b in _agent_backend.funnel_backends()
        )
        raise ReviewError(f"unknown review agent {agent!r} (known: {known})") from None

    # Preflight the timeout BEFORE launching: a malformed `--timeout` raises a
    # raw ValueError deep in the producer's `_seam_deadline`, which is NOT in the
    # normalized error set below — validate it here (user input on a new CLI
    # path) so a typo dies as one clean `error: …` line, never a traceback, and
    # never after a model run already billed.
    try:
        parse_duration(timeout)
    except ValueError as exc:
        raise ReviewError(f"invalid --timeout {timeout!r}: {exc}") from exc

    # Preflight the instructions BEFORE resolving or launching anything: a
    # missing/unreadable file must die as one clean line, not as a traceback
    # mid-replay (and never after a model run already billed).
    if instructions is not None:
        try:
            load_instructions(instructions)
        except OSError as exc:
            raise ReviewError(
                f"cannot read review instructions {instructions!r}: {exc}"
            ) from exc

    # The fan-out config surface (RVW03-WS01), preflighted the same way: the
    # fan-out-only flags refuse without --fanout (a silently-ignored flag would
    # mislabel the experiment arm), an unknown dimension name and a malformed
    # calibrator field die here as one clean line — before any model run bills.
    calibrator_fields = {
        "backend": calibrator_backend,
        "model": calibrator_model,
        "reasoning": calibrator_reasoning,
        "timeout": calibrator_timeout,
    }
    calibrator_given = any(value is not None for value in calibrator_fields.values())
    if not fanout and (dimensions is not None or calibrator_given):
        raise ReviewError(
            "--dimensions and --calibrator-* apply only to the fan-out arm — "
            "pass --fanout to run the dimension fan-out over the range"
        )
    dimension_names = (
        tuple(name.strip() for name in dimensions.split(",") if name.strip())
        if dimensions
        else None
    )
    if dimension_names:
        try:
            resolve_dimensions(dimension_names)
        except KeyError as exc:
            raise ReviewError(
                f"unknown review dimension {exc.args[0]!r} — known dimensions: "
                f"{', '.join(known_dimension_names())}"
            ) from None
    calibrator = None
    if calibrator_given:
        try:
            calibrator = CalibratorConfig(
                **{k: v for k, v in calibrator_fields.items() if v is not None}
            )
        except ValueError as exc:
            raise ReviewError(f"invalid --calibrator-* options: {exc}") from exc

    view = replay_mod.resolve_range(range_spec)
    try:
        if fanout:
            result = replay_mod.run_fanout_replay(
                backend,
                view,
                model=model,
                timeout=timeout,
                instructions_path=instructions,
                dimensions=dimension_names,
                calibrator=calibrator,
            )
        else:
            result = replay_mod.run_replay(
                backend,
                view,
                model=model,
                timeout=timeout,
                instructions_path=instructions,
            )
    except (BackendUnavailable, BackendError, RuntimeError) as exc:
        # The producer's + fan-out's error set (missing CLI / unparseable /
        # timed out / nonzero child / every pass failed / a calibrator contract
        # violation) — normalized to the review path's domain refusal so
        # the shell renders one clean line; there is no funnel breadcrumb to
        # settle here (replay has no check run).
        raise ReviewError(str(exc)) from exc
    review = result["review"]
    comments = review.get("comments") or []
    arm = "fan-out" if fanout else "single pass"
    print(
        f"replayed {view.base_sha}..{view.head_sha} with {agent} ({arm}): "
        f"{len(comments)} finding(s), status "
        f"{(review.get('summary') or {}).get('status')}"
    )
    print(f"round record: {result['record_path']} (no PR touched)")
    return 0


@cmd.command(name="_run", hidden=True)
@click.option("--agent", "agent", required=True)
@click.option("--pr", "pr", required=True, type=int)
@click.option("--repo", "repo", required=True)
@click.option("--run-id", "run_id", default=None, type=int)
@click.option("--model", "model", default="pro")
@click.option("--timeout", "timeout", default="600s")
@click.option("--instructions", "instructions", default=None)
@click.option("--dimensions", "dimensions", default=None)
@click.option("--nit-cap", "nit_cap", default=None, type=int)
@click.option("--calibrator-backend", "calibrator_backend", default=None)
@click.option("--calibrator-model", "calibrator_model", default=None)
@click.option("--calibrator-reasoning", "calibrator_reasoning", default=None)
@click.option("--calibrator-timeout", "calibrator_timeout", default=None)
@click.option("--as-app/--no-as-app", "as_app", default=True)
def run_internal_cmd(
    agent: str,
    pr: int,
    repo: str,
    run_id: int | None,
    model: str,
    timeout: str,
    instructions: str | None,
    dimensions: str | None,
    nit_cap: int | None,
    calibrator_backend: str | None,
    calibrator_model: str | None,
    calibrator_reasoning: str | None,
    calibrator_timeout: str | None,
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
    from ...review.calibrator import CalibratorConfig

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

    # The RVW02-WS04 config surface arrives as explicit arguments, mirroring
    # `_child_argv` (the child never re-reads config): `--dimensions` is the
    # comma-joined per-reviewer pass set; the four `--calibrator-*` fields
    # reconstruct the table-level CalibratorConfig — any of them present mints
    # one (absent fields keep the shipped defaults); all absent means the
    # shipped default calibrator. A malformed field dies here as one clean
    # line, at the process boundary, before any model run bills.
    dimension_names = (
        tuple(name.strip() for name in dimensions.split(",") if name.strip())
        if dimensions
        else None
    )
    # `--nit-cap` is a non-negative budget (0 = floor at minor), validated at the
    # config boundary (`_parse_nit_cap`); the child entrypoint enforces the same
    # floor so a hand-built child argv dies here as one clean line — CLI parity
    # with the malformed-`--calibrator-*` guard below — before any model run bills.
    if nit_cap is not None and nit_cap < 0:
        print(
            f"error: invalid --nit-cap for the review child: must be a "
            f"non-negative integer of round-1 nits (0 = floor at minor), "
            f"got {nit_cap}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    calibrator = None
    calibrator_fields = {
        "backend": calibrator_backend,
        "model": calibrator_model,
        "reasoning": calibrator_reasoning,
        "timeout": calibrator_timeout,
    }
    if any(value is not None for value in calibrator_fields.values()):
        try:
            calibrator = CalibratorConfig(
                **{k: v for k, v in calibrator_fields.items() if v is not None}
            )
        except ValueError as exc:
            print(
                f"error: invalid --calibrator-* options for the review child: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    service.run_detached_review(
        backend,
        target,
        run_id=run_id,
        model=model,
        timeout=timeout,
        instructions_path=instructions,
        dimensions=dimension_names,
        calibrator=calibrator,
        nit_cap=nit_cap,
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
