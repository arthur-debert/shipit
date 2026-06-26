"""service — the programmatic run-and-post path for a local review backend.

This is the in-process entry the ``prstate`` reviewer adapters call to GENERATE
a review and POST it, without shelling through a CLI.

Two functions, layered:

  * :func:`generate_review` — resolve the backend, preflight it, build the shared
    prompt over a resolved PR's diff, and run the backend → the parsed review
    dict. No GitHub posting.
  * :func:`run_and_post` — resolve the PR, generate the review, and post it via
    :func:`shipit.review.post.post_review`, returning a small result dict.

``prstate`` may import this module (``prstate → review`` is a ONE-WAY edge —
``review`` never imports ``prstate``), so the reviewer adapters' synchronous
``request`` can run a local review here.
"""

from __future__ import annotations

import logging

from .. import gh
from . import checkrun, post
from .backends import get_backend
from .backends.base import _TIMEOUT_MARKER, BackendError
from .diff import resolve_pr
from .instructions import load_instructions
from .prompt import build_prompt
from .schema import REVIEW_SCHEMA

#: The review path's logger — a child of the package ``shipit`` logger. A local
#: review run (start, the backend/agent invoked, and the outcome) is recorded
#: here at DEBUG/INFO so an async, detached run (OBS03) leaves a durable record.
#: The review text and the diff are deliberately summarised, not dumped.
logger = logging.getLogger("shipit.review")


def generate_review(
    agent: str,
    ctx,
    *,
    instructions_path: str | None = None,
    model: str = "pro",
) -> dict:
    """Run ``agent`` over ``ctx``'s diff and return the parsed review dict.

    Resolves the backend, preflights it (a missing CLI raises
    :class:`~shipit.review.backends.base.BackendUnavailable`, which is allowed to
    propagate — these are LOCAL backends and a missing binary must fail loud),
    loads the review instructions (bundled default unless ``instructions_path``
    is given), builds the shared prompt over ``ctx.diff`` (with the schema
    described in-prose only for ``agy``, which has no native schema enforcement),
    and runs the backend in ``ctx.workdir``.
    """
    logger.info(
        "review run: agent=%s model=%s starting (backend resolve)", agent, model
    )
    backend = get_backend(agent, model=model)
    backend.preflight()
    instructions = load_instructions(instructions_path)
    prompt = build_prompt(instructions, ctx.diff, schema_inline=(agent == "agy"))
    logger.debug(
        "review run: agent=%s invoking backend over diff (%d bytes) in %s",
        agent,
        len(ctx.diff or ""),
        ctx.workdir,
    )
    review = backend.run(prompt, REVIEW_SCHEMA, cwd=ctx.workdir)
    summary = (review.get("summary") or {}) if isinstance(review, dict) else {}
    logger.info(
        "review run: agent=%s complete -> status=%s, %d comment(s)",
        agent,
        summary.get("status"),
        len((review.get("comments") or []) if isinstance(review, dict) else []),
    )
    return review


def run_and_post(
    agent: str,
    pr: int,
    *,
    repo: str | None = None,
    model: str = "pro",
    instructions_path: str | None = None,
    event: str | None = None,
    as_app: bool = True,
    dry_run: bool = False,
) -> dict:
    """Resolve ``pr``, generate a review with ``agent``, and post it.

    Returns ``{"review": <dict>, "post": <dict>, "ctx_repo": <str|None>,
    "pr": <int>}``.

    With ``as_app=True`` (the default), the review is posted AS the agent's
    GitHub App (``adr-<agent>-review[bot]``) — the App credentials are sourced
    from Doppler at post time (:mod:`shipit.review.ghauth`); there is no local
    app-registration step to precheck. ``event=None`` lets the review's own
    summary status drive APPROVE/REQUEST_CHANGES/COMMENT (the bot is a distinct
    identity, so a self-review 422 does not apply).
    """
    logger.info(
        "run_and_post: agent=%s pr=#%s repo=%s as_app=%s dry_run=%s",
        agent,
        pr,
        repo,
        as_app,
        dry_run,
    )
    ctx = resolve_pr(pr, repo=repo)
    run_id, run_repo = _open_funnel_breadcrumb(agent, ctx)
    try:
        review = generate_review(
            agent, ctx, instructions_path=instructions_path, model=model
        )
        result = post.post_review(
            review,
            ctx,
            agent_name=agent,
            event=event,
            dry_run=dry_run,
            as_app=as_app,
        )
    except BackendError as exc:
        # A backend that ran but produced no usable review: the agy timeout marker
        # in its output means it TIMED OUT (-> timed_out); any other unparseable /
        # empty output is the degraded "empty" non-delivery (-> failure, NOT
        # success — distinct from a clean zero-findings review which posts).
        outcome = "timed_out" if _TIMEOUT_MARKER in str(exc).lower() else "empty"
        _close_funnel_breadcrumb(
            agent, run_repo, run_id, outcome=outcome, detail=str(exc)
        )
        # Record the breadcrumb, then RE-RAISE so the caller still sees the real
        # review failure (the adapter normalizes it to GhError).
        raise
    except Exception as exc:  # noqa: BLE001 - any other failure is a degraded run
        # The agent errored (missing CLI, crash) or the review POST failed.
        _close_funnel_breadcrumb(
            agent, run_repo, run_id, outcome="failed", detail=str(exc)
        )
        raise
    # Success — incl. a clean zero-findings review: the review POST above already
    # fired unchanged; now close the funnel run to completed/success.
    _close_funnel_breadcrumb(agent, run_repo, run_id, outcome="success")
    logger.info("run_and_post: agent=%s pr=#%s done", agent, pr)
    return {"review": review, "post": result, "ctx_repo": ctx.repo, "pr": pr}


