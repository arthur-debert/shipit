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
    (:func:`shipit.review.backends.parse_review_output`).

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
from dataclasses import dataclass

from .. import execrun, gh, git
from ..agent.backend import ANTIGRAVITY, CODEX, Backend
from ..identity import Repo, repo_from_slug
from ..spawn import launch
from ..spawn.backends.antigravity import AntigravityAdapter
from ..spawn.backends.base import BackendAdapter
from ..spawn.backends.codex import CodexAdapter
from ..tree.cleanup import parse_duration
from ..tree.readonly import create_readonly, readonly_plan
from .backends import BackendError, BackendUnavailable, parse_review_output
from .backends.base import _TIMEOUT_MARKER
from .instructions import load_instructions
from .prompt import build_reviewer_task
from .schema import REVIEW_SCHEMA

logger = logging.getLogger("shipit.review")

#: The reviewer role the spawn adapter's read-only posture is built for (mirrors
#: :data:`shipit.verbs.spawn.REVIEWER_ROLE`). It anchors the role preamble codex / agy
#: prepend to the task; the funnel result channel is shipit's capture-and-post, not the
#: agent self-posting, so the task itself tells the agent NOT to post.
_REVIEWER_ROLE = "reviewer"


@dataclass(frozen=True)
class _BackendSpec:
    """How one funnel backend maps onto the shared spawn launch seam.

    ``adapter_factory`` builds the spawn ``BackendAdapter`` carrying the model (and,
    for agy, the timeout) — the SAME adapter the spawn surface launches a reviewer
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
    """

    schema_inline: bool
    native_schema: bool
    native_timeout: bool
    adapter_factory: object  # Callable[[str, str], BackendAdapter]


def _codex_adapter(model: str, timeout: str) -> BackendAdapter:
    # codex has no per-run timeout flag, so the deadline is NOT threaded into the
    # adapter (only the model is) — it is enforced at the launch SEAM instead, where
    # `run_tree_review` passes it to `launch.launch(timeout=...)` as a hard process
    # deadline (#404). `native_timeout=False` in the spec records that the seam is
    # codex's SOLE timeout enforcement.
    del timeout
    return CodexAdapter(model=model)


def _agy_adapter(model: str, timeout: str) -> BackendAdapter:
    return AntigravityAdapter(model=model, timeout=timeout)


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
    ),
    ANTIGRAVITY: _BackendSpec(
        schema_inline=True,
        native_schema=False,
        native_timeout=True,
        adapter_factory=_agy_adapter,
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


def run_tree_review(
    backend: Backend,
    ctx,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dry_run: bool = False,
    launcher: launch.Runner | None = None,
) -> dict:
    """Launch ``backend`` as a reviewer in a read-only Tree and CAPTURE its review
    dict.

    Provisions the shared read-only Tree on ``ctx``'s PR head, launches the backend
    through its spawn read-only posture with a task that fetches the diff via
    ``gh pr diff`` and emits structured JSON, captures stdout, and parses it. Returns
    the review dict; it does NOT post and does NOT touch the check-run (the service
    owns those). Raises :class:`BackendUnavailable` (missing CLI), :class:`BackendError`
    (unparseable / timed-out output, carrying the raw for salvage), or a plain
    ``RuntimeError`` (a nonzero child / a missing PR head branch) → the service maps it
    to ``failed``.

    With ``dry_run=True``: resolves the Tree COORDINATES (no clone, no model bill),
    prints the would-run Tree-launch argv, and returns an empty review — so a dry-run is
    honest (it shows exactly what would run and bills nothing).
    """
    agent = backend.funnel_agent or backend.name
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    _preflight(backend, dry_run=dry_run)

    repo = _resolve_repo(ctx)
    branch = (ctx.head_ref or "").strip()
    if not branch:
        raise RuntimeError(
            f"cannot review PR #{ctx.number}: its head branch (headRefName) is "
            "unknown, so the shared read-only Tree cannot be provisioned."
        )

    instructions = load_instructions(instructions_path)
    task = build_reviewer_task(
        instructions, ctx.number, schema_inline=spec.schema_inline
    )
    adapter = spec.adapter_factory(model, timeout)  # type: ignore[operator]

    schema_path: str | None = None
    try:
        if dry_run:
            return _dry_run(agent, ctx, spec, adapter, task, repo, branch)

        if spec.native_schema:
            schema_path = _write_schema_tempfile()

        tree = create_readonly(
            readonly_plan(repo=repo, branch=branch),
            source_repo=ctx.workdir,
            github_url=_github_url(ctx),
        )
        cmd = adapter.build_command(
            task,
            _REVIEWER_ROLE,
            read_only=True,
            cwd=tree.path,
            output_schema_path=schema_path,
        )
        logger.info(
            "review launching for pr#%s (agent=%s) in read-only Tree %s",
            ctx.number,
            agent,
            tree.path,
            extra={"pr": ctx.number, "tree": tree.path, "reviewer": agent},
        )
        try:
            result = launch.launch(
                cmd,
                cwd=tree.path,
                env=adapter.child_env(),
                timeout=_seam_deadline(timeout, spec),
                runner=launcher,
            )
        except execrun.ExecError as exc:
            if exc.cause != execrun.CAUSE_TIMEOUT:
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
        return _capture(agent, result)
    finally:
        if schema_path and os.path.exists(schema_path):
            os.remove(schema_path)


def _capture(agent: str, result: launch.LaunchResult) -> dict:
    """Turn the launched reviewer's result into a review dict, or raise.

    A nonzero exit is a hard failure (mirroring the retired ``proc.run(check=True)``
    backends) — UNLESS the agy print-timeout marker is present, which is a TIMEOUT, not
    a generic failure (so it settles ``timed_out``, not ``failed``). On exit 0 the raw
    stdout is parsed; an unparseable / marker-bearing parse raises :class:`BackendError`
    (carrying the raw for the #76 salvage), exactly as before.
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
            # context to surface.
            raise BackendError(
                f"{agent} timed out before returning a complete review "
                "(try a faster model or a smaller diff)",
                raw=f"{stdout}\n{stderr}".strip(),
                timed_out=True,
            )
        detail = stderr.strip() or stdout.strip()
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
) -> dict:
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
    return {
        "summary": {"status": "COMMENT", "overall_feedback": "(dry-run)"},
        "comments": [],
    }


def _preflight(backend: Backend, *, dry_run: bool) -> None:
    """Verify the backend's CLI binary (the registry's ``binary`` alias) is on
    PATH; raise :class:`BackendUnavailable` otherwise.

    Skipped in ``dry_run`` (a dry-run only prints the would-run argv; it must work
    without the CLI installed, mirroring the spawn dry-run posture). A missing CLI on a
    REAL run fails loud — these are LOCAL backends and a missing binary must never
    silently degrade.
    """
    if dry_run:
        return
    if shutil.which(backend.binary) is None:
        raise BackendUnavailable(
            f"The '{backend.funnel_agent or backend.name}' review backend requires "
            f"the '{backend.binary}' CLI on your PATH, but it was not found. "
            f"Install it (and log it in), then re-run."
        )


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
