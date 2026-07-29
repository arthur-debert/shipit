"""The run/post/detach paths for a local review backend.

``prstate`` imports this module; ``review`` never imports ``prstate``.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from .. import execrun, gh, logcontext
from ..agent.backend import Backend
from ..pr import PrId
from . import checkrun, diff, fanout, ghauth, post, roundrecord, rounds
from .backends.base import BackendError
from .calibrator import CalibratorConfig
from .diff import ReviewError, resolve_pr
from .dimensions import resolve_dimensions

logger = logging.getLogger("shipit.review")


def generate_review(
    backend: Backend,
    ctx,
    *,
    instructions_path: str | None = None,
    model: str = "pro",
    timeout: str = "600s",
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    nit_cap: int | None = None,
    dry_run: bool = False,
    review_tree_naming: Mapping[str, str] | None = None,
) -> dict:
    """Run ``backend``'s review over ``ctx`` and return the routed review; never posts."""
    agent = backend.funnel_agent
    reviewer = agent or backend.name
    plan = (
        rounds.plan_for_view(ctx, reviewer)
        if not dry_run and rounds.planable(ctx)
        else rounds.RoundPlan(
            incremental=False,
            base=getattr(ctx, "base_sha", None),
            head=getattr(ctx, "head_sha", None),
        )
    )
    if plan.incremental:
        ctx = diff.rescoped_view(ctx, plan.base)
    logger.info(
        "review run: agent=%s model=%s timeout=%s starting (%s)",
        agent,
        model,
        timeout,
        (
            f"incremental fix-range {ctx.base_sha}..{ctx.head_sha}"
            if plan.incremental
            else ("dimension fan-out" if dimensions else "single full-scope pass")
        ),
        extra={"reviewer": agent, "pr": ctx.number},
    )
    start = time.monotonic()
    outcome = fanout.run_fanout_review(
        backend,
        ctx,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        dimensions=dimensions,
        calibrator=calibrator,
        nit_cap=nit_cap,
        incremental=plan.incremental,
        dry_run=dry_run,
        review_tree_naming=review_tree_naming,
    )
    review = outcome.review
    duration_ms = int((time.monotonic() - start) * 1000)
    summary = (review.get("summary") or {}) if isinstance(review, dict) else {}
    logger.info(
        "review run: agent=%s complete in %dms -> status=%s, %d comment(s)",
        agent,
        duration_ms,
        summary.get("status"),
        len((review.get("comments") or []) if isinstance(review, dict) else []),
        extra={"reviewer": agent, "pr": ctx.number, "duration_ms": duration_ms},
    )
    if not dry_run:
        _tee_round_record(
            backend,
            ctx,
            review,
            model=model,
            timeout=timeout,
            instructions_path=instructions_path,
            findings=outcome.findings,
            runs=outcome.runs,
            duration_ms=duration_ms,
            total_tokens=outcome.total_tokens,
            round_id=outcome.round_id or None,
            artifacts_dir=outcome.artifacts_dir,
            dimension_names=(
                None
                if plan.incremental or not dimensions
                else tuple(d.name for d in resolve_dimensions(dimensions))
            ),
        )
    return review


def _tee_round_record(
    backend: Backend,
    ctx,
    review: dict,
    *,
    model: str,
    timeout: str,
    instructions_path: str | None,
    findings=None,
    runs=(),
    duration_ms: int | None,
    total_tokens: int | None = None,
    round_id: str | None = None,
    artifacts_dir: str | None = None,
    dimension_names: Sequence[str] | None = None,
) -> None:
    """Tee the generated review into the local review-round record store — fail-open."""
    repo = getattr(ctx, "repo", None)
    if not repo:
        logger.warning(
            "review-round record skipped for pr#%s: ctx carries no repo identity",
            ctx.number,
            extra={"pr": ctx.number},
        )
        return
    try:
        path = roundrecord.record_round(
            review,
            repo_slug=repo,
            pr=ctx.number,
            base_sha=str(ctx.base_sha),
            head_sha=str(ctx.head_sha),
            reviewer=backend.funnel_agent or backend.name,
            model=model,
            timeout=timeout,
            instructions_path=instructions_path,
            findings=findings,
            runs=runs,
            duration_ms=duration_ms,
            total_tokens=total_tokens,
            round_id=round_id,
            artifacts_dir=artifacts_dir,
            dimension_names=dimension_names,
        )
    except Exception:  # noqa: BLE001 - the tee is telemetry; never degrade the review
        logger.warning(
            "review-round record write failed for pr#%s (the review is unaffected)",
            ctx.number,
            exc_info=True,
            extra={"pr": ctx.number},
        )
        return
    logger.info(
        "review-round record written for pr#%s -> %s",
        ctx.number,
        path,
        extra={"pr": ctx.number, "repo": repo},
    )


