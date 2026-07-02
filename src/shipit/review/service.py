"""service — the run/post/detach paths for a local review backend.

This is the entry the ``prstate`` reviewer adapters call to GENERATE a review and
POST it. Since OBS03 a local review runs ASYNC: the reviewer adapter's
``request`` no longer blocks on the model run — it detaches a child process and
returns in-flight.

Layered functions:

  * :func:`generate_review` — delegate to the Tree-fetch producer
    (:func:`shipit.review.producer.run_tree_review`): provision a shared read-only
    Tree (ADR-0018) on the PR head, launch the agent through its spawn read-only
    posture with a task that fetches the diff itself via ``gh pr diff``, and CAPTURE
    its structured stdout → the parsed review dict. No GitHub posting.
  * :func:`start_detached_review` — the OBS03 PARENT entry the reviewer adapter
    calls: do the cheap synchronous work (resolve ``(repo, head_sha)``, reconcile
    against any in-flight run, open the ``in_progress`` breadcrumb), spawn a
    DETACHED child, and return immediately (in-flight).
  * :func:`run_detached_review` — the OBS03 CHILD body (run by the hidden
    ``shipit pr review _run`` command in the detached process): resolve fully,
    then generate, post, and close the SAME ``run_id`` the parent opened (the
    shared terminal body is :func:`_generate_post_and_close`) — so there is
    exactly ONE check run.

``prstate`` may import this module (``prstate → review`` is a ONE-WAY edge —
``review`` never imports ``prstate``), so the reviewer adapters' ``request`` can
detach a local review here.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from .. import execrun, gh, logcontext
from ..pr import CORE_JSON_FIELDS, core_from_node, repo_from_slug
from . import checkrun, post, producer
from .backends.base import BackendError
from .diff import ReviewError, resolve_pr

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
    dry_run: bool = False,
) -> dict:
    """Run ``agent`` as a reviewer in a read-only Tree and return the parsed review.

    The Tree-fetch producer (ADR-0020 §Reviewer-path reconciliation — REPLACE): instead
    of front-loading ``ctx.diff`` into the prompt and running the CLI in the consumer's
    checkout, this provisions a shared read-only Tree (ADR-0018) on the PR head, launches
    the agent through its spawn read-only posture with a task that fetches the scoped diff
    itself via ``gh pr diff`` (never assuming the base is ``main``) and emits structured
    JSON, then CAPTURES that stdout into the review dict. Posting + the funnel check-run
    are the caller's job, unchanged — this only PRODUCES the review.

    Delegates to :func:`shipit.review.producer.run_tree_review`, which owns the Tree, the
    spawn-adapter launch, and the parse. A missing CLI raises
    :class:`~shipit.review.backends.base.BackendUnavailable` and an unparseable / timed-out
    run raises :class:`~shipit.review.backends.base.BackendError` — both propagate exactly
    as before so the service's outcome mapping is unchanged.

    ``timeout`` is the per-run agent timeout (a ``<N>s`` duration string); it reaches
    ``agy`` as ``--print-timeout`` (``codex`` has no per-run timeout flag — parity only).
    ``dry_run`` prints the would-run Tree-launch argv and bills nothing.
    """
    logger.info(
        "review run: agent=%s model=%s timeout=%s starting (read-only Tree producer)",
        agent,
        model,
        timeout,
        extra={"reviewer": agent, "pr": ctx.number},
    )
    start = time.monotonic()
    review = producer.run_tree_review(
        agent,
        ctx,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        dry_run=dry_run,
    )
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
    return review


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

    The shared terminal body of the detached :func:`run_detached_review`: it does
    NOT open a breadcrumb (the async PARENT, :func:`start_detached_review`, already
    did) — it only closes the run it is handed. Every outcome (success / empty /
    failed / timed_out) flips the run through :func:`_close_funnel_breadcrumb`
    before returning or re-raising, so the terminal-mapping logic lives in ONE
    place.
    """
    try:
        review = generate_review(
            agent,
            ctx,
            instructions_path=instructions_path,
            model=model,
            timeout=timeout,
            dry_run=dry_run,
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
        # A backend that ran but produced no usable review: a TIMEOUT means it
        # settles ``timed_out``; any other unparseable / empty output is the
        # degraded "empty" non-delivery (-> failure, NOT success — distinct from a
        # clean zero-findings review which posts). We read the STRUCTURED
        # ``exc.timed_out`` flag, NOT a string match on the message: a timeout
        # whose signal lived in stderr (or whose message paraphrases the marker)
        # is still classed correctly (the producer sets the flag explicitly).
        outcome = "timed_out" if exc.timed_out else "empty"
        # SALVAGE (#76): the agent produced CONTENT but unparseable JSON — don't
        # drop it. Post the raw text as a single top-level comment so the human
        # still gets the feedback and the failure is debuggable from the PR. This is
        # ADDITIVE and best-effort: the funnel still records the degraded `outcome`
        # below (the salvage NEVER flips the run to success), and a degraded local
        # review stays non-blocking (ADR-0006).
        _maybe_post_salvage(agent, ctx, exc, as_app=as_app, dry_run=dry_run)
        _close_funnel_breadcrumb(
            agent, run_repo, run_id, outcome=outcome, detail=str(exc)
        )
        # Record the breadcrumb, then RE-RAISE so the caller still sees the real
        # review failure (the adapter normalizes it to PrStateError).
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


#: Cap on the salvaged raw text posted to a PR comment. GitHub's review-body limit
#: is 65536 chars; stay well under it to leave room for the marker + code fences.
_SALVAGE_MAX = 60000


def _safe_fence(content: str) -> str:
    """A backtick fence guaranteed to CONTAIN ``content`` — never closed early by it.

    CommonMark ends a fenced code block only on a line whose backtick run is at least
    as long as the opening run, so a fence of ``max_backtick_run + 1`` backticks
    (floor 3, the CommonMark minimum) cannot be closed by anything inside ``content``.
    Untrusted agent output routinely carries ```` ``` ```` fences (the very ```json
    blocks ``extract_json`` tolerates); a FIXED ``` fence would close early, breaking
    the rendering AND — worse — letting the remaining raw render as LIVE GitHub
    markdown (stray headings / mentions / links / checkboxes — an injection surface).
    A delimiter longer than any run in the content fixes both at once: fenced content
    is literal, so nothing inside it can fire.
    """
    longest_run = max((len(m) for m in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest_run + 1)


def _salvage_body(agent: str, raw: str) -> tuple[str, bool]:
    """Build the salvage comment body from the agent's raw output — (body, truncated).

    A clear marker that the STRUCTURED parse failed (so a reader never mistakes the
    raw dump for a normal review), then the raw text in a fenced block. The fence is
    sized by :func:`_safe_fence` to be longer than any backtick run in the raw, so the
    untrusted output is fully CONTAINED — it can't close the fence early and leak as
    live markdown. Truncated to :data:`_SALVAGE_MAX` with an explicit note when the
    output is huge, so the post never trips GitHub's comment-size limit.
    """
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
    agent: str, ctx, exc: BackendError, *, as_app: bool, dry_run: bool
) -> None:
    """Post unparseable-but-non-empty agent output as a top-level review COMMENT (#76).

    When a local agent returns CONTENT but JSON we couldn't parse, the structured
    review is lost — but the prose is still valuable. Rather than drop it, post it as
    ONE top-level comment (a synthetic ``COMMENT`` review: no inline comments, the
    raw text in the body) prefixed with a marker that the structured parse failed.

    The funnel outcome is recorded SEPARATELY by the caller and stays degraded — this
    only preserves the content; it never flips the run to success. Best-effort, like
    the funnel breadcrumb: a genuinely EMPTY stdout (nothing on ``exc.raw``) posts
    nothing, and a post failure here is logged and swallowed so it never masks the
    real ``BackendError`` the caller re-raises. The event is forced to ``COMMENT``
    (there is no parsed status, so this must never APPROVE / REQUEST_CHANGES).
    """
    raw = (getattr(exc, "raw", "") or "").strip()
    if not raw:
        # Genuinely empty — there is nothing to salvage; behave exactly as before.
        return
    body, truncated = _salvage_body(agent, raw)
    review = {
        "summary": {"status": "COMMENT", "overall_feedback": body},
        "comments": [],
    }
    try:
        post.post_review(
            review,
            ctx,
            agent_name=agent,
            event="COMMENT",
            dry_run=dry_run,
            as_app=as_app,
        )
        logger.info(
            "salvaged unparseable %s review for %s#%s as a top-level comment "
            "(%d raw chars%s) — funnel still records the degraded outcome",
            agent,
            ctx.repo,
            ctx.number,
            len(raw),
            ", truncated" if truncated else "",
        )
    except Exception as post_exc:  # noqa: BLE001 - salvage is best-effort, never fatal
        logger.warning(
            "could not post salvage comment for %s#%s (the degraded outcome is "
            "still recorded; the original review error still propagates): %s",
            ctx.repo,
            ctx.number,
            post_exc,
        )


def start_detached_review(
    agent: str,
    pr: int,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    as_app: bool = True,
    spawn: Callable[[Sequence[str], Mapping[str, str]], None] | None = None,
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
    ``spawn`` is the injected detach boundary — called ``(argv, env)``, default
    the exec seam's fire-and-forget :func:`shipit.execrun.spawn_detached` (the
    one deliberate non-Exec, kept in ``execrun`` so all subprocess use stays in
    one module, ADR-0028; ``env`` carries the ADR-0029 cross-process context) —
    and ``find`` the injected reconcile-lookup boundary (default:
    :func:`shipit.review.checkrun.find_nonterminal`) — mirrored injectable
    seams so a test asserts reconcile + detach WITHOUT the network or a fork.

    This is a DETACH SEAM for the domain-key context (ADR-0029): ``pr`` and
    ``repo`` bind here — the parent's own records from this point carry them —
    and the child's environment (:func:`shipit.logcontext.env_export`) carries
    every bound key plus the freshly-opened funnel ``run`` id (the child's
    story, so it is exported without binding in this parent). The child rebinds
    them at its logging setup, so the detached run's records correlate to the
    same ``pr``/``repo``/``run`` with no shared state.
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
    # Bind the seam's domain keys (ADR-0029): from here on the parent's records
    # carry pr/repo, and the export below hands them (plus the run id, which is
    # the CHILD's correlation, not this parent's) across the process boundary.
    logcontext.bind(pr=pr, repo=repo)
    run_id = _open_breadcrumb(agent, repo, head_sha)
    child_env = logcontext.env_export(run=run_id)
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
        (spawn or execrun.spawn_detached)(argv, env=child_env)
    except Exception as exc:  # noqa: BLE001 - any spawn failure must still close the run
        # The spawn is what the child relies on to reach its terminal close. If it
        # fails AFTER the parent opened the in_progress run, no child will ever
        # close that run — it would hang `in_progress` forever. Close it as failed
        # here (only when a run was actually opened), then re-raise so the reviewer
        # adapter still normalizes the request failure to `PrStateError`. (This is only
        # the PARENT-observed spawn failure; the child's own self-resolution
        # catch-all is OBS03-WS03's deliverable, issue #41.)
        if run_id is not None:
            _close_funnel_breadcrumb(
                agent, repo, run_id, outcome="failed", detail=str(exc)
            )
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
    start = time.monotonic()
    logger.info(
        "run_detached_review: agent=%s pr=#%s repo=%s run_id=%s — child start",
        agent,
        pr,
        repo,
        run_id,
        extra={"reviewer": agent, "pr": pr},
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
        # The failure PROPAGATES (re-raised below), so it records at ERROR with
        # the exception attached (glassbox spray) — plus the start→settle
        # duration, since the failed resolve is this run's terminal settle.
        duration_ms = int((time.monotonic() - start) * 1000)
        if run_id is not None:
            _close_funnel_breadcrumb(
                agent, repo, run_id, outcome="failed", detail=str(exc)
            )
            logger.error(
                "run_detached_review: agent=%s pr=#%s resolve failed after %dms — "
                "closed run %s as failed",
                agent,
                pr,
                duration_ms,
                run_id,
                exc_info=True,
                extra={"reviewer": agent, "pr": pr, "duration_ms": duration_ms},
            )
        else:
            logger.error(
                "run_detached_review: agent=%s pr=#%s resolve failed after %dms — "
                "no run to close (parent opened none)",
                agent,
                pr,
                duration_ms,
                exc_info=True,
                extra={"reviewer": agent, "pr": pr, "duration_ms": duration_ms},
            )
        raise
    try:
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
    except Exception:
        # The helper already closed the funnel run to its own terminal state
        # (timed_out / empty / failed) — this records the SETTLE of the child
        # itself: a propagating failure at ERROR with the exception attached and
        # the start→settle duration (glassbox spray).
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "run_detached_review: agent=%s pr=#%s — child failed after %dms",
            agent,
            pr,
            duration_ms,
            exc_info=True,
            extra={"reviewer": agent, "pr": pr, "duration_ms": duration_ms},
        )
        raise
    # The review's start→settle duration (LOG02): child start (moments after the
    # parent's request) to the terminal close `_generate_post_and_close` just made.
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "run_detached_review: agent=%s pr=#%s — child done in %dms",
        agent,
        pr,
        duration_ms,
        extra={"reviewer": agent, "pr": pr, "duration_ms": duration_ms},
    )
    return result