#: Funnel outcome → (check-run ``conclusion``, output ``title``, output
#: ``summary``). The mapping ADR-0005 fixes: a posted review (incl. a clean
#: zero-findings one) is ``success``; a failed run is ``failure``; an EMPTY run
#: (no parseable review — the agy mode) is ``failure`` with an explicit "empty"
#: reason — a non-delivery, deliberately NOT ``success`` (``neutral`` would be an
#: accepted alternative); a timeout is ``timed_out``.
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
        "failure",
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


def _open_funnel_breadcrumb(agent, ctx) -> tuple[int | None, str | None]:
    """Open the kickoff funnel check run for this review — BEST-EFFORT.

    Opens the ``in_progress`` ``review: <agent>-local`` check run
    (:func:`shipit.review.checkrun.create`) so the same flow that kicks the
    review off leaves the *requested / in-flight* breadcrumb that GitHub denies
    these App bots a native edge for. Returns ``(run_id, repo)`` for the terminal
    :func:`_close_funnel_breadcrumb` to transition the SAME run — both ``None`` on
    any failure, so the close is a clean skip (nothing was created).

    **A breadcrumb failure must NEVER fail the review.** Per the OBS02
    prerequisite, until the App's ``checks:write`` re-grant propagates everywhere
    a create can ``403``; the local review must still post regardless. So every
    failure here is caught, logged through the OBS01 sink (the failure FACT only —
    the installation token never reaches a record, mirroring ``post.py``), and
    swallowed, leaving ``generate_review`` / ``post_review`` unaffected.

    The repo slug is ``ctx.repo`` when set, else inferred from the checkout
    (``gh.current_repo()``) — the same source ``post.post_review`` resolves to.
    """
    try:
        repo = ctx.repo or gh.current_repo()
        run_id = checkrun.create(agent, repo, ctx.head_sha)
        logger.info(
            "run_and_post: opened funnel check run for %s-local on %s (run id=%s)",
            agent,
            repo,
            run_id,
        )
        return run_id, repo
    except Exception as exc:  # noqa: BLE001 - the breadcrumb is best-effort, never fatal
        # Record the failure fact (never the token) and proceed — the review post
        # is unaffected by a missing/denied check-runs scope.
        logger.warning(
            "run_and_post: funnel check run create failed for %s-local "
            "(continuing to post the review): %s",
            agent,
            exc,
        )
        return None, None


def _close_funnel_breadcrumb(
    agent, repo, run_id, *, outcome: str, detail: str | None = None
) -> None:
    """Transition the funnel run to its terminal ``outcome`` — BEST-EFFORT.

    Maps ``outcome`` (``success`` / ``failed`` / ``empty`` / ``timed_out``) through
    :data:`_FUNNEL_TERMINAL` to the check-run ``conclusion`` + ``output`` message
    and PATCHes the SAME run :func:`_open_funnel_breadcrumb` opened
    (:func:`shipit.review.checkrun.transition`).

    Two best-effort guards, so the breadcrumb NEVER crashes the flow or masks the
    review's real outcome:

      * if ``create`` returned no run id (``run_id is None`` — e.g. a ``403``
        before the ``checks:write`` re-grant left no run), there is nothing to
        transition, so SKIP cleanly; and
      * a PATCH/mint failure is caught, logged through the OBS01 sink (the failure
        FACT only — the installation token never reaches a record), and swallowed.

    On the success path the review has already posted; on a failure path the caller
    re-raises the real review error AFTER this records the terminal breadcrumb.
    """
    if run_id is None or repo is None:
        return
    # Defensive: an unexpected/typo outcome must not KeyError out of this
    # best-effort path and mask the review's real result — fall back to the
    # `failed` mapping (a degraded breadcrumb beats a crash) and log the fact.
    terminal = _FUNNEL_TERMINAL.get(outcome)
    if terminal is None:
        logger.warning(
            "run_and_post: unknown funnel outcome %r for %s-local (run id=%s); "
            "recording it as 'failed'",
            outcome,
            agent,
            run_id,
        )
        terminal = _FUNNEL_TERMINAL["failed"]
    conclusion, title, base_summary = terminal
    summary = f"{base_summary}\n\n{detail}" if detail else base_summary
    try:
        checkrun.transition(
            agent, repo, run_id, conclusion=conclusion, title=title, summary=summary
        )
        logger.info(
            "run_and_post: closed funnel check run for %s-local on %s "
            "(run id=%s) -> completed/%s",
            agent,
            repo,
            run_id,
            conclusion,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort; never masks the review outcome
        logger.warning(
            "run_and_post: funnel check run transition failed for %s-local "
            "(run id=%s); the review outcome is unaffected: %s",
            agent,
            run_id,
            exc,
        )
