"""`shipit pr review` — the PR-scoped review subgroup: request, replay."""

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
    """Request (or re-request) review(s) on PR and verify the request attached."""
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
    """Review commit RANGE offline and write the review-round record — no PR touched."""
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
    """Run the offline review over the resolved range; returns an exit code."""
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

    try:
        parse_duration(timeout)
    except ValueError as exc:
        raise ReviewError(f"invalid --timeout {timeout!r}: {exc}") from exc

    if instructions is not None:
        try:
            load_instructions(instructions)
        except OSError as exc:
            raise ReviewError(
                f"cannot read review instructions {instructions!r}: {exc}"
            ) from exc

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
    """INTERNAL — the detached local-review child entrypoint (hidden, not a verb)."""
    from ...identity import repo_from_slug
    from ...logsetup import configure_logging_for_slug
    from ...review import service
    from ...review.calibrator import CalibratorConfig

    configure_logging_for_slug(repo)

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

    try:
        target = PrId(repo=repo_from_slug(repo), number=pr)
    except ValueError as exc:
        print(
            f"error: invalid --repo/--pr for the review child: {exc}", file=sys.stderr
        )
        raise SystemExit(1) from None

    dimension_names = (
        tuple(name.strip() for name in dimensions.split(",") if name.strip())
        if dimensions
        else None
    )
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
    """Request and verify review on one PR; returns an exit code."""
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
        raise PrStateError(
            "review request dropped by GitHub (no review_requested edge "
            f"created): {', '.join(result.dropped)} (service stall / quota) — "
            "retry later"
        )
    return 0


def format_request(pr: int, result: RequestResult) -> str:
    """Each reviewer's outcome, one line apiece."""
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