def _resolve_target(pr: int) -> tuple[str, str]:
    """Cheaply resolve ``(repo, head_sha)`` for ``pr`` — the FAST synchronous path.

    Uses the lightweight ``gh repo view`` + ``gh pr view``, then reads the PR core
    through the ONE :func:`shipit.pr.core_from_node` boundary — the SAME extraction
    :func:`resolve_pr` and the readiness path use, so ``head_sha`` is fetched exactly
    one way. It is NOT the full diff resolve — that fetch/merge-base/diff is the
    detached child's work, so the request stays fast. A ``gh``/auth failure
    PROPAGATES (the reviewer adapter normalizes it to ``PrStateError``); the breadcrumb
    create that follows is the only best-effort step.
    """
    repo = gh.current_repo()
    raw = gh.pr_view(str(pr), json_fields=list(CORE_JSON_FIELDS))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"unparseable `gh pr view` output for #{pr}: {exc}") from exc
    # A truthy non-dict (e.g. a JSON list) would fail on the core read; and a
    # missing/empty `headRefOid` would silently degrade the breadcrumb create to an
    # in-flight reply with no target commit. This is the synchronous validation
    # path, so both are real failures — raise `ReviewError` (like the unparseable case
    # above) so the request fails loud instead of degrading, BEFORE building the core.
    if not (isinstance(data, dict) and data.get("headRefOid")):
        raise ReviewError(f"`gh pr view` output for #{pr} has no headRefOid: {raw!r}")
    # Both boundary reads can raise raw, untyped errors on a malformed upstream:
    # `repo_from_slug` raises `ValueError` when `gh.current_repo()` returned an
    # empty/non-`owner/name` slug, and `core_from_node` raises `KeyError`/`ValueError`
    # when the node omits a required core key or carries a non-bool `isDraft`. This
    # is the fast synchronous boundary, so normalize either to `ReviewError` (like the
    # unparseable/headRefOid cases above) — a clear, typed message instead of a raw
    # traceback leaking out of the request path.
    try:
        core = core_from_node(data, repo_from_slug(repo))
    except (ValueError, KeyError) as exc:
        raise ReviewError(
            f"could not resolve target repo/core for #{pr} from `gh` output "
            f"(repo={repo!r}): {exc}"
        ) from exc
    return core.slug, core.head_sha


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