def _generate_post_and_close(
    backend: Backend,
    ctx,
    run_id: int | None,
    run_repo: str | None,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    nit_cap: int | None = None,
    event: str | None = None,
    as_app: bool = True,
    dry_run: bool = False,
    review_tree_naming: Mapping[str, str] | None = None,
) -> dict:
    """Generate the review for ``ctx``, post it, and close ``run_id``; never opens one."""
    try:
        review = generate_review(
            backend,
            ctx,
            instructions_path=instructions_path,
            model=model,
            timeout=timeout,
            dimensions=dimensions,
            calibrator=calibrator,
            nit_cap=nit_cap,
            dry_run=dry_run,
            review_tree_naming=review_tree_naming,
        )
        result = post.post_review(
            review,
            ctx,
            backend=backend,
            event=event,
            dry_run=dry_run,
            as_app=as_app,
        )
    except BackendError as exc:
        # Read the structured flag, not the message text: a timeout whose signal
        # lived in stderr must still class as `timed_out`.
        outcome = "timed_out" if exc.timed_out else "empty"
        _maybe_post_salvage(backend, ctx, exc, as_app=as_app, dry_run=dry_run)
        _close_funnel_breadcrumb(
            backend, run_repo, run_id, outcome=outcome, detail=str(exc)
        )
        raise
    except Exception as exc:  # noqa: BLE001 - any other failure is a degraded run
        _close_funnel_breadcrumb(
            backend, run_repo, run_id, outcome="failed", detail=str(exc)
        )
        raise
    _close_funnel_breadcrumb(backend, run_repo, run_id, outcome="success")
    return {"review": review, "post": result, "ctx_repo": ctx.repo, "pr": ctx.number}


#: Cap on the salvaged raw text posted to a PR comment. GitHub's review-body limit
#: is 65536 chars; stay under it to leave room for the marker + code fences.
_SALVAGE_MAX = 60000


def _safe_fence(content: str) -> str:
    """A backtick fence long enough that nothing inside ``content`` can close it early."""
    longest_run = max((len(m) for m in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest_run + 1)


def _salvage_body(agent: str | None, raw: str) -> tuple[str, bool]:
    """Build the salvage comment body from the agent's raw output — ``(body, truncated)``."""
    marker = (
        f"⚠️ {agent}'s structured review could not be parsed "
        "(truncated/invalid JSON); raw response below:"
    )
    truncated = len(raw) > _SALVAGE_MAX
    shown = raw[:_SALVAGE_MAX]
    note = "\n\n_(raw response truncated)_" if truncated else ""
    fence = _safe_fence(shown)
    return f"{marker}\n\n{fence}\n{shown}\n{fence}{note}", truncated


def _maybe_post_salvage(
    backend: Backend, ctx, exc: BackendError, *, as_app: bool, dry_run: bool
) -> None:
    """Post unparseable-but-non-empty agent output as a top-level review comment.

    Best-effort and never flips the run to success; the event is forced to
    ``COMMENT`` because there is no parsed status to approve or reject on.
    """
    raw = (getattr(exc, "raw", "") or "").strip()
    if not raw:
        return
    body, truncated = _salvage_body(backend.funnel_agent, raw)
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": body},
        "comments": [],
    }
    try:
        post.post_review(
            review,
            ctx,
            backend=backend,
            event="COMMENT",
            dry_run=dry_run,
            as_app=as_app,
        )
        logger.info(
            "salvaged unparseable %s review on pr#%s as a top-level comment "
            "(%d raw chars%s) — funnel still records the degraded outcome",
            backend.funnel_agent,
            ctx.number,
            len(raw),
            ", truncated" if truncated else "",
            extra={"pr": ctx.number, "repo": ctx.repo},
        )
    except Exception:  # noqa: BLE001 - salvage is best-effort, never fatal
        logger.warning(
            "could not post salvage comment on pr#%s (the degraded outcome is "
            "still recorded; the original review error still propagates)",
            ctx.number,
            exc_info=True,
            extra={"pr": ctx.number, "repo": ctx.repo},
        )


