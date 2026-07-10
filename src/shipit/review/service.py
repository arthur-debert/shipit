"""service — the run/post/detach paths for a local review backend.

This is the entry the ``prstate`` reviewer adapters call to GENERATE a review and
POST it. Since OBS03 a local review runs ASYNC: the reviewer adapter's
``request`` no longer blocks on the model run — it detaches a child process and
returns in-flight.

Layered functions:

  * :func:`generate_review` — decide the round SCOPE (RVW02-WS06:
    :func:`shipit.review.rounds.plan_for_view`) then delegate to the FAN-OUT
    (:func:`shipit.review.fanout.run_fanout_review`, RVW02-WS04 / ADR-0045).
    ROUND 1 provisions ONE shared read-only Tree (ADR-0018) on the PR head,
    launches the reviewer's configured **Dimension passes** in parallel (each
    fetching the diff itself via ``gh pr diff``), and unions the results. A
    round AFTER the first — this reviewer already reviewed an earlier head still
    an ancestor of the new head — re-diffs to the FIX RANGE and runs ONE
    incremental pass with new nits suppressed instead (a rebase/force-push falls
    back to a full round). Either way the union is routed (mechanical dedup by
    default, the dormant **Calibrator** when opted on) to the review dict. No
    GitHub posting — the fan-out is invisible below this seam (one review per
    reviewer per head, exactly as before). The generated review is TEED to the
    local review-round record store here (RVW02-WS03, fail-open) — verb-witnessed
    at generate time, before any post, carrying the REAL dispositions, the range
    reviewed, and every contributing run (passes + calibrator) with run ids +
    variant hashes.
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

#: The review path's logger — a child of the package ``shipit`` logger. A local
#: review run (start, the backend/agent invoked, and the outcome) is recorded
#: here at DEBUG/INFO so an async, detached run (OBS03) leaves a durable record.
#: The review text and the diff are deliberately summarised, not dumped.
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
) -> dict:
    """Run ``backend``'s review over ``ctx`` (full round 1, or an incremental
    fix-range round ≥ 2) and return the routed review.

    Round SCOPE is decided here (RVW02-WS06, ADR-0045) via
    :func:`shipit.review.rounds.plan_for_view`: when this reviewer already
    reviewed an earlier head of this PR that is still an ancestor of the new
    head, ``ctx`` is re-diffed to the FIX RANGE
    (:func:`shipit.review.diff.rescoped_view`) and the fan-out runs ONE
    incremental pass with new nits suppressed, instead of the full dimension
    fan-out; a rebase/force-push (the old head is no longer an ancestor) or a
    first round runs the full pipeline. A ``dry_run`` always takes the full
    round-1 path (it touches neither the round store nor git).

    The RVW02-WS04/WS08 round-1 pipeline (ADR-0045): one shared read-only Tree
    (ADR-0018) on the PR head, the reviewer's configured **Dimension passes**
    in parallel through its spawn read-only posture (each fetching the scoped
    diff itself via ``gh pr diff`` — never assuming the base is ``main``), and
    the union posted — by DEFAULT through the mechanical dedup (calibrator off,
    RVW02-WS08), or through the dormant table-level **Calibrator** when a
    reviewer opts it on. The routed result is returned as the same
    REVIEW_SCHEMA-shaped dict the monolithic producer yielded — posting + the
    funnel check-run are the caller's job, unchanged; below this seam the
    fan-out is invisible.

    Delegates to :func:`shipit.review.fanout.run_fanout_review`, which owns the
    Tree, the pass fan-out, the dedup/calibration, and the routing. A missing
    CLI raises :class:`~shipit.review.backends.base.BackendUnavailable`; a review
    that produced nothing usable (every pass failed, or — with the calibrator on
    — a calibrator timeout/unparseable output/contract violation) raises
    :class:`~shipit.review.backends.base.BackendError` /
    ``RuntimeError`` — the same error set as before, so the service's outcome
    mapping is unchanged.

    ``dimensions`` / ``calibrator`` / ``nit_cap`` are the RVW02 config surface
    (per-reviewer Roster option; table-level judge + nit budget) — ``calibrator``
    ``None`` means the judge is OFF (the deduped union); ``dimensions`` /
    ``nit_cap`` ``None`` mean the shipped defaults. ``timeout`` is the PER-PASS
    agent timeout (a
    ``<N>s`` duration string), enforced at the launch seam as a process
    deadline for every backend (#404). ``dry_run`` prints each pass's would-run
    Tree-launch argv and bills nothing.

    Every successfully generated review is ALSO teed to the local
    **review-round record** store at this seam (RVW02-WS03) — verb-witnessed at
    generate time, BEFORE any posting, so the record exists whether or not the
    post succeeds and the posting path is untouched. The tee carries the REAL
    dispositions the routing assigned (from the dedup or the calibrator —
    routed-out findings retained) and every contributing run's id + variant
    hash. It is fail-open
    (:func:`_tee_round_record`): a record miss is logged and swallowed, never a
    degraded review.
    """
    agent = backend.funnel_agent
    # Decide the round SCOPE (RVW02-WS06, ADR-0045): round 1 is the full-PR
    # dimension fan-out; a round after the first — this reviewer already reviewed
    # an earlier head of this PR, still an ancestor of the new head — is ONE
    # incremental pass over the fix range. A rebase/force-push (the old head is no
    # longer an ancestor) falls back to a full round. A dry run never touches the
    # store or git for a plan — it just exercises the round-1 dry-run path.
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
        # Re-diff over the fix range so the fan-out reviews (and the round record
        # records) exactly ``last-reviewed-head..new-head``.
        ctx = diff.rescoped_view(ctx, plan.base)
    logger.info(
        "review run: agent=%s model=%s timeout=%s starting (%s)",
        agent,
        model,
        timeout,
        (
            f"incremental fix-range {ctx.base_sha}..{ctx.head_sha}"
            if plan.incremental
            else "dimension fan-out"
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
            round_id=outcome.round_id or None,
            artifacts_dir=outcome.artifacts_dir,
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
    round_id: str | None = None,
    artifacts_dir: str | None = None,
) -> None:
    """Tee the generated review into the local review-round record store — FAIL-OPEN.

    Verb-witnessed at generate time (RVW02-WS03): the review's product (all
    findings with the Calibrator's dispositions — ``findings``, routed-out
    entries retained, each carrying its originating pass's ``run_id`` — plus
    every contributing run's id + variant hash + artifact bundle path —
    ``runs`` — the round's ``round_id`` / ``artifacts_dir`` bundle location
    (RVW03-WS02), the coverage attestation, and the range reviewed) lands in
    the harness-owned store the moment it exists, independent of the posting
    path — a tee, not a pipeline change. Any failure (a hand-built ctx with no repo,
    an unwritable store, an unreadable instructions file) is logged at WARNING
    and swallowed: process telemetry must never degrade the review it observes
    (the same posture as the eval hook's fail-open contract).
    """
    # A hand-built ctx (tests, ad-hoc callers) may carry no repo identity at all;
    # the tee reads it defensively — fail-open means "no record", never a crash.
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
            round_id=round_id,
            artifacts_dir=artifacts_dir,
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
            backend,
            ctx,
            instructions_path=instructions_path,
            model=model,
            timeout=timeout,
            dimensions=dimensions,
            calibrator=calibrator,
            nit_cap=nit_cap,
            dry_run=dry_run,
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
        _maybe_post_salvage(backend, ctx, exc, as_app=as_app, dry_run=dry_run)
        _close_funnel_breadcrumb(
            backend, run_repo, run_id, outcome=outcome, detail=str(exc)
        )
        # Record the breadcrumb, then RE-RAISE so the caller still sees the real
        # review failure (the adapter normalizes it to PrStateError).
        raise
    except Exception as exc:  # noqa: BLE001 - any other failure is a degraded run
        # The agent errored (missing CLI, crash) or the review POST failed.
        _close_funnel_breadcrumb(
            backend, run_repo, run_id, outcome="failed", detail=str(exc)
        )
        raise
    # Success — incl. a clean zero-findings review: the review POST above already
    # fired unchanged; now close the funnel run to completed/success.
    _close_funnel_breadcrumb(backend, run_repo, run_id, outcome="success")
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


def _salvage_body(agent: str | None, raw: str) -> tuple[str, bool]:
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
    backend: Backend, ctx, exc: BackendError, *, as_app: bool, dry_run: bool
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
    """Open the in_progress funnel run, DETACH the review, report what it did (OBS03).

    The PARENT half of the async inversion: it does ONLY the cheap, synchronous
    work — resolve ``(repo, head_sha)`` via the lightweight ``gh pr view``,
    RECONCILE against any in-flight run, and open the OBS02 ``in_progress``
    breadcrumb (best-effort) — then spawns a DETACHED child (``shipit pr review
    _run``) that runs the model, posts the review, and closes the SAME ``run_id`` to
    its terminal state. It does NOT block on the model run; the review's outcome is
    read LATER from the PR (the funnel check run + the posted review), never from
    this call.

    The return says WHICH path it took, both of which leave the review in-flight:
    ``True`` when it opened + spawned a fresh detached child, ``False`` when it
    RECONCILED against an already in-flight run (no breadcrumb, no spawn). The
    reviewer adapter narrates only a real start as a request transition; a
    reconcile is an idempotent no-op, not a new request edge.

    **Idempotent reconcile (OBS03-WS03, issue #41):** because the check run IS the
    store, a re-request for a reviewer whose funnel run is already non-terminal on
    THIS head must NOT open a second breadcrumb + spawn a second child that
    double-posts. So BEFORE creating + spawning, this reads whether such a run exists
    (:func:`shipit.review.checkrun.find_nonterminal`) and, if so, reconciles —
    reports in-flight and returns ``False`` without creating or spawning. No local /
    daemon state: the check run is the only source of truth (ADR-0005 / #41).

    The breadcrumb create is BEST-EFFORT — a 403 before the ``checks:write``
    re-grant (or any failure) must not fail the request, so the child still runs
    with ``run_id=None`` (no in_progress marker, but the review still posts).
    ONE precondition pierces that rule (#347, #343 gap 6): with ``as_app`` a
    :class:`~shipit.review.ghauth.ReviewAuthError` on the synchronous path (the
    App token could not be minted — PyJWT absent outside the `review` env, missing
    Doppler creds, the App not installed) PROPAGATES instead of being swallowed:
    the detached child needs that SAME auth to post the review and close its run,
    so proceeding would fire a doomed child with NO visible breadcrumb while this
    parent reports a false in-flight — the caller would render
    ``requested review(s): …`` for a request that never happened. The reviewer
    adapter normalizes the propagated error to ``PrStateError`` (clean stderr +
    non-zero exit). The child spawns via ``sys.executable`` — the parent's own
    env — so the parent's auth env IS a faithful precondition for the child's.
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
        "review detach requested for pr#%s (agent=%s) — resolving + detaching",
        pr.number,
        backend.funnel_agent,
        extra={"pr": pr.number},
    )
    # The repo rides in on the PrId (ADR-0030) — the former ambient
    # `gh.current_repo()` resolution is gone; only the head sha needs the wire.
    repo = pr.slug
    head_sha = _resolve_head_sha(pr)
    # Bind the seam's domain keys (ADR-0029) as soon as both are known: from
    # here on the parent's records — including the reconcile lookup's and the
    # breadcrumb's, which only NAME the repo — carry pr/repo, and the export
    # below hands them (plus the run id, which is the CHILD's correlation, not
    # this parent's) across the process boundary.
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
        return False  # reconciled: in-flight, but no new child was started
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
        # The spawn is what the child relies on to reach its terminal close. If it
        # fails AFTER the parent opened the in_progress run, no child will ever
        # close that run — it would hang `in_progress` forever. Close it as failed
        # here (only when a run was actually opened), then re-raise so the reviewer
        # adapter still normalizes the request failure to `PrStateError`. (This is only
        # the PARENT-observed spawn failure; the child's own self-resolution
        # catch-all is OBS03-WS03's deliverable, issue #41.)
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
    return True  # started: a fresh detached child was opened + spawned


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
    agent = backend.funnel_agent
    # The repo rides in on the PrId (ADR-0030): the child entry point minted it
    # from its explicit ``--repo`` argument, so the slug feeds the review-path
    # resolve and the funnel close without a separate parameter.
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
        # The heavy resolve (fetch + merge-base + diff) the request path deliberately
        # skipped is now done — record its shape (NOT the diff text) so the detached
        # run's file-sink record shows what was reviewed.
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
        )
    except Exception:
        # The helper already closed the funnel run to its own terminal state
        # (timed_out / empty / failed) — this records the SETTLE of the child
        # itself: a propagating failure at ERROR with the exception attached and
        # the start→settle duration (glassbox spray).
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
    # The review's start→settle duration (LOG02): child start (moments after the
    # parent's request) to the terminal close `_generate_post_and_close` just made.
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
    """Cheaply resolve the head sha for ``pr`` — the FAST synchronous path.

    Uses the TYPED adapter read (PROC03): ``gh.pr_core()`` returns the
    :class:`~shipit.pr.PR` core, routed through the ONE
    :func:`shipit.pr.core_from_node` boundary — the SAME extraction
    :func:`resolve_pr` and the readiness path use, so ``head_sha`` is fetched exactly
    one way and no JSON is parsed here. The repo needs NO resolving at all: it
    rides in on the :class:`~shipit.pr.PrId` (ADR-0030) — the former ambient
    ``gh.current_repo()`` shellout is gone. It is NOT the full diff resolve — that
    fetch/merge-base/diff is the detached child's work, so the request stays fast.
    A ``gh``/auth failure PROPAGATES (the reviewer adapter normalizes it to
    ``PrStateError``); the breadcrumb create that follows is the only best-effort step.

    The typed read can raise raw, untyped errors on a malformed upstream —
    ``ValueError`` for unparseable output / a malformed head sha /
    non-bool ``isDraft``, ``KeyError`` for a missing required core key. This is the
    fast synchronous boundary, so each is normalized to `ReviewError` — a clear,
    typed message instead of a raw traceback leaking out of the request path.
    """
    try:
        core = gh.pr_core(pr)
    except (ValueError, KeyError) as exc:
        raise ReviewError(
            f"could not resolve target core for #{pr.number} from `gh` output "
            f"(repo={pr.slug!r}): {exc}"
        ) from exc
    # The core carries a typed `Sha` (COR02); this seam hands the wire-facing
    # checkrun helpers (URL path / JSON payload) the string form.
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

    The child reconstructs everything it needs from these arguments + the PR; it
    shares NO state with the parent (no daemon, no job-store file — the PR + check
    run are the only state). Invoked via ``python -m shipit`` so it does not depend
    on the ``shipit`` console-script being on the child's PATH. ``--agent`` carries
    the backend's funnel-agent alias; the child resolves it back to the SAME
    registry identity (:func:`shipit.agent.backend.by_funnel_agent`).

    The RVW02-WS04 config surface rides the same explicit-argument convention
    (never a config re-read in the child): ``--dimensions`` as a comma-joined
    list, ``--nit-cap`` as an int, and the table-level calibrator as its four
    ``--calibrator-*`` fields — each flag omitted when the value is the shipped
    default, so a hand-run child stays as short as before.
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
    backend: Backend,
    repo: str,
    head_sha: str,
    find: Callable[[Backend, str, str], int | None] | None,
    *,
    auth_fatal: bool,
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

    The one exception (#347): with ``auth_fatal`` (the review posts AS the App),
    a :class:`~shipit.review.ghauth.ReviewAuthError` — the App token could not even
    be MINTED (PyJWT absent outside the `review` env, missing Doppler creds, the
    App not installed) — is a PRECONDITION failure, not a degraded read: the child
    needs the SAME auth to post the review, so swallowing it here would detach a
    doomed child and report a false in-flight. It propagates.
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
    """Open the ``in_progress`` funnel check run on ``repo@head_sha`` — BEST-EFFORT.

    The create the async parent (:func:`start_detached_review`) opens its
    ``in_progress`` run through, so the "a breadcrumb failure must NEVER fail the
    review" rule lives in ONE place. Any failure (a 403 before
    the ``checks:write`` re-grant, an auth/``gh`` failure) is logged through the
    OBS01 sink (the failure FACT only — the installation token never reaches a
    record) and swallowed, returning ``None`` so the flow proceeds with no
    breadcrumb. Returns the new run's id otherwise.

    The one exception (#347), mirroring :func:`_reconcile_inflight`: with
    ``auth_fatal`` (the review posts AS the App), a
    :class:`~shipit.review.ghauth.ReviewAuthError` — the App token could not even
    be minted — dooms the child's post too, so it is a precondition failure of
    the whole request and propagates rather than degrading to "no breadcrumb".
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
        # Record the failure fact (never the token) and proceed — the review post
        # is unaffected by a missing/denied check-runs scope.
        logger.warning(
            "funnel check run create failed for %s (continuing to post the review)",
            backend.check_run_name,
            exc_info=True,
        )
        return None


def _close_funnel_breadcrumb(
    backend: Backend, repo, run_id, *, outcome: str, detail: str | None = None
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
