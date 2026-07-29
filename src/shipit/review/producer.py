"""Launches a review backend and captures its output. See docs/adr/0020-backend-adapter-contract.md."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .. import execrun, gh, git, workenv
from ..agent.backend import ANTIGRAVITY, CODEX, Backend
from ..identity import Repo, Sha, repo_from_slug
from ..spawn import launch
from ..spawn.backends.antigravity import AntigravityAdapter
from ..spawn.backends.base import BackendAdapter
from ..spawn.backends.codex import CodexAdapter
from ..tree.cleanup import parse_duration
from ..tree.create import new_tree_naming
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
    build_supplied_diff_incremental_task,
    build_supplied_diff_range_task,
    build_supplied_diff_reviewer_task,
)
from .schema import REVIEW_SCHEMA
from .usage import UNREPORTED, TokenUsage, from_codex_stderr

logger = logging.getLogger("shipit.review")

_REVIEWER_ROLE = "reviewer"


@dataclass(frozen=True)
class CapturedReview:
    """A parsed review plus the launch's measured usage and APPLIED reasoning level."""

    review: dict
    usage: TokenUsage
    reasoning: str | None


@dataclass(frozen=True)
class _BackendSpec:
    """How one funnel backend maps onto the shared spawn launch seam."""

    delivery_mode: str
    schema_inline: bool
    native_schema: bool
    native_timeout: bool
    adapter_factory: object  # (model, timeout, reasoning) -> BackendAdapter
    usage_parser: object  # (LaunchResult) -> TokenUsage
    retry_on_parse_failure: bool


def _codex_adapter(model: str, timeout: str, reasoning: str | None) -> BackendAdapter:
    # codex has no per-run timeout flag; the launch seam is its sole enforcement.
    del timeout
    return CodexAdapter(model=model, reasoning=reasoning)


def _agy_adapter(model: str, timeout: str, reasoning: str | None) -> BackendAdapter:
    # agy has no reasoning knob, so the level is dropped, not stamped as applied.
    del reasoning
    return AntigravityAdapter(model=model, timeout=timeout)


def _codex_usage(result: launch.LaunchResult) -> TokenUsage:
    return from_codex_stderr(result.stderr or "")


def _agy_usage(result: launch.LaunchResult) -> TokenUsage:
    del result
    return UNREPORTED


#: :class:`Backend` identity → how it launches as a reviewer.
_SPECS: dict[Backend, _BackendSpec] = {
    CODEX: _BackendSpec(
        delivery_mode="self-fetch",
        schema_inline=False,
        native_schema=True,
        native_timeout=False,
        adapter_factory=_codex_adapter,
        usage_parser=_codex_usage,
        retry_on_parse_failure=False,
    ),
    ANTIGRAVITY: _BackendSpec(
        delivery_mode="supplied-diff",
        schema_inline=True,
        native_schema=False,
        native_timeout=True,
        adapter_factory=_agy_adapter,
        usage_parser=_agy_usage,
        retry_on_parse_failure=True,
    ),
}


#: Seconds the seam deadline sits past a backend's own timeout flag.
_SEAM_HEADROOM_SECONDS = 60.0


def _seam_deadline(timeout: str, spec: _BackendSpec) -> float:
    """The launch-seam process deadline in seconds; raises ``ValueError`` if malformed."""
    base = parse_duration(timeout)
    return base + _SEAM_HEADROOM_SECONDS if spec.native_timeout else base


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _permission_posture(spec: _BackendSpec, *, substrate: str) -> str:
    if substrate not in {"read-only-tree", "ambient-checkout"}:
        raise ValueError(f"unknown review substrate {substrate!r}")
    if spec.delivery_mode == "supplied-diff":
        suffix = "no-dangerous-skip"
    else:
        suffix = "workspace-write-sandbox"
    return f"{substrate}-{suffix}"


def _cli_version(binary: str) -> str:
    """Best-effort CLI version for provenance; ``unknown`` rather than failing."""
    try:
        result = execrun.run([binary, "--version"], check=False, timeout=5)
    except Exception:
        return "unknown"
    text = (result.stdout or result.stderr or "").strip()
    return text.splitlines()[0].strip() if text else "unknown"