def start_detached_review(
    backend: Backend,
    pr: PrId,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    nit_cap: int | None = None,
    as_app: bool = True,
    spawn: Callable[[Sequence[str], Mapping[str, str]], None] | None = None,
    find: Callable[[Backend, str, str], int | None] | None = None,
) -> bool:
    """Open the in_progress funnel run and detach the review; ``True`` when a fresh
    child was spawned, ``False`` when it reconciled against an in-flight run.
    """
    logger.info(
        "review detach requested for pr#%s (agent=%s) — resolving + detaching",
        pr.number,
        backend.funnel_agent,
        extra={"pr": pr.number},
    )
    repo = pr.slug
    head_sha = _resolve_head_sha(pr)
    logcontext.bind(pr=pr.number, repo=repo)
    existing = _reconcile_inflight(backend, repo, head_sha, find, auth_fatal=as_app)
    if existing is not None:
        logger.info(
            "review detach reconciled against an existing in-flight run (id=%s) "
            "for pr#%s (agent=%s) — not opening or spawning a duplicate",
            existing,
            pr.number,
            backend.funnel_agent,
            extra={"pr": pr.number},
        )
        return False
    run_id = _open_breadcrumb(backend, repo, head_sha, auth_fatal=as_app)
    child_env = logcontext.env_export(run=run_id)
    argv = _child_argv(
        backend,
        pr,
        run_id=run_id,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        dimensions=dimensions,
        calibrator=calibrator,
        nit_cap=nit_cap,
        as_app=as_app,
    )
    try:
        (spawn or execrun.spawn_detached)(argv, env=child_env)
    except Exception as exc:  # noqa: BLE001 - any spawn failure must still close the run
        # Without a child, nothing else will ever close the run the parent opened.
        if run_id is not None:
            _close_funnel_breadcrumb(
                backend, repo, run_id, outcome="failed", detail=str(exc)
            )
        raise
    logger.info(
        "review detached for pr#%s (agent=%s, run id=%s) — in-flight",
        pr.number,
        backend.funnel_agent,
        run_id,
        extra={"pr": pr.number},
    )
    return True


