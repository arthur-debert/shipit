"""producer — the Tree-fetch review producer (ADR-0020 §Reviewer-path, REPLACE).

This is the new producer that feeds the EXISTING funnel gate. The maintainer
ratified REPLACE outright (ADR-0020): the front-loaded ``codex`` / ``agy`` review
backends (which pasted a pre-computed diff into the prompt and ran the CLI in the
consumer's checkout) are retired in favour of a reviewer that runs in a **shared
read-only Tree** (ADR-0018) at the PR's true head and **fetches the scoped diff
itself** with ``gh pr diff``. shipit then CAPTURES the agent's structured stdout
and the service posts it via the existing App-identity ``post`` path onto the
existing ``review: <agent>-local`` check-run — so readiness/posting/identity/config
are all preserved; only the producer changed.

What this module owns (and ONLY this):

  * map the funnel :class:`~shipit.agent.backend.Backend` identity (``CODEX`` /
    ``ANTIGRAVITY``, ADR-0025) to its spawn ``BackendAdapter`` — keyed by the
    registry value objects themselves, never a retyped agent-name string — ONE
    definition of "launch codex/agy as a reviewer", shared with the spawn
    surface (the WS04a read-only posture);
  * provision the shared read-only Tree on the PR head (reusing
    :func:`shipit.tree.readonly.create_readonly` — a second reviewer on the same
    ``(repo, branch)`` reuses the clone);
  * build the Tree-fetch reviewer task (:func:`shipit.review.prompt.build_reviewer_task`)
    and, for codex, write the JSON schema temp file so codex enforces the output
    shape natively (``--output-schema`` — the robustness win ADR-0020 keeps);
  * launch the child rooted in the Tree (shared :func:`shipit.spawn.launch.launch`,
    stdin ``/dev/null``, auth-env scrubbed) under the reviewer's ``--timeout`` as a
    real process DEADLINE (#404) — a review is a bounded, non-blocking degrade
    (ADR-0006), so a stalled backend is KILLED at the seam and settled ``timed_out``,
    never waited on forever — then CAPTURE its stdout and parse it into a review dict
    (:func:`shipit.review.backends.parse_review_output`), wrapped in a
    :class:`CapturedReview` that also carries the launch's MEASURED token usage
    (:mod:`shipit.review.usage` — parsed per-backend from the CLI's own output,
    RVW03-WS04) and the ReasoningLevel the adapter ACTUALLY wired into argv
    (``None`` when the backend has no knob — records stamp from this, never from
    config).

A second producer shares the same launch core (RVW02-WS03):
:func:`run_range_review`, the OFFLINE commit-range sibling — no Tree, no ``gh``,
the diff read via ``git diff <base>..<head>`` in the caller's checkout — which
feeds the no-post replay path (:mod:`shipit.review.replay`) instead of the funnel.

The RVW02-WS04 dimension fan-out (:mod:`shipit.review.fanout`) drives
:func:`run_tree_review` too — once per configured **Dimension pass**
(``dimension=…``) against ONE shared Tree it provisions up front
(:func:`provision_review_tree`, so N parallel passes never race N refreshes) —
and hashes each pass's exact prompt via :func:`pass_task_text` for the
review-round record's per-run **Variant**. The offline fan-out replay
(RVW03-WS01) drives :func:`run_range_review` the same way — once per pass with
the same ``dimension=`` narrowing, :func:`range_pass_task_text` as its variant
source — so the live and replay arms differ only in how the diff is fetched.

A schema-unenforced backend (agy) whose stdout is UNPARSEABLE is re-prompted ONCE
with the specific parse failure appended before the producer gives up (#826, the
per-backend ``retry_on_parse_failure`` opt-in) — the deterministic net even when
the agent skipped its best-effort self-check. codex (native ``--output-schema``)
and any TIMEOUT are never retried. Only when the single retry ALSO fails does the
:class:`BackendError` surface, so the service's #76 salvage stays the FINAL backstop.

It does NOT post, does NOT touch the check-run, and does NOT decide outcomes — the
service layer (:mod:`shipit.review.service`) owns posting + the funnel breadcrumb,
exactly as before. The producer raises :class:`BackendUnavailable` (missing CLI),
:class:`BackendError` (unparseable / timed-out output — carrying the raw for the #76
salvage), or a plain error (a nonzero child / a Tree precondition failure) which the
service maps to the ``failed`` funnel outcome.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass

from .. import execrun, gh, git, workenv
from ..agent.backend import ANTIGRAVITY, CODEX, Backend
from ..identity import Repo, Sha, repo_from_slug
from ..spawn import launch
from ..spawn.backends.antigravity import AntigravityAdapter
from ..spawn.backends.base import BackendAdapter
from ..spawn.backends.codex import CodexAdapter
from ..tree.cleanup import parse_duration
from ..tree.readonly import create_readonly, readonly_plan
from .artifacts import RunArtifacts
from .backends import BackendError, BackendUnavailable, parse_review_output
from .backends.base import _TIMEOUT_MARKER
from .dimensions import Dimension
from .instructions import load_instructions
from .prompt import (
    build_incremental_reviewer_task,
    build_range_reviewer_task,
    build_reviewer_task,
)
from .schema import REVIEW_SCHEMA
from .usage import UNREPORTED, TokenUsage, from_codex_stderr

logger = logging.getLogger("shipit.review")

#: The reviewer role the spawn adapter's read-only posture is built for. It anchors
#: the role preamble codex / agy prepend to the task; the funnel result channel is
#: shipit's capture-and-post, not agent self-posting, so the task says NOT to post.
_REVIEWER_ROLE = "reviewer"


@dataclass(frozen=True)
class CapturedReview:
    """One reviewer launch's full capture (RVW03-WS04): the parsed review PLUS
    the launch's measurements.

    ``review`` is the REVIEW_SCHEMA-shaped dict downstream consumes exactly as
    before. ``usage`` is the launch's token cost as the backend's CLI actually
    reported it (:mod:`shipit.review.usage`; explicitly :data:`~shipit.review.usage.UNREPORTED`
    for a CLI that reports none — never a fabricated number). ``reasoning`` is
    the ReasoningLevel the adapter ACTUALLY wired into the launched argv
    (``adapter.reasoning``): ``None`` means no knob was applied — either unset,
    or the backend has none — so a record stamped from it never echoes a config
    value that did not run (#685).
    """

    review: dict
    usage: TokenUsage
    reasoning: str | None


@dataclass(frozen=True)
class _BackendSpec:
    """How one funnel backend maps onto the shared spawn launch seam.

    ``adapter_factory`` builds the spawn ``BackendAdapter`` carrying the model (and,
    for agy, the timeout; and, where the CLI has a knob, the reasoning level —
    RVW03-WS04) — the SAME adapter the spawn surface launches a reviewer
    through, so there is one definition of the launch. The CLI binary that must be
    on PATH (preflight) is NOT here — it is the :class:`Backend` identity's
    ``binary`` alias (ADR-0025), read off the registry entry. ``schema_inline``
    describes the schema in prose in the prompt for a backend with no native
    ``--output-schema`` (agy); ``native_schema`` backends (codex) get the schema as
    a temp file passed to ``build_command``.

    ``native_timeout`` says whether the backend enforces the ``--timeout`` itself
    (agy's ``--print-timeout``) or relies SOLELY on the launch-seam deadline (codex,
    which has no per-run timeout flag). It steers :func:`_seam_deadline`: a
    native-timeout backend gets seam HEADROOM over its own flag so its native
    (salvageable-output) path wins the race and the seam is a pure backstop; a
    backend without one is killed by the seam at exactly the configured deadline
    (#404).

    ``usage_parser`` extracts the launch's token usage from the finished
    :class:`~shipit.spawn.launch.LaunchResult` — per-backend, because WHERE a CLI
    reports usage is backend-private (codex: a stderr log line; agy: nowhere —
    explicitly :data:`~shipit.review.usage.UNREPORTED`). Probed facts, documented
    in :mod:`shipit.review.usage`.

    ``retry_on_parse_failure`` opts the backend into the deterministic ONE-shot
    re-prompt net (#826): when a launch's stdout is unparseable, re-prompt the
    backend ONCE with the specific parse failure appended, then re-parse. On ONLY
    for a backend with no native schema enforcement (agy) — codex's
    ``--output-schema`` makes an off-shape response impossible, so it never needs
    the retry. It does NOT fire on a TIMEOUT (a timeout would just burn a second
    full deadline; re-prompting fixes a promptly-returned-but-off-shape body, not
    a slow one) — the salvage stays the backstop there.
    """

    schema_inline: bool
    native_schema: bool
    native_timeout: bool
    adapter_factory: object  # Callable[[str, str, str | None], BackendAdapter]
    usage_parser: object  # Callable[[launch.LaunchResult], TokenUsage]
    retry_on_parse_failure: bool


def _codex_adapter(model: str, timeout: str, reasoning: str | None) -> BackendAdapter:
    # codex has no per-run timeout flag, so the deadline is NOT threaded into the
    # adapter (only the model is) — it is enforced at the launch SEAM instead, where
    # `run_tree_review` passes it to `launch.launch(timeout=...)` as a hard process
    # deadline (#404). `native_timeout=False` in the spec records that the seam is
    # codex's SOLE timeout enforcement. `reasoning` IS threaded (RVW03-WS04): codex
    # carries the one probed reasoning knob (`-c model_reasoning_effort=<level>`).
    del timeout
    return CodexAdapter(model=model, reasoning=reasoning)


def _agy_adapter(model: str, timeout: str, reasoning: str | None) -> BackendAdapter:
    # agy has NO reasoning knob (probed 1.1.1) — the requested level is DROPPED
    # here, deliberately: the adapter's `reasoning` stays None, so the run record
    # stamps "unset" instead of echoing a config value that never reached the CLI.
    del reasoning
    return AntigravityAdapter(model=model, timeout=timeout)


def _codex_usage(result: launch.LaunchResult) -> TokenUsage:
    """codex reports its token total on STDERR (probed 0.139.0, RVW03-WS04)."""
    return from_codex_stderr(result.stderr or "")


def _agy_usage(result: launch.LaunchResult) -> TokenUsage:
    """agy reports NO usage anywhere (probed 1.1.1) — explicitly unknown."""
    del result
    return UNREPORTED


#: :class:`Backend` identity → how it launches as a reviewer. ``CODEX`` ≡ the ``codex``
#: spawn adapter (native ``--output-schema``); ``ANTIGRAVITY`` ≡ the ``antigravity``
#: spawn adapter (no native schema → prose schema in the prompt). Keyed by the
#: registry :class:`Backend` VALUE OBJECTS themselves — not a retyped canonical-name
#: string — so the funnel and launch axes meet on the ONE registry identity (ADR-0025)
#: and renaming a backend is a single registry edit (the key follows the constant's
#: identity, which is its canonical name). This is the single place the funnel backends
#: are mapped onto the spawn seam.
_SPECS: dict[Backend, _BackendSpec] = {
    CODEX: _BackendSpec(
        schema_inline=False,
        native_schema=True,
        native_timeout=False,
        adapter_factory=_codex_adapter,
        usage_parser=_codex_usage,
        # codex's `--output-schema` enforces the shape natively, so an off-shape
        # response can't happen — no re-prompt net needed.
        retry_on_parse_failure=False,
    ),
    ANTIGRAVITY: _BackendSpec(
        schema_inline=True,
        native_schema=False,
        native_timeout=True,
        adapter_factory=_agy_adapter,
        usage_parser=_agy_usage,
        # agy has no native schema enforcement, so a promptly-returned but
        # unparseable body is the #76 failure mode — re-prompt it ONCE with the
        # parse failure before the salvage takes over (#826).
        retry_on_parse_failure=True,
    ),
}


#: Headroom (seconds) the launch-seam deadline adds OVER a backend's OWN native
#: timeout flag (agy's ``--print-timeout``). agy's native timeout produces a
#: truncated-but-SALVAGEABLE review (a partial JSON body + the timeout marker in
#: stdout, #76); the seam deadline is a SIGKILL that loses that output. So a
#: native-timeout backend's seam deadline is set past its own flag by this margin —
#: comfortably more than agy's sub-second teardown, negligible against a 600s base —
#: so its native path always fires first and the seam only bites if agy hangs past
#: its OWN deadline. A backend with no native flag (codex) gets NO headroom: the seam
#: IS its enforcement, at exactly the configured ``--timeout``.
_SEAM_HEADROOM_SECONDS = 60.0


def _seam_deadline(timeout: str, spec: _BackendSpec) -> float:
    """The launch-seam process deadline (seconds) for a reviewer launch (#404).

    Parses the ``<N>s`` ``timeout`` string (the canonical roster shape) into seconds
    and, for a backend that carries its OWN native timeout flag
    (``spec.native_timeout``), adds :data:`_SEAM_HEADROOM_SECONDS` so the native
    (salvageable-output) path wins the race and the seam is a pure backstop. A backend
    without a native flag (codex) is killed by the seam at exactly the configured
    deadline. A malformed ``timeout`` raises ``ValueError`` from
    :func:`shipit.tree.cleanup.parse_duration` — a loud failure the service maps to a
    ``failed`` outcome, never a silent unbounded run.
    """
    base = parse_duration(timeout)
    return base + _SEAM_HEADROOM_SECONDS if spec.native_timeout else base


def pass_task_text(
    backend: Backend,
    pr_number: int,
    *,
    instructions_path: str | None = None,
    dimension: Dimension | None = None,
    incremental_range: tuple[str, str] | None = None,
) -> str:
    """The EXACT reviewer task text a :func:`run_tree_review` launch composes —
    the fan-out's **Variant** source (RVW02-WS04).

    The round record hashes each contributing run's prompt
    (:func:`shipit.harness.eval.variant.variant_of`) so a review-prompt A/B
    separates arms on content; this helper re-derives the same bytes
    :func:`run_tree_review` will launch with (instructions + PR number + the
    backend's schema presentation + the optional dimension slice) without
    launching anything. Raises ``ValueError`` for a non-funnel backend, exactly
    like the launch path.

    ``incremental_range`` (RVW02-WS06) selects the INCREMENTAL fix-range task
    (:func:`~shipit.review.prompt.build_incremental_reviewer_task`) over
    ``(base_sha, head_sha)`` instead of the full-PR task — so the incremental
    round's single pass hashes the same bytes it launches with. ``incremental_range``
    is mutually exclusive with ``dimension`` (round ≥ 2 is ONE full-scope pass, not
    a dimension fan-out); passing both is a caller error this helper rejects with
    ``ValueError`` — exactly like the launch path — so misuse fails loudly instead
    of silently hashing the wrong task shape.
    """
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    if incremental_range is not None and dimension is not None:
        raise ValueError(
            "pass_task_text: incremental_range and dimension are mutually "
            "exclusive — an incremental round is ONE full-scope fix-range pass, "
            "not a dimension pass"
        )
    if incremental_range is not None:
        base_sha, head_sha = incremental_range
        return build_incremental_reviewer_task(
            load_instructions(instructions_path),
            pr_number,
            base_sha,
            head_sha,
            schema_inline=spec.schema_inline,
        )
    return build_reviewer_task(
        load_instructions(instructions_path),
        pr_number,
        schema_inline=spec.schema_inline,
        dimension=dimension,
    )


def range_pass_task_text(
    backend: Backend,
    view,
    *,
    instructions_path: str | None = None,
    dimension: Dimension | None = None,
) -> str:
    """The EXACT reviewer task text a :func:`run_range_review` launch composes —
    the offline fan-out replay's **Variant** source (RVW03-WS01).

    The range sibling of :func:`pass_task_text`: re-derives the same bytes
    :func:`run_range_review` will launch with (instructions + the resolved
    range + the backend's schema presentation + the optional dimension slice)
    without launching anything, so a replayed pass's ``round.runs`` variant
    hash is honest exactly like a live pass's. ``view`` is the resolved
    :class:`~shipit.review.diff.RangeView`. Raises ``ValueError`` for a
    non-funnel backend, exactly like the launch path.
    """
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    return build_range_reviewer_task(
        load_instructions(instructions_path),
        str(view.base_sha),
        str(view.head_sha),
        schema_inline=spec.schema_inline,
        dimension=dimension,
    )


def provision_review_tree(ctx) -> str:
    """Provision (or reuse) the shared read-only Tree on ``ctx``'s PR head and
    return its path.

    The one Tree resolution the review producers share: resolve the repo
    identity + head branch, then :func:`shipit.tree.readonly.create_readonly`
    (a second caller on the same ``(repo, branch)`` reuses the clone). The
    RVW02-WS04 fan-out calls this ONCE before launching its parallel dimension
    passes so the N passes share one provisioning instead of racing N
    refreshes; :func:`run_tree_review` provisions through here too when no
    ``tree_path`` was handed in. Raises ``RuntimeError`` when the head branch
    is unknown (no Tree can be provisioned).
    """
    repo = _resolve_repo(ctx)
    branch = (ctx.head_ref or "").strip()
    if not branch:
        raise RuntimeError(
            f"cannot review PR #{ctx.number}: its head branch (headRefName) is "
            "unknown, so the shared read-only Tree cannot be provisioned."
        )
    tree = create_readonly(
        readonly_plan(repo=repo, branch=branch),
        source_repo=ctx.workdir,
        github_url=_github_url(ctx),
    )
    return tree.path


def run_tree_review(
    backend: Backend,
    ctx,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dry_run: bool = False,
    launcher: launch.Runner | None = None,
    dimension: Dimension | None = None,
    tree_path: str | None = None,
    incremental_range: tuple[str, str] | None = None,
    reasoning: str | None = None,
    run_id: str | None = None,
    artifacts: RunArtifacts | None = None,
) -> CapturedReview:
    """Launch ``backend`` as a reviewer in a read-only Tree and CAPTURE its review.

    Provisions the shared read-only Tree on ``ctx``'s PR head, launches the backend
    through its spawn read-only posture with a task that fetches the diff via
    ``gh pr diff`` and emits structured JSON, captures stdout, and parses it. Returns
    a :class:`CapturedReview` — the review dict plus the launch's measured token
    usage and the reasoning level ACTUALLY applied to argv (RVW03-WS04); it does NOT
    post and does NOT touch the check-run (the service
    owns those). Raises :class:`BackendUnavailable` (missing CLI), :class:`BackendError`
    (unparseable / timed-out output, carrying the raw for salvage), or a plain
    ``RuntimeError`` (a nonzero child / a missing PR head branch) → the service maps it
    to ``failed``.

    ``reasoning`` requests a ReasoningLevel for the launch (RVW03-WS04, #685). It
    reaches real argv ONLY where the backend's CLI has a knob (codex
    ``-c model_reasoning_effort=<level>``); a backend without one (agy) drops it,
    and the returned ``CapturedReview.reasoning`` reports what was actually
    applied — the value records must stamp, never the request.

    ``dimension`` narrows the task to ONE **Dimension pass** (RVW02-WS04 — the
    fan-out launches this once per configured dimension); ``None`` keeps the
    full-scope task. ``tree_path`` hands in an ALREADY-provisioned Tree (the
    fan-out provisions once via :func:`provision_review_tree` and shares it
    across its parallel passes); ``None`` provisions here, exactly as before.

    ``incremental_range`` (RVW02-WS06) selects the INCREMENTAL fix-range task
    (:func:`~shipit.review.prompt.build_incremental_reviewer_task`) over
    ``(base_sha, head_sha)`` — the reviewer reads only ``git diff base..head``
    plus the dependency neighborhood, not the full ``gh pr diff``. It is
    mutually exclusive with ``dimension`` (round ≥ 2 is ONE full-scope pass, not
    a fan-out) — passing both raises ``ValueError`` so a misrouted call fails
    loudly rather than silently running the wrong task shape; the fan-out never
    combines them. ``None`` keeps the full-PR task, exactly as before.

    ``run_id`` / ``artifacts`` are the RVW03-WS02 observability seam: ``run_id``
    is the fan-out-minted pass id, stamped (with the dimension) onto this
    launch's log records so parallel passes are separable in the log sink;
    ``artifacts`` is the pass's :class:`~shipit.review.artifacts.RunArtifacts`
    bundle, which the launch core fills with the exact prompt, the raw streams,
    and the launch meta — success and failure alike, fail-open. ``None`` for
    either keeps the pre-WS02 behaviour (no correlation extras, no bundle).

    With ``dry_run=True``: resolves the Tree COORDINATES (no clone, no model bill),
    prints the would-run Tree-launch argv, and returns an empty capture — so a dry-run
    is honest (it shows exactly what would run and bills nothing).
    """
    agent = backend.funnel_agent or backend.name
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    if incremental_range is not None and dimension is not None:
        raise ValueError(
            "run_tree_review: incremental_range and dimension are mutually "
            "exclusive — an incremental round is ONE full-scope fix-range pass, "
            "not a dimension pass"
        )
    _preflight(backend, model=model, dry_run=dry_run)

    instructions = load_instructions(instructions_path)
    if incremental_range is not None:
        base_sha, head_sha = incremental_range
        task = build_incremental_reviewer_task(
            instructions,
            ctx.number,
            base_sha,
            head_sha,
            schema_inline=spec.schema_inline,
        )
    else:
        task = build_reviewer_task(
            instructions,
            ctx.number,
            schema_inline=spec.schema_inline,
            dimension=dimension,
        )
    adapter = spec.adapter_factory(model, timeout, reasoning)  # type: ignore[operator]
    repo = _resolve_repo(ctx)
    branch = (ctx.head_ref or "").strip()
    if not branch:
        raise RuntimeError(
            f"cannot review PR #{ctx.number}: its head branch "
            "(headRefName) is unknown, so the shared read-only Tree "
            "cannot be provisioned."
        )

    schema_path: str | None = None
    try:
        if dry_run:
            return _dry_run(agent, ctx, spec, adapter, task, repo, branch)

        if spec.native_schema:
            schema_path = _write_schema_tempfile()

        cwd = tree_path if tree_path is not None else provision_review_tree(ctx)
        head = getattr(ctx, "head_sha", None)
        commit = head if isinstance(head, Sha) else None
        review_env = workenv.resolve_readonly_review_env(
            repo=repo,
            tree_path=cwd,
            branch=branch,
            commit=commit,
        )
        correlation = {} if run_id is None else {"run_id": run_id}
        if dimension is not None:
            correlation["dimension"] = dimension.name
        logger.info(
            "review work env resolved — %s routing for read-only reviewer tree",
            review_env.routing.value,
            extra=workenv.resolution_record(
                review_env,
                boundary="review.readonly-run",
                role=_REVIEWER_ROLE,
                extra={"pr": ctx.number, "reviewer": agent, **correlation},
            ),
        )
        logger.info(
            "review launching for pr#%s (agent=%s%s) in read-only Tree %s",
            ctx.number,
            agent,
            f", dimension={dimension.name}" if dimension is not None else "",
            cwd,
            extra={"pr": ctx.number, "tree": cwd, "reviewer": agent, **correlation},
        )
        return _launch_and_capture(
            agent,
            spec,
            adapter,
            task,
            cwd=cwd,
            timeout=timeout,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
        )
    finally:
        if schema_path and os.path.exists(schema_path):
            os.remove(schema_path)


def run_range_review(
    backend: Backend,
    view,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    launcher: launch.Runner | None = None,
    reasoning: str | None = None,
    dimension: Dimension | None = None,
    run_id: str | None = None,
    artifacts: RunArtifacts | None = None,
) -> CapturedReview:
    """Launch ``backend`` as an OFFLINE commit-range reviewer and CAPTURE its
    review (RVW02-WS03 replay) — a :class:`CapturedReview`, exactly like
    :func:`run_tree_review` (usage + applied reasoning ride along, RVW03-WS04).

    The range sibling of :func:`run_tree_review` — the SAME backend specs,
    preflight, adapters, schema handling, launch seam, deadline mapping, and
    capture (:func:`_launch_and_capture`), with two deliberate differences:

      * NO Tree and NO ``gh``: the review runs in ``view.workdir`` (the checkout
        whose range is being replayed) with a task that reads the diff itself via
        ``git diff <base>..<head>`` (:func:`~shipit.review.prompt.build_range_reviewer_task`)
        — the replay boundary already resolved + validated both endpoints;
      * nothing downstream posts: the caller (:mod:`shipit.review.replay`) writes
        the review-round record and stops — no PR is touched.

    ``dimension`` narrows the task to ONE **Dimension pass** exactly as on
    :func:`run_tree_review` (RVW03-WS01: the offline fan-out replay launches
    this once per configured dimension); ``None`` keeps the full-scope task.
    ``run_id`` / ``artifacts`` are the same RVW03-WS02 observability seam as on
    :func:`run_tree_review`: the caller-minted run id lands on this launch's
    log records, and the :class:`~shipit.review.artifacts.RunArtifacts` bundle
    captures the exact prompt + raw streams + launch meta, fail-open.

    Raises exactly the :func:`run_tree_review` error set (missing CLI →
    :class:`BackendUnavailable`; unparseable / timed-out output →
    :class:`BackendError`; a nonzero child → ``RuntimeError``).
    """
    agent = backend.funnel_agent or backend.name
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    _preflight(backend, model=model, dry_run=False)

    instructions = load_instructions(instructions_path)
    task = build_range_reviewer_task(
        instructions,
        str(view.base_sha),
        str(view.head_sha),
        schema_inline=spec.schema_inline,
        dimension=dimension,
    )
    adapter = spec.adapter_factory(model, timeout, reasoning)  # type: ignore[operator]

    schema_path: str | None = None
    try:
        if spec.native_schema:
            schema_path = _write_schema_tempfile()
        logger.info(
            "range review launching (agent=%s) in %s over %s..%s",
            agent,
            view.workdir,
            view.base_sha,
            view.head_sha,
            extra={
                "reviewer": agent,
                **({} if run_id is None else {"run_id": run_id}),
            },
        )
        return _launch_and_capture(
            agent,
            spec,
            adapter,
            task,
            cwd=str(view.workdir),
            timeout=timeout,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
        )
    finally:
        if schema_path and os.path.exists(schema_path):
            os.remove(schema_path)


def _launch_and_capture(
    agent: str,
    spec: _BackendSpec,
    adapter: BackendAdapter,
    task: str,
    *,
    cwd: str,
    timeout: str,
    schema_path: str | None,
    launcher: launch.Runner | None,
    artifacts: RunArtifacts | None = None,
    run_id: str | None = None,
) -> CapturedReview:
    """Launch a reviewer child, capture its review, and — for a schema-unenforced
    backend — apply the deterministic ONE-shot re-prompt net (#826).

    The launch core :func:`run_tree_review` (PR/Tree) and :func:`run_range_review`
    (offline range) share: it delegates a single launch+parse to :func:`_attempt`,
    and when that raises a PARSE-failure :class:`BackendError` on a backend opted
    into the retry (``spec.retry_on_parse_failure`` — agy, no native schema), it
    re-prompts ONCE with the specific parse failure appended (:func:`_retry_task`)
    and re-parses. The retry is the deterministic fix even when the agent skipped
    its best-effort self-check; codex (native ``--output-schema``) never retries.

    A TIMEOUT is NEVER retried (``exc.timed_out``): re-prompting a slow run would
    just burn a second full deadline, and a timeout is not an off-shape body a
    re-prompt corrects. When the retry ALSO fails (or the backend does not opt in),
    the :class:`BackendError` propagates unchanged so the service's #76 salvage
    stays the FINAL backstop — the retry slots strictly BEFORE it.

    ``run_id`` / ``artifacts`` are threaded to :func:`_attempt`; the bundle
    reflects the LAST attempt (a retry overwrites the first attempt's streams +
    meta), so the artifact of record is the output the outcome is settled from.
    """
    try:
        return _attempt(
            agent,
            spec,
            adapter,
            task,
            cwd=cwd,
            timeout=timeout,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
        )
    except BackendError as exc:
        # The retry net (#826) fires ONLY for a schema-unenforced backend (agy) and
        # ONLY on a parse failure, never a timeout — a second run of a slow launch
        # would just re-burn the deadline. On any other BackendError, propagate to
        # the service's salvage backstop unchanged.
        if not spec.retry_on_parse_failure or exc.timed_out:
            raise
        logger.info(
            "%s review output was unparseable — re-prompting ONCE with the parse "
            "failure before falling through to salvage (retry net, #826)",
            agent,
            extra={
                "reviewer": agent,
                **({} if run_id is None else {"run_id": run_id}),
            },
        )
        return _attempt(
            agent,
            spec,
            adapter,
            _retry_task(task, exc),
            cwd=cwd,
            timeout=timeout,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
        )


def _retry_task(task: str, failure: BackendError) -> str:
    """Compose the ONE agy re-prompt: the original ``task`` plus the exact failure.

    Appends a terminal RETRY block quoting why the previous output could not be
    parsed — the :class:`BackendError` message already carries the actionable hint
    plus a head/tail snippet of the raw output — so agy fixes the concrete problem
    rather than re-guessing the shape blind. The schema is already in ``task`` (it
    is the agy prompt), so it is not restated; this composes the single retry and
    nothing more — the caller (:func:`_launch_and_capture`) never loops.
    """
    return (
        f"{task}\n\n"
        "RETRY — your PREVIOUS response could NOT be parsed as a valid review:\n"
        f"{failure}\n"
        "Emit a SINGLE, complete, valid JSON object that matches the schema above "
        "and NOTHING else — fix the exact problem reported, and do not add prose "
        "or markdown fences."
    )


def _attempt(
    agent: str,
    spec: _BackendSpec,
    adapter: BackendAdapter,
    task: str,
    *,
    cwd: str,
    timeout: str,
    schema_path: str | None,
    launcher: launch.Runner | None,
    artifacts: RunArtifacts | None = None,
    run_id: str | None = None,
) -> CapturedReview:
    """Launch ONE reviewer child in ``cwd`` under the seam deadline and parse its
    stdout — one attempt, no retry (the retry lives in :func:`_launch_and_capture`).

    The deadline mapping and the timeout→``BackendError`` normalization exist
    exactly once here. The returned :class:`CapturedReview` carries the launch's
    measured usage (``spec.usage_parser`` over the raw
    :class:`~shipit.spawn.launch.LaunchResult`) and the adapter's APPLIED reasoning
    level (RVW03-WS04).

    ``run_id`` is the pass's correlation id — threaded onto the local breadcrumb
    WARNING (the line pointing at the bundle path) so ``shipit logs --run`` /
    ``--reviewer`` can select the very record that says where the raw output lives.

    ``artifacts`` (RVW03-WS02) is the run's fail-open bundle: the EXACT prompt
    is written BEFORE the launch (a hung/killed child still leaves it
    inspectable), the raw streams + launch meta (argv, exit code, duration,
    timed-out flag) after — on the success, timeout, and nonzero-exit paths
    alike, so the full raw output is always on disk even where the raised
    error's message truncates. ``None`` disables the bundle (pre-WS02 callers).
    """
    sink = artifacts if artifacts is not None else RunArtifacts.disabled()
    cmd = adapter.build_command(
        task,
        _REVIEWER_ROLE,
        read_only=True,
        cwd=cwd,
        output_schema_path=schema_path,
    )
    sink.write_prompt(task)
    sink.record(argv=list(cmd), cwd=cwd, seam_deadline_s=_seam_deadline(timeout, spec))
    start = time.monotonic()
    try:
        result = launch.launch(
            cmd,
            cwd=cwd,
            env=adapter.child_env(),
            timeout=_seam_deadline(timeout, spec),
            runner=launcher,
        )
    except execrun.ExecError as exc:
        timed_out = exc.cause == execrun.CAUSE_TIMEOUT
        sink.write_streams(exc.stdout, exc.stderr)
        sink.record(
            duration_ms=int((time.monotonic() - start) * 1000),
            exit_code=None,
            timed_out=timed_out,
            error=str(exc),
        )
        if not timed_out:
            # A non-timeout transport failure (missing binary, bad cwd): leave it
            # for the service's generic mapping to `failed` (ADR-0028 normalizes
            # every OS-level launch error into ExecError; a nonzero CHILD is a
            # LaunchResult, never raised, so this is always transport).
            raise
        # The seam killed a STALLED backend at the deadline (#404). Turn it into
        # the funnel's `timed_out` terminal outcome: BackendError(timed_out=True)
        # so the service settles `timed_out` (degraded, non-blocking, ADR-0006),
        # carrying the partial stdout+stderr as `raw` so the #76 salvage can still
        # surface whatever the backend had written before it hung.
        raise BackendError(
            f"{agent} timed out before returning a review — the launch seam "
            f"killed it at {_seam_deadline(timeout, spec):.0f}s "
            f"(configured --timeout {timeout}); try a faster model or a smaller "
            "diff",
            raw=f"{exc.stdout}\n{exc.stderr}".strip(),
            timed_out=True,
        ) from exc
    sink.write_streams(result.stdout, result.stderr)
    sink.record(
        duration_ms=int((time.monotonic() - start) * 1000),
        exit_code=result.returncode,
        timed_out=False,
    )
    try:
        review = _capture(agent, result, artifacts=sink, run_id=run_id)
    except BackendError as exc:
        # An exit-0 launch can STILL be a timeout: `_capture` re-parses the
        # stdout and `parse_review_output` raises `BackendError(timed_out=True)`
        # when otherwise-unparseable output carries the marker. Correct the
        # optimistic `timed_out=False` just recorded so `meta.json` agrees with
        # the `timed_out` outcome the fanout/service will settle, before the
        # failure propagates — the bundle must never claim a timeout was a clean
        # exit.
        sink.record(timed_out=exc.timed_out)
        raise
    # RVW03-WS04: wrap the captured review with the launch's measured token usage
    # and the reasoning level the adapter actually applied to argv.
    return CapturedReview(
        review=review,
        usage=spec.usage_parser(result),  # type: ignore[operator]
        reasoning=adapter.reasoning,
    )


def _capture(
    agent: str,
    result: launch.LaunchResult,
    *,
    artifacts: RunArtifacts | None = None,
    run_id: str | None = None,
) -> dict:
    """Turn the launched reviewer's result into a review dict, or raise.

    A nonzero exit is a hard failure (mirroring the retired ``proc.run(check=True)``
    backends) — UNLESS the agy print-timeout marker is present, which is a TIMEOUT, not
    a generic failure (so it settles ``timed_out``, not ``failed``). On exit 0 the raw
    stdout is parsed; an unparseable / marker-bearing parse raises :class:`BackendError`
    (carrying the raw for the #76 salvage), exactly as before.

    The nonzero-exit ``RuntimeError`` still truncates its human-facing detail;
    the FULL raw streams live in the ``artifacts`` bundle (RVW03-WS02). The
    bundle's absolute path is logged LOCALLY (a developer running the review sees
    it), but is kept OUT of the raised message — that message crosses into the
    GitHub-facing funnel breadcrumb (:func:`shipit.review.service._close_funnel_breadcrumb`),
    where a user-home / state-root path must not leak into the PR check summary.
    """
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        haystack = f"{stdout}\n{stderr}".lower()
        if _TIMEOUT_MARKER in haystack:
            # A TIMEOUT, not a generic failure. The marker may live in *stderr*
            # (not the salvageable stdout), so the human-facing message here does
            # NOT echo it — we set the STRUCTURED ``timed_out`` flag explicitly so
            # the service settles ``timed_out`` (not ``empty``) regardless. ``raw``
            # carries combined stdout+stderr so the #76 salvage still has the marker
            # context to surface. The caller (`_launch_and_capture`) records the
            # timeout into the bundle meta from this exception's ``timed_out``.
            raise BackendError(
                f"{agent} timed out before returning a complete review "
                "(try a faster model or a smaller diff)",
                raw=f"{stdout}\n{stderr}".strip(),
                timed_out=True,
            )
        detail = stderr.strip() or stdout.strip()
        if artifacts is not None and artifacts.dir is not None:
            logger.warning(
                "%s reviewer exited %d — full raw output at %s",
                agent,
                result.returncode,
                artifacts.dir,
                extra={
                    "reviewer": agent,
                    **({} if run_id is None else {"run_id": run_id}),
                },
            )
        raise RuntimeError(
            f"{agent} reviewer exited {result.returncode}: {detail[:500]}"
        )
    return parse_review_output(stdout, backend_name=agent)


def _dry_run(
    agent: str,
    ctx,
    spec: _BackendSpec,
    adapter: BackendAdapter,
    task: str,
    repo: Repo,
    branch: str,
) -> CapturedReview:
    """Print the would-run Tree-launch argv WITHOUT cloning or billing; return empty.

    Resolves the Tree's COORDINATES (the leaf dir the read-only Tree WOULD occupy) so
    the printed ``cwd`` is real, but never clones it and never launches a model. The
    codex schema temp file is shown as a placeholder path (no file written). The empty
    review flows on to ``post_review(dry_run=True)``, which prints the would-post payload
    — so the whole dry-run is honest end to end and bills nothing.
    """
    plan = readonly_plan(repo=repo, branch=branch)
    placeholder = "<review-schema-tempfile>.json" if spec.native_schema else None
    cmd = adapter.build_command(
        task,
        _REVIEWER_ROLE,
        read_only=True,
        cwd=str(plan.dir),
        output_schema_path=placeholder,
    )
    print(f"(dry-run: would launch {agent} reviewer in read-only Tree {plan.dir})")
    print(json.dumps({"cwd": str(plan.dir), "argv": cmd}, indent=2))
    return CapturedReview(
        review={
            "summary": {"status": "COMMENT", "overall_feedback": "(dry-run)"},
            "comments": [],
        },
        # A dry run launches nothing, so — like usage — no reasoning was actually
        # applied to a real launch: report the unset state, never adapter.reasoning
        # (which would echo a requested level as "applied" and mis-stamp telemetry).
        # The would-run level is already visible in the printed argv above.
        usage=UNREPORTED,
        reasoning=None,
    )


def _require_review_model(backend: Backend, model: str | None) -> None:
    """Refuse a model the backend DECLARES unusable for a reviewer Run (issue #1006).

    The mechanical half of the capability: the registry identity answers whether
    the configured model can return a verdict from a headless ``--print`` Run at
    all (:meth:`shipit.agent.backend.Backend.require_review_model` over its
    declared ``review_unusable_models``), and a refusal is re-raised as the same
    :class:`BackendUnavailable` surface a missing binary uses — so the reviewer
    dies at preflight with an actionable "this model cannot review, use <default>"
    message that names the config edit, instead of launching an agent that can only
    narrate and settling later as an unparseable "no JSON" failure.

    The check is a pure CONFIG fact (no CLI probe), so unlike the binary/flag
    preflight it is NOT skipped for a dry-run: a dry-run of a reviewer that could
    never work must say so, not print a would-run argv that reads as fine.
    ``model`` ``None`` means "the backend's own default", which the identity
    resolves and checks the same way.
    """
    try:
        backend.require_review_model(model)
    except ValueError as exc:
        raise BackendUnavailable(str(exc)) from exc


def _preflight(backend: Backend, *, model: str | None = None, dry_run: bool) -> None:
    """Verify the backend can actually review as configured — its CLI binary (the
    registry's ``binary`` alias) is on PATH, for agy that it supports the reviewer's
    ``--agent`` flag, and that ``model`` is not one the backend declares unusable for
    a review Run; raise :class:`BackendUnavailable` otherwise.

    The binary/flag probes are skipped in ``dry_run`` (a dry-run only prints the
    would-run argv; it must work without the CLI installed, mirroring the spawn
    dry-run posture); the ``model`` refusal (:func:`_require_review_model`) is NOT —
    it is config, not environment. A missing CLI on a REAL run fails loud — these are
    LOCAL backends and a missing binary must never silently degrade.

    For the ANTIGRAVITY backend the reviewer posture depends on AGY 1.1.2's native
    ``--agent`` flag (issue #989), so a real launch additionally preflights that
    capability (:func:`shipit.spawn.backends.antigravity.require_agent_support`)
    and surfaces a clean UPGRADE message when the installed ``agy`` predates it —
    the same :class:`BackendUnavailable` surface as a missing binary, so the
    round-level preflight and the service map it uniformly.
    """
    _require_review_model(backend, model)
    if dry_run:
        return
    if shutil.which(backend.binary) is None:
        raise BackendUnavailable(
            f"The '{backend.funnel_agent or backend.name}' review backend requires "
            f"the '{backend.binary}' CLI on your PATH, but it was not found. "
            f"Install it (and log it in), then re-run."
        )
    if backend is ANTIGRAVITY:
        from ..spawn.backends.antigravity import require_agent_support

        try:
            require_agent_support(binary=backend.binary)
        except RuntimeError as exc:
            raise BackendUnavailable(str(exc)) from exc


def preflight_round(
    backends: Sequence[Backend], models: Sequence[str | None] | None = None
) -> None:
    """Verify EVERY backend a round is configured to launch, ONCE, before any
    pass starts; raise ONE :class:`BackendUnavailable` naming each missing binary.

    The round-level preflight (RVW03-WS03): the fan-out calls this before
    provisioning the Tree or launching a single pass, so a missing binary
    surfaces as one actionable "binary X not found — install/configure it"
    error and NO pass processes launch — never as "all N dimension passes
    failed" with N truncated per-pass details. ``backends`` is the round's
    configured set (the reviewer's own backend plus, when the dormant judge is
    on, the calibrator's); duplicate binaries are checked once. For an AGY
    backend the round preflight ALSO validates the reviewer's ``--agent`` support
    once here (issue #989), so an ``agy`` predating 1.1.2 surfaces ONE clean
    UPGRADE :class:`BackendUnavailable` before Tree provisioning — never a
    wrapped "all N passes failed" from each per-launch :func:`_preflight`. The
    per-launch checks (:func:`_preflight`, the calibrator's own) stay as
    backstops for callers outside a fan-out round.

    ``models`` is the round's configured model per entry, POSITIONALLY aligned to
    ``backends`` (``None`` for "the backend's default"); it is checked against each
    backend's declared-unusable reviewer set FIRST (issue #1006), so a reviewer
    that could never return a verdict is refused before the Tree — the same
    round-level "fail once, launch nothing" posture the binary check has. Omitting
    ``models`` skips that check (the per-launch :func:`_preflight` still refuses).
    A length mismatch is a programming error raised loud, never a silently
    unchecked model.
    """
    if models is not None:
        if len(models) != len(backends):
            raise ValueError(
                f"preflight_round: models ({len(models)}) must align positionally "
                f"with backends ({len(backends)})"
            )
        for backend, model in zip(backends, models, strict=True):
            _require_review_model(backend, model)
    missing: list[Backend] = []
    seen: set[str] = set()
    for backend in backends:
        if backend.binary in seen:
            continue
        seen.add(backend.binary)
        if shutil.which(backend.binary) is None:
            missing.append(backend)
    if missing:
        details = "; ".join(
            f"binary {b.binary!r} not found — install/configure it "
            f"(the {(b.funnel_agent or b.name)!r} backend requires it on PATH)"
            for b in missing
        )
        raise BackendUnavailable(
            f"review preflight failed, no passes were launched: {details}"
        )
    # Every configured binary is present; now verify AGY's reviewer capability
    # once, before any Tree is provisioned (issue #989). The membership check
    # above already guarantees the binary is on PATH, so a False here is an
    # OUTDATED agy, not a missing one — raise the targeted upgrade message.
    if any(backend is ANTIGRAVITY for backend in backends):
        from ..spawn.backends.antigravity import require_agent_support

        try:
            require_agent_support(binary=ANTIGRAVITY.binary)
        except RuntimeError as exc:
            raise BackendUnavailable(str(exc)) from exc


def _resolve_repo(ctx) -> Repo:
    """The :class:`shipit.identity.Repo` for ``ctx`` — from ``ctx.repo``, else inferred.

    The detached child always resolves ``ctx`` with an explicit ``--repo``, so
    ``ctx.repo`` is normally set; a hand-built context falls back to ``gh repo view``.
    Either slug routes through the ONE canonical parser
    (:func:`shipit.identity.repo_from_slug`) so the read-only Tree's namespace is the
    case-normalized identity — an API-cased slug can never land a divergent Tree path
    (ADR-0024). A slug that is not ``owner/name`` fails loud rather than provisioning
    a Tree under a malformed identity.
    """
    slug = (ctx.repo or "").strip()
    try:
        # `gh.current_repo()` already returns the typed identity (PROC03) — the
        # fallback needs no slug round-trip; only an explicit `ctx.repo` slug is
        # parsed, through the ONE canonical parser. Either path raises
        # `ValueError` on a non-`owner/name` answer.
        return repo_from_slug(slug) if slug else gh.current_repo()
    except ValueError as exc:
        # Name the actual source: an explicit `ctx.repo` slug vs the empty-slug
        # `gh repo view` fallback — and surface `exc` so the malformed output is
        # in the message, not only the exception chain.
        source = f"the repo slug {slug!r}" if slug else "`gh repo view`"
        raise RuntimeError(
            f"cannot review PR #{ctx.number}: {source} did not yield an "
            f"owner/name identity ({exc}), so the read-only Tree's namespace "
            "cannot be resolved."
        ) from exc


def _github_url(ctx) -> str:
    """The clone URL for the read-only Tree — the consumer checkout's ``origin`` remote."""
    return git.remote_url(cwd=ctx.workdir)


def _write_schema_tempfile() -> str:
    """Write :data:`REVIEW_SCHEMA` to a temp file for codex ``--output-schema``.

    Returns the path; the caller removes it in a ``finally``. The producer owns this
    (not ``build_command``, which must stay a pure argv builder so the dry-run print is
    honest): the path is handed to the adapter, the file is cleaned up after the launch.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=".review_schema_", delete=False
    ) as fh:
        json.dump(REVIEW_SCHEMA, fh)
        return fh.name
