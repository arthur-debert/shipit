"""service — the run/post/detach paths for a local review backend.

This is the entry the ``prstate`` reviewer adapters call to GENERATE a review and
POST it. Since OBS03 a local review runs ASYNC: the reviewer adapter's
``request`` no longer blocks on the model run — it detaches a child process and
returns in-flight.

Layered functions:

  * :func:`generate_review` — resolve the backend, preflight it, build the shared
    prompt over a resolved PR's diff, and run the backend → the parsed review
    dict. No GitHub posting.
  * :func:`run_and_post` — resolve the PR, open the funnel breadcrumb, generate
    the review, and post it via :func:`shipit.review.post.post_review` (the
    SYNCHRONOUS composition; the funnel suite exercises the create+close pairing
    through it).
  * :func:`start_detached_review` — the OBS03 PARENT entry the reviewer adapter
    calls: do the cheap synchronous work (resolve ``(repo, head_sha)``, open the
    ``in_progress`` breadcrumb), spawn a DETACHED child, and return immediately.
  * :func:`run_detached_review` — the OBS03 CHILD body (run by the hidden
    ``shipit pr review _run`` command in the detached process): resolve fully,
    generate, post, and close the SAME ``run_id`` the parent opened — so there is
    exactly ONE check run.

``prstate`` may import this module (``prstate → review`` is a ONE-WAY edge —
``review`` never imports ``prstate``), so the reviewer adapters' ``request`` can
detach a local review here.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence

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
    timeout: str = "600s",
) -> dict:
    """Run ``agent`` over ``ctx``'s diff and return the parsed review dict.

    Resolves the backend, preflights it (a missing CLI raises
    :class:`~shipit.review.backends.base.BackendUnavailable`, which is allowed to
    propagate — these are LOCAL backends and a missing binary must fail loud),
    loads the review instructions (bundled default unless ``instructions_path``
    is given), builds the shared prompt over ``ctx.diff`` (with the schema
    described in-prose only for ``agy``, which has no native schema enforcement),
    and runs the backend in ``ctx.workdir``.

    ``timeout`` is the per-run agent timeout (a ``<N>s`` duration string,
    defaulting to ``600s``); it is threaded to the backend, where ``agy`` applies
    it as ``--print-timeout`` (``codex`` accepts it for interface parity).
    """
    logger.info(
        "review run: agent=%s model=%s timeout=%s starting (backend resolve)",
        agent,
        model,
        timeout,
    )
    backend = get_backend(agent, model=model, timeout=timeout)
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
    timeout: str = "600s",
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
    result = _generate_post_and_close(
        agent,
        ctx,
        run_id,
        run_repo,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        event=event,
        as_app=as_app,
        dry_run=dry_run,
    )
    logger.info("run_and_post: agent=%s pr=#%s done", agent, pr)
    return result


def _generate_post_and_close(
    agent: str,
    ctx,
    run_id: int | None,
    run_repo: str | None,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    event: str | None = None,
    as_app: bool = True,
    dry_run: bool = False,
) -> dict:
    """Generate the review for ``ctx``, post it, and CLOSE ``run_id`` to terminal.

    The shared body of both the synchronous :func:`run_and_post` and the detached
    :func:`run_detached_review`: it does NOT open a breadcrumb (its caller already
    did — the synchronous path here, the async PARENT for the child) — it only
    closes the run it is handed. Every outcome (success / empty / failed /
    timed_out) flips the run through :func:`_close_funnel_breadcrumb` before
    returning or re-raising, so the terminal-mapping logic lives in ONE place.
    """
    try:
        review = generate_review(
            agent,
            ctx,
            instructions_path=instructions_path,
            model=model,
            timeout=timeout,
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
    return {"review": review, "post": result, "ctx_repo": ctx.repo, "pr": ctx.number}


def start_detached_review(
    agent: str,
    pr: int,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    as_app: bool = True,
    spawn: Callable[[Sequence[str]], None] | None = None,
    find: Callable[[str, str, str], int | None] | None = None,
) -> bool:
    """Open the in_progress funnel run, DETACH the review, return in-flight (OBS03).

    The PARENT half of the async inversion: it does ONLY the cheap, synchronous
    work — resolve ``(repo, head_sha)`` via the lightweight ``gh pr view``,
    RECONCILE against any in-flight run, and open the OBS02 ``in_progress``
    breadcrumb (best-effort) — then spawns a DETACHED child (``shipit pr review
    _run``) that runs the model, posts the review, and closes the SAME ``run_id`` to
    its terminal state. It returns ``True`` (in-flight) WITHOUT blocking on the model
    run; the outcome is read LATER from the PR (the funnel check run + the posted
    review), never from this return.

    **Idempotent reconcile (OBS03-WS03, issue #41):** because the check run IS the
    store, a re-request for a reviewer whose funnel run is already non-terminal on
    THIS head must NOT open a second breadcrumb + spawn a second child that
    double-posts. So BEFORE creating + spawning, this reads whether such a run exists
    (:func:`shipit.review.checkrun.find_nonterminal`) and, if so, reconciles —
    reports in-flight and returns ``True`` without creating or spawning. No local /
    daemon state: the check run is the only source of truth (ADR-0005 / #41).

    The breadcrumb create is BEST-EFFORT — a 403 before the ``checks:write``
    re-grant (or any failure) must not fail the request, so the child still runs
    with ``run_id=None`` (no in_progress marker, but the review still posts).
    ``spawn`` is the injected detach boundary (default: a new-session, no-daemon
    :func:`_spawn_detached`) and ``find`` the injected reconcile-lookup boundary
    (default: :func:`shipit.review.checkrun.find_nonterminal`) — mirrored injectable
    seams so a test asserts reconcile + detach WITHOUT the network or a fork.
    """
    logger.info(
        "start_detached_review: agent=%s pr=#%s — resolving + detaching", agent, pr
    )
    repo, head_sha = _resolve_target(pr)
    existing = _reconcile_inflight(agent, repo, head_sha, find)
    if existing is not None:
        logger.info(
            "start_detached_review: agent=%s pr=#%s reconciled against existing "
            "in-flight run (id=%s) — not opening or spawning a duplicate",
            agent,
            pr,
            existing,
        )
        return True
    run_id = _open_breadcrumb(agent, repo, head_sha)
    argv = _child_argv(
        agent,
        pr,
        repo=repo,
        run_id=run_id,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        as_app=as_app,
    )
    try:
        (spawn or _spawn_detached)(argv)
    except Exception:
        # The spawn is what the child relies on to reach its terminal close. If it
        # fails AFTER the parent opened the in_progress run, no child will ever
        # close that run — it would hang `in_progress` forever. Close it as failed
        # here (only when a run was actually opened), then re-raise so the reviewer
        # adapter still normalizes the request failure to `GhError`. (This is only
        # the PARENT-observed spawn failure; the child's own self-resolution
        # catch-all is OBS03-WS03's deliverable, issue #41.)
        if run_id is not None:
            _close_funnel_breadcrumb(agent, repo, run_id, outcome="failed")
        raise
    logger.info(
        "start_detached_review: agent=%s pr=#%s detached (run id=%s) — in-flight",
        agent,
        pr,
        run_id,
    )
    return True


def run_detached_review(
    agent: str,
    pr: int,
    *,
    repo: str | None,
    run_id: int | None,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    as_app: bool = True,
) -> dict:
    """The detached CHILD body: resolve fully, generate, post, close ``run_id``.

    Run inside the detached child process (by the hidden ``shipit pr review _run``
    command). The PARENT (:func:`start_detached_review`) already opened the
    ``in_progress`` funnel run and handed its ``run_id`` here; this does the heavy
    work the request path deliberately skipped — the full :func:`resolve_pr`
    (fetch + merge-base + diff) — then generates + posts the review and CLOSES that
    SAME ``run_id`` to its terminal state. The parent creates, the child closes:
    exactly ONE check run, never two.

    The child's diagnostics land in the OBS01 file sink: the child entrypoint
    (``shipit pr review _run``) attempts to wire the per-repo file sink
    DETERMINISTICALLY from its ``--repo`` argument before calling this (best-effort —
    a malformed slug or logging-setup failure is swallowed), so a detached process
    with no terminal normally leaves a durable record (OBS03 story 5) — independent
    of the bootstrap's best-effort cwd resolution. Each step here is recorded (resolve,
    generate, post, terminal transition) so a reader of the sink can reconstruct
    what the run did and why it ended where it did. ``run_id`` is ``None`` only when
    the parent's best-effort create failed; the review still posts and the terminal
    close cleanly skips.

    Self-resolution covers EVERY observable outcome (OBS03-WS03, issue #41): the
    heavy :func:`resolve_pr` is wrapped so a fetch/auth/network failure closes the
    parent-opened ``run_id`` to ``failed`` instead of dying before
    :func:`_generate_post_and_close` and leaving the run stuck ``in_progress``
    forever; everything past resolve is closed by :func:`_generate_post_and_close`
    with its OWN conclusion (success / empty→failure / backend-error→failure / agy
    timeout-marker→timed_out). The guard's scope is PRECISELY the resolve region that
    helper does not cover — it deliberately does NOT wrap the helper, so a correct
    ``timed_out`` / ``empty`` close is never overwritten with ``failed``. A
    CATASTROPHIC child-startup death (a crash in click parsing / import, OOM, a
    reboot — before/outside these guards) is the *vanished-process* case: it leaves
    the run ``in_progress`` with its ``started_at``, resolved by OBS04's wait window
    ageing that timestamp. WS03 does NOT implement that window — it only relies on it
    as the backstop (PRD "Failure & Timeout").
    """
    logger.info(
        "run_detached_review: agent=%s pr=#%s repo=%s run_id=%s — child start",
        agent,
        pr,
        repo,
        run_id,
    )
    try:
        ctx = resolve_pr(pr, repo=repo)
        # The heavy resolve (fetch + merge-base + diff) the request path deliberately
        # skipped is now done — record its shape (NOT the diff text) so the detached
        # run's file-sink record shows what was reviewed.
        logger.info(
            "run_detached_review: agent=%s pr=#%s resolved — %d changed file(s), "
            "%d chars diff; generating + posting",
            agent,
            pr,
            len(ctx.changed_files or []),
            len(ctx.diff or ""),
        )
    except Exception as exc:  # noqa: BLE001 - any resolve failure must still resolve the run
        # The resolve region is OUTSIDE `_generate_post_and_close`'s own
        # terminal-close region, so a failure here would otherwise kill the child
        # before any close — leaving the parent-opened run stuck `in_progress`.
        # Close it `failed` (only when the parent actually opened a run) and RE-RAISE
        # so the failure is still surfaced. This is the ONLY close on the resolve
        # path; the helper below owns every post-resolve outcome's close.
        if run_id is not None:
            _close_funnel_breadcrumb(
                agent, repo, run_id, outcome="failed", detail=str(exc)
            )
            logger.warning(
                "run_detached_review: agent=%s pr=#%s resolve failed — closed run "
                "%s as failed: %s",
                agent,
                pr,
                run_id,
                exc,
            )
        else:
            logger.warning(
                "run_detached_review: agent=%s pr=#%s resolve failed — no run to "
                "close (parent opened none): %s",
                agent,
                pr,
                exc,
            )
        raise
    result = _generate_post_and_close(
        agent,
        ctx,
        run_id,
        repo,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        as_app=as_app,
    )
    logger.info("run_detached_review: agent=%s pr=#%s — child done", agent, pr)
    return result


def _resolve_target(pr: int) -> tuple[str, str]:
    """Cheaply resolve ``(repo, head_sha)`` for ``pr`` — the FAST synchronous path.

    Uses the lightweight ``gh repo view`` + ``gh pr view`` (the same ``headRefOid``
    source :func:`resolve_pr` reads), NOT the full diff resolve — that
    fetch/merge-base/diff is the detached child's work, so the request stays fast.
    A ``gh``/auth failure PROPAGATES (the reviewer adapter normalizes it to
    ``GhError``); the breadcrumb create that follows is the only best-effort step.
    """
    repo = gh.current_repo()
    raw = gh.pr_view(str(pr), json_fields=["headRefOid"])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise gh.GhError(f"unparseable `gh pr view` output for #{pr}: {exc}") from exc
    # A truthy non-dict (e.g. a JSON list) would AttributeError on `.get`; and a
    # missing/empty `headRefOid` would silently degrade the breadcrumb create to an
    # in-flight reply with no target commit. This is the synchronous validation
    # path, so both are real failures — raise `GhError` (like the unparseable case
    # above) so the request fails loud instead of degrading.
    head_sha = data.get("headRefOid") if isinstance(data, dict) else None
    if not head_sha:
        raise gh.GhError(f"`gh pr view` output for #{pr} has no headRefOid: {raw!r}")
    return repo, head_sha


def _child_argv(
    agent: str,
    pr: int,
    *,
    repo: str,
    run_id: int | None,
    model: str,
    timeout: str,
    instructions_path: str | None,
    as_app: bool,
) -> list[str]:
    """The argv for the detached child — a ``shipit pr review _run`` subinvocation.

    The child reconstructs everything it needs from these arguments + the PR; it
    shares NO state with the parent (no daemon, no job-store file — the PR + check
    run are the only state). Invoked via ``python -m shipit`` so it does not depend
    on the ``shipit`` console-script being on the child's PATH.
    """
    argv = [
        sys.executable,
        "-m",
        "shipit",
        "pr",
        "review",
        "_run",
        "--agent",
        agent,
        "--pr",
        str(pr),
        "--repo",
        repo,
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
    return argv


def _spawn_detached(argv: Sequence[str]) -> None:
    """Spawn ``argv`` as a DETACHED child — survives the parent exiting, no daemon.

    ``start_new_session=True`` puts the child in its own session/process group, so
    it is not killed when the parent exits and has no controlling terminal; stdio
    is redirected to ``/dev/null`` because the child's diagnostics go to the OBS01
    file sink, not a pipe the parent would have to drain. Fire-and-forget: the
    handle is intentionally not retained — the PR + check run are the only state.
    """
    subprocess.Popen(  # noqa: S603 - argv is built internally, not from user input
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


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
    except Exception as exc:  # noqa: BLE001 - the breadcrumb is best-effort, never fatal
        logger.warning(
            "run_and_post: funnel check run repo resolution failed for %s-local "
            "(continuing to post the review): %s",
            agent,
            exc,
        )
        return None, None
    run_id = _open_breadcrumb(agent, repo, ctx.head_sha)
    return (run_id, repo) if run_id is not None else (None, None)


def _reconcile_inflight(
    agent: str,
    repo: str,
    head_sha: str,
    find: Callable[[str, str, str], int | None] | None,
) -> int | None:
    """Look up an in-flight funnel run to RECONCILE against — BEST-EFFORT (OBS03-WS03).

    The idempotency read: the check run IS the store, so a re-request for a reviewer
    whose funnel run is still non-terminal on THIS head must reconcile against it
    (report in-flight) instead of opening a second breadcrumb + spawning a second
    child that double-posts. Returns the existing run id when one is in flight, else
    ``None`` (the caller proceeds to open + spawn a fresh run).

    Best-effort like :func:`_open_breadcrumb`: the lookup rides the SAME App-token
    boundary, which can ``403`` before the ``checks`` re-grant propagates. A read
    failure must not fail the request, so it is logged (the failure FACT only — the
    installation token never reaches a record) and treated as "no in-flight run" — at
    worst a duplicate run, never a blocked request. ``find`` is injected so a test
    simulates "already in-flight" without the network.
    """
    try:
        return (find or checkrun.find_nonterminal)(agent, repo, head_sha)
    except Exception as exc:  # noqa: BLE001 - the reconcile read is best-effort
        logger.warning(
            "start_detached_review: in-flight reconcile lookup failed for %s-local "
            "on %s (proceeding to open a fresh run): %s",
            agent,
            repo,
            exc,
        )
        return None


def _open_breadcrumb(agent: str, repo: str, head_sha: str) -> int | None:
    """Open the ``in_progress`` funnel check run on ``repo@head_sha`` — BEST-EFFORT.

    The shared create both the synchronous :func:`_open_funnel_breadcrumb` and the
    async parent (:func:`start_detached_review`) use, so the "a breadcrumb failure
    must NEVER fail the review" rule lives in ONE place. Any failure (a 403 before
    the ``checks:write`` re-grant, an auth/``gh`` failure) is logged through the
    OBS01 sink (the failure FACT only — the installation token never reaches a
    record) and swallowed, returning ``None`` so the flow proceeds with no
    breadcrumb. Returns the new run's id otherwise.
    """
    try:
        run_id = checkrun.create(agent, repo, head_sha)
        logger.info(
            "opened funnel check run for %s-local on %s (run id=%s)",
            agent,
            repo,
            run_id,
        )
        return run_id
    except Exception as exc:  # noqa: BLE001 - the breadcrumb is best-effort, never fatal
        # Record the failure fact (never the token) and proceed — the review post
        # is unaffected by a missing/denied check-runs scope.
        logger.warning(
            "funnel check run create failed for %s-local "
            "(continuing to post the review): %s",
            agent,
            exc,
        )
        return None


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