def run_detached_review(
    backend: Backend,
    pr: PrId,
    *,
    run_id: int | None,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    nit_cap: int | None = None,
    as_app: bool = True,
    review_tree_naming: Mapping[str, str] | None = None,
) -> dict:
    """The detached child body: resolve fully, generate, post, close ``run_id``."""
    agent = backend.funnel_agent
    repo = pr.slug
    start = time.monotonic()
    logger.info(
        "review child started for pr#%s (agent=%s, repo=%s, run_id=%s)",
        pr.number,
        agent,
        repo,
        run_id,
        extra={"reviewer": agent, "pr": pr.number},
    )
    try:
        ctx = resolve_pr(pr.number, repo=repo)
        logger.info(
            "review target resolved for pr#%s (agent=%s) — %d changed file(s), "
            "%d chars diff; generating + posting",
            pr.number,
            agent,
            len(ctx.changed_files or []),
            len(ctx.diff or ""),
            extra={"reviewer": agent, "pr": pr.number},
        )
    except Exception as exc:  # noqa: BLE001 - any resolve failure must still resolve the run
        # This region sits outside `_generate_post_and_close`'s own close, so
        # without this the parent-opened run would stay stuck `in_progress`.
        duration_ms = int((time.monotonic() - start) * 1000)
        if run_id is not None:
            _close_funnel_breadcrumb(
                backend, repo, run_id, outcome="failed", detail=str(exc)
            )
            logger.error(
                "review resolve failed for pr#%s (agent=%s) after %dms — "
                "closed run %s as failed",
                pr.number,
                agent,
                duration_ms,
                run_id,
                exc_info=True,
                extra={"reviewer": agent, "pr": pr.number, "duration_ms": duration_ms},
            )
        else:
            logger.error(
                "review resolve failed for pr#%s (agent=%s) after %dms — "
                "no run to close (parent opened none)",
                pr.number,
                agent,
                duration_ms,
                exc_info=True,
                extra={"reviewer": agent, "pr": pr.number, "duration_ms": duration_ms},
            )
        raise
    try:
        result = _generate_post_and_close(
            backend,
            ctx,
            run_id,
            repo,
            model=model,
            timeout=timeout,
            instructions_path=instructions_path,
            dimensions=dimensions,
            calibrator=calibrator,
            nit_cap=nit_cap,
            as_app=as_app,
            review_tree_naming=review_tree_naming,
        )
    except Exception:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "review child failed for pr#%s (agent=%s) after %dms",
            pr.number,
            agent,
            duration_ms,
            exc_info=True,
            extra={"reviewer": agent, "pr": pr.number, "duration_ms": duration_ms},
        )
        raise
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "review child done for pr#%s (agent=%s) in %dms",
        pr.number,
        agent,
        duration_ms,
        extra={"reviewer": agent, "pr": pr.number, "duration_ms": duration_ms},
    )
    return result


def _resolve_head_sha(pr: PrId) -> str:
    """Cheaply resolve the head sha for ``pr``; a malformed upstream read raises ``ReviewError``."""
    try:
        core = gh.pr_core(pr)
    except (ValueError, KeyError) as exc:
        raise ReviewError(
            f"could not resolve target core for #{pr.number} from `gh` output "
            f"(repo={pr.slug!r}): {exc}"
        ) from exc
    return str(core.head_sha)


def _child_argv(
    backend: Backend,
    pr: PrId,
    *,
    run_id: int | None,
    model: str,
    timeout: str,
    instructions_path: str | None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    nit_cap: int | None = None,
    as_app: bool,
) -> list[str]:
    """The argv for the detached child — a ``shipit pr review _run`` subinvocation.

    The child shares no state with the parent, so every value it needs must be an
    explicit argument here; flags at their shipped default are omitted.
    """
    argv = [
        sys.executable,
        "-m",
        "shipit",
        "pr",
        "review",
        "_run",
        "--agent",
        backend.funnel_agent or backend.name,
        "--pr",
        str(pr.number),
        "--repo",
        pr.slug,
        "--model",
        model,
        "--timeout",
        timeout,
        "--as-app" if as_app else "--no-as-app",
    ]
    if run_id is not None:
        argv += ["--run-id", str(run_id)]
    if instructions_path is not None:
        argv += ["--instructions", instructions_path]
    if dimensions:
        argv += ["--dimensions", ",".join(dimensions)]
    if nit_cap is not None:
        argv += ["--nit-cap", str(nit_cap)]
    if calibrator is not None:
        argv += ["--calibrator-backend", calibrator.backend]
        if calibrator.model is not None:
            argv += ["--calibrator-model", calibrator.model]
        argv += ["--calibrator-reasoning", calibrator.reasoning]
        argv += ["--calibrator-timeout", calibrator.timeout]
    return argv