def pass_task_text(
    backend: Backend,
    pr_number: int,
    *,
    diff: str | None = None,
    instructions_path: str | None = None,
    dimension: Dimension | None = None,
    incremental_range: tuple[str, str] | None = None,
) -> str:
    """The exact task text :func:`run_tree_review` would launch with, without launching."""
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
    instructions = load_instructions(instructions_path)
    if spec.delivery_mode == "supplied-diff":
        if diff is None:
            raise ValueError(
                "pass_task_text: backend 'agy' uses supplied-diff delivery and "
                "requires diff so the variant hashes the exact launched prompt"
            )
        if incremental_range is not None:
            return build_supplied_diff_incremental_task(
                instructions,
                diff,
                pr_number,
                schema_inline=spec.schema_inline,
            )
        return build_supplied_diff_reviewer_task(
            instructions,
            diff,
            target_label=f"pull request #{pr_number}",
            diff_noun="this PR's diff",
            schema_inline=spec.schema_inline,
            dimension=dimension,
        )
    if incremental_range is not None:
        base_sha, head_sha = incremental_range
        return build_incremental_reviewer_task(
            instructions,
            pr_number,
            base_sha,
            head_sha,
            schema_inline=spec.schema_inline,
        )
    return build_reviewer_task(
        instructions,
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
    """The exact task text :func:`run_range_review` would launch with, without launching."""
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    instructions = load_instructions(instructions_path)
    if spec.delivery_mode == "supplied-diff":
        return build_supplied_diff_range_task(
            instructions,
            view.diff,
            str(view.base_sha),
            str(view.head_sha),
            schema_inline=spec.schema_inline,
            dimension=dimension,
        )
    return build_range_reviewer_task(
        instructions,
        str(view.base_sha),
        str(view.head_sha),
        schema_inline=spec.schema_inline,
        dimension=dimension,
    )


def provision_review_tree(
    ctx, backend: Backend, *, naming: Mapping[str, str] | None = None
) -> str:
    """Provision the read-only Tree on ``ctx``'s head; ``naming`` reuses already-minted leaf coordinates."""
    repo = _resolve_repo(ctx)
    branch = (ctx.head_ref or "").strip()
    if not branch:
        raise RuntimeError(
            f"cannot review PR #{ctx.number}: its head branch (headRefName) is "
            "unknown, so the read-only Tree cannot be provisioned."
        )
    leaf = dict(naming) if naming is not None else new_tree_naming(backend.binary)
    tree = create_readonly(
        readonly_plan(repo=repo, branch=branch, **leaf),
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
    """Launch ``backend`` as a reviewer in a read-only Tree and capture its review; never posts."""
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
    _preflight(backend, dry_run=dry_run)

    instructions = load_instructions(instructions_path)
    if spec.delivery_mode == "supplied-diff":
        if incremental_range is not None:
            task = build_supplied_diff_incremental_task(
                instructions,
                ctx.diff,
                ctx.number,
                schema_inline=spec.schema_inline,
            )
        else:
            task = build_supplied_diff_reviewer_task(
                instructions,
                ctx.diff,
                target_label=f"pull request #{ctx.number}",
                diff_noun="this PR's diff",
                schema_inline=spec.schema_inline,
                dimension=dimension,
            )
    elif incremental_range is not None:
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
            "(headRefName) is unknown, so the per-Run read-only Tree "
            "cannot be provisioned."
        )

    schema_path: str | None = None
    try:
        if dry_run:
            return _dry_run(agent, ctx, spec, adapter, task, repo, branch)

        if spec.native_schema:
            schema_path = _write_schema_tempfile()

        cwd = (
            tree_path if tree_path is not None else provision_review_tree(ctx, backend)
        )
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
            backend,
            spec,
            adapter,
            task,
            cwd=cwd,
            timeout=timeout,
            diff=ctx.diff if spec.delivery_mode == "supplied-diff" else None,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
            substrate="read-only-tree",
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
    """Launch ``backend`` over a commit range in ``view.workdir`` — no Tree, no ``gh``."""
    agent = backend.funnel_agent or backend.name
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"unknown funnel review backend {backend.name!r} "
            f"(known: {', '.join(b.name for b in _SPECS)})"
        )
    _preflight(backend, dry_run=False)

    instructions = load_instructions(instructions_path)
    if spec.delivery_mode == "supplied-diff":
        task = build_supplied_diff_range_task(
            instructions,
            view.diff,
            str(view.base_sha),
            str(view.head_sha),
            schema_inline=spec.schema_inline,
            dimension=dimension,
        )
    else:
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
            backend,
            spec,
            adapter,
            task,
            cwd=str(view.workdir),
            timeout=timeout,
            diff=view.diff if spec.delivery_mode == "supplied-diff" else None,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
            substrate="ambient-checkout",
        )
    finally:
        if schema_path and os.path.exists(schema_path):
            os.remove(schema_path)


def _launch_and_capture(
    agent: str,
    backend: Backend,
    spec: _BackendSpec,
    adapter: BackendAdapter,
    task: str,
    *,
    cwd: str,
    timeout: str,
    diff: str | None = None,
    schema_path: str | None,
    launcher: launch.Runner | None,
    artifacts: RunArtifacts | None = None,
    run_id: str | None = None,
    substrate: str,
) -> CapturedReview:
    """Launch a reviewer child, capturing its review with at most one re-prompt on parse failure."""
    try:
        return _attempt(
            agent,
            backend,
            spec,
            adapter,
            task,
            cwd=cwd,
            timeout=timeout,
            diff=diff,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
            substrate=substrate,
        )
    except BackendError as exc:
        # A timeout is never retried: a second run would just re-burn the deadline.
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
            backend,
            spec,
            adapter,
            _retry_task(task, exc),
            cwd=cwd,
            timeout=timeout,
            diff=diff,
            schema_path=schema_path,
            launcher=launcher,
            artifacts=artifacts,
            run_id=run_id,
            substrate=substrate,
        )