#: Funnel outcome → (check-run ``conclusion``, output ``title``, output
#: ``summary``). The mapping ADR-0005 fixes: a posted review (incl. a clean
#: zero-findings one) is ``success``; a failed run is ``failure``; an EMPTY run
#: (no parseable review — the agy mode) is ``neutral``; a timeout is ``timed_out``.
#:
#: EMPTY takes ADR-0005's blessed ``neutral`` alternative (over ``failure`` + an
#: "empty" output reason) DELIBERATELY: the OBS04 readiness snapshot carries only
#: the check run's ``status`` / ``conclusion`` / ``startedAt`` (not its ``output``
#: text), so a distinct ``conclusion`` is the ONLY way the readiness layer can tell an *empty*
#: non-delivery (degraded, but distinct from a hard ``failure``) apart from a
#: backend ``failure`` WITHOUT the snapshot fetching check-run output. The load-
#: bearing point ADR-0005 makes is unchanged — empty is NOT ``success`` — and both
#: ``neutral`` and ``failure`` settle as degraded + non-blocking at the readiness layer
#: (`shipit.prstate.reviewers._funnel_state_from_check`); the conclusion split only
#: sharpens the human-facing "why" (empty vs failed). The "empty" word stays in the
#: output title/summary for a human reading the run directly.
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

    The create the async parent (:func:`start_detached_review`) opens its
    ``in_progress`` run through, so the "a breadcrumb failure must NEVER fail the
    review" rule lives in ONE place. Any failure (a 403 before
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
    and PATCHes the SAME run :func:`_open_breadcrumb` opened
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
            "_close_funnel_breadcrumb: unknown funnel outcome %r for %s-local "
            "(run id=%s); recording it as 'failed'",
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
            "_close_funnel_breadcrumb: closed funnel check run for %s-local on %s "
            "(run id=%s) -> completed/%s",
            agent,
            repo,
            run_id,
            conclusion,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort; never masks the review outcome
        logger.warning(
            "_close_funnel_breadcrumb: funnel check run transition failed for "
            "%s-local (run id=%s); the review outcome is unaffected: %s",
            agent,
            run_id,
            exc,
        )