#: Funnel outcome → (check-run ``conclusion``, output ``title``, output ``summary``).
#: The readiness snapshot reads only the conclusion, never the output text, so
#: ``empty`` needs its own conclusion to stay distinguishable from ``failed``.
_FUNNEL_TERMINAL: dict[str, tuple[str, str, str]] = {
    "success": (
        "success",
        "Local review posted",
        "The local review completed and posted its verdict to the PR.",
    ),
    "failed": (
        "failure",
        "Local review failed",
        "The local review backend errored before a verdict could be posted.",
    ),
    "empty": (
        "neutral",
        "Local review empty",
        "The local review returned nothing parseable (empty) — a degraded "
        "non-delivery, NOT a clean zero-findings review.",
    ),
    "timed_out": (
        "timed_out",
        "Local review timed out",
        "The local review backend timed out before returning a complete review.",
    ),
}


def _reconcile_inflight(
    backend: Backend,
    repo: str,
    head_sha: str,
    find: Callable[[Backend, str, str], int | None] | None,
    *,
    auth_fatal: bool,
) -> int | None:
    """The in-flight funnel run to reconcile against, else ``None`` — best-effort.

    A read failure degrades to ``None`` (at worst a duplicate run), but with
    ``auth_fatal`` a :class:`~shipit.review.ghauth.ReviewAuthError` propagates: the
    child needs the same auth, so proceeding would report a false in-flight.
    """
    try:
        return (find or checkrun.find_nonterminal)(backend, repo, head_sha)
    except Exception as exc:  # noqa: BLE001 - the reconcile read is best-effort
        if auth_fatal and isinstance(exc, ghauth.ReviewAuthError):
            raise
        logger.warning(
            "review in-flight reconcile lookup failed for %s "
            "on %s (proceeding to open a fresh run)",
            backend.check_run_name,
            repo,
            exc_info=True,
        )
        return None


def _open_breadcrumb(
    backend: Backend, repo: str, head_sha: str, *, auth_fatal: bool
) -> int | None:
    """Open the ``in_progress`` funnel check run on ``repo@head_sha`` — best-effort.

    Any failure degrades to ``None`` (no breadcrumb, the review still posts), but
    with ``auth_fatal`` a :class:`~shipit.review.ghauth.ReviewAuthError` propagates.
    """
    try:
        run_id = checkrun.create(backend, repo, head_sha)
        logger.info(
            "funnel check run opened for %s on %s (run id=%s)",
            backend.check_run_name,
            repo,
            run_id,
        )
        return run_id
    except Exception as exc:  # noqa: BLE001 - the breadcrumb is best-effort, never fatal
        if auth_fatal and isinstance(exc, ghauth.ReviewAuthError):
            raise
        logger.warning(
            "funnel check run create failed for %s (continuing to post the review)",
            backend.check_run_name,
            exc_info=True,
        )
        return None


def _close_funnel_breadcrumb(
    backend: Backend, repo, run_id, *, outcome: str, detail: str | None = None
) -> None:
    """Transition the funnel run to its terminal ``outcome`` — best-effort, never raises."""
    if run_id is None or repo is None:
        return
    terminal = _FUNNEL_TERMINAL.get(outcome)
    if terminal is None:
        logger.warning(
            "unknown funnel outcome %r for %s (run id=%s); recording it as 'failed'",
            outcome,
            backend.check_run_name,
            run_id,
        )
        terminal = _FUNNEL_TERMINAL["failed"]
    conclusion, title, base_summary = terminal
    summary = f"{base_summary}\n\n{detail}" if detail else base_summary
    try:
        checkrun.transition(
            backend, repo, run_id, conclusion=conclusion, title=title, summary=summary
        )
        logger.info(
            "funnel check run closed for %s on %s (run id=%s) -> completed/%s",
            backend.check_run_name,
            repo,
            run_id,
            conclusion,
        )
    except Exception:  # noqa: BLE001 - best-effort; never masks the review outcome
        logger.warning(
            "funnel check run transition failed for "
            "%s (run id=%s); the review outcome is unaffected",
            backend.check_run_name,
            run_id,
            exc_info=True,
        )