def _retry_task(task: str, failure: BackendError) -> str:
    """Compose the re-prompt: the original ``task`` plus the exact parse failure."""
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
    backend: Backend,
    spec: _BackendSpec,
    adapter: BackendAdapter,
    task: str,
    *,
    cwd: str,
    timeout: str,
    diff: str | None,
    schema_path: str | None,
    launcher: launch.Runner | None,
    artifacts: RunArtifacts | None = None,
    run_id: str | None = None,
    substrate: str,
) -> CapturedReview:
    """Launch one reviewer child in ``cwd`` under the seam deadline and parse its stdout."""
    sink = artifacts if artifacts is not None else RunArtifacts.disabled()
    cmd = adapter.build_command(
        task,
        _REVIEWER_ROLE,
        read_only=True,
        cwd=cwd,
        output_schema_path=schema_path,
    )
    sink.write_prompt(task)
    sink.record(
        argv=list(cmd),
        cwd=cwd,
        seam_deadline_s=_seam_deadline(timeout, spec),
        cli_version=_cli_version(backend.binary),
        resolved_model=getattr(adapter, "model", None),
        delivery_mode=spec.delivery_mode,
        permission_posture=_permission_posture(spec, substrate=substrate),
        input_digest=_sha256(task),
        input_bytes=len(task.encode("utf-8")),
        diff_digest=None if diff is None else _sha256(diff),
        diff_bytes=None if diff is None else len(diff.encode("utf-8")),
    )
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
            stdout_bytes=len((exc.stdout or "").encode("utf-8")),
            stderr_bytes=len((exc.stderr or "").encode("utf-8")),
            outcome="timed_out" if timed_out else "failed",
            error=str(exc),
        )
        if not timed_out:
            # A nonzero child is a LaunchResult, so this is always transport.
            raise
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
        stdout_bytes=len((result.stdout or "").encode("utf-8")),
        stderr_bytes=len((result.stderr or "").encode("utf-8")),
    )
    try:
        review = _capture(agent, result, artifacts=sink, run_id=run_id)
    except BackendError as exc:
        # An exit-0 launch can still be a timeout, so correct the optimism above.
        sink.record(
            timed_out=exc.timed_out,
            outcome="timed_out" if exc.timed_out else "failed",
        )
        raise
    except RuntimeError:
        sink.record(outcome="failed")
        raise
    sink.record(outcome="success")
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
    """Turn the reviewer's result into a review dict; raised messages carry no local paths."""
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        haystack = f"{stdout}\n{stderr}".lower()
        if _TIMEOUT_MARKER in haystack:
            # The marker may live in stderr, so signal the timeout structurally.
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
    """Print the would-run Tree-launch argv without cloning or billing; return empty."""
    plan = readonly_plan(repo=repo, branch=branch, **new_tree_naming(agent))
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
        usage=UNREPORTED,
        reasoning=None,
    )


def _preflight(backend: Backend, *, dry_run: bool) -> None:
    """Verify the backend's CLI binary is on PATH; skipped under ``dry_run``."""
    if dry_run:
        return
    if shutil.which(backend.binary) is None:
        raise BackendUnavailable(
            f"The '{backend.funnel_agent or backend.name}' review backend requires "
            f"the '{backend.binary}' CLI on your PATH, but it was not found. "
            f"Install it (and log it in), then re-run."
        )


def preflight_round(backends: Sequence[Backend]) -> None:
    """Check every backend a round will launch up front, raising one error naming all missing binaries."""
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


def _resolve_repo(ctx) -> Repo:
    """The :class:`shipit.identity.Repo` for ``ctx`` — from ``ctx.repo``, else inferred."""
    slug = (ctx.repo or "").strip()
    try:
        return repo_from_slug(slug) if slug else gh.current_repo()
    except ValueError as exc:
        source = f"the repo slug {slug!r}" if slug else "`gh repo view`"
        raise RuntimeError(
            f"cannot review PR #{ctx.number}: {source} did not yield an "
            f"owner/name identity ({exc}), so the read-only Tree's namespace "
            "cannot be resolved."
        ) from exc


def _github_url(ctx) -> str:
    return git.remote_url(cwd=ctx.workdir)


def _write_schema_tempfile() -> str:
    """Write :data:`REVIEW_SCHEMA` to a temp file the caller must remove."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=".review_schema_", delete=False
    ) as fh:
        json.dump(REVIEW_SCHEMA, fh)
        return fh.name
