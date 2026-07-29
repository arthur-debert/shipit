"""The ``shipit spawn subagent`` pipeline: spec → validate → Tree → launch → audit."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import events, execrun, gh, git, identity, logcontext, pixienv, workenv
from ..agent import backend as agent_backend
from ..harness import roleprofile
from ..pr import PrId
from ..review import service as review_service
from ..tree.create import Tree, create, new_tree_naming
from ..tree.layout import (
    TreeSpec,
    epic_umbrella_base,
    issue_branch,
    work_stream_branch,
)
from ..tree.readonly import readonly_plan
from . import backends, launch

logger = logging.getLogger("shipit.spawn")

SUPPORTED_BACKENDS = backends.supported_backends()


class SpawnError(RuntimeError):
    """A spawn pipeline refusal, rendered as ``error: …`` + exit 1."""


@dataclass(frozen=True)
class SubagentSpec:
    repo: str
    role: str
    epic: str | None = None
    ws: int | None = None
    issue: int | None = None
    pr: int | None = None
    session: str = "work"
    backend: str = "claude"

    @property
    def has_epic_shape(self) -> bool:
        return self.epic is not None or self.ws is not None


@dataclass(frozen=True)
class SpawnResult:
    """The finished spawn's coordinates; PR fields stay ``None`` for a reviewer Run."""

    tree: str
    branch: str
    base: str
    role: str
    backend: str
    pr: int | None = None
    pr_state: str | None = None
    pr_is_draft: bool | None = None

    def to_dict(self) -> dict:
        payload: dict = {
            "tree": self.tree,
            "branch": self.branch,
            "base": self.base,
            "role": self.role,
            "backend": self.backend,
        }
        if self.pr is not None:
            payload["pr"] = self.pr
            payload["pr_state"] = self.pr_state
            payload["pr_is_draft"] = self.pr_is_draft
        return payload


@dataclass(frozen=True)
class Boundaries:
    """The injectable effectful edges; a ``None`` runner means the real one."""

    repo_root: Callable[[], str | None] = git.repo_root
    resolve_repo: Callable[[str], identity.Repo] = identity.resolve_repo
    remote_url: Callable[..., str] = git.remote_url
    remote_branch_exists: Callable[..., bool] = git.remote_branch_exists
    create_tree: Callable[..., Tree] = create
    pr_for_head: Callable[..., gh.HeadPr | gh.UnknownPr | None] = gh.pr_for_head
    pr_for_number: Callable[..., gh.PrAttachment] = gh.pr_for_number
    status_porcelain: Callable[..., list[str]] = git.status_porcelain
    runner: launch.Runner | None = None
    run_review: Callable[..., dict] = review_service.run_detached_review


BOUNDARIES = Boundaries()


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _refusal(
    message: str, *, exc: BaseException | None = None, **fields: object
) -> SpawnError:
    """Log the durable ERROR record and RETURN the exception for the caller to raise."""
    extras = {name: value for name, value in fields.items() if value is not None}
    # `exc_info=True` (not the instance) so the real traceback is attached.
    logger.error("spawn subagent: %s", message, exc_info=exc is not None, extra=extras)
    return SpawnError(message)


def spawn_subagent(spec: SubagentSpec, bounds: Boundaries | None = None) -> SpawnResult:
    """Raises :class:`SpawnError` at every refusal; ``bounds`` of ``None`` is production."""
    bounds = bounds if bounds is not None else BOUNDARIES
    # A nested spawn inherits the parent's `SHIPIT_LOG_CTX_*`, and `bind` drops
    # `None` halves, so a stale `epic`/`ws` would leak into the new child.
    logcontext.unbind("tree", "agent", "epic", "ws", "role", "pr", "repo")
    logger.info(
        "spawn subagent: %s run requested on backend %s",
        spec.role,
        spec.backend,
        extra={
            name: value
            for name, value in {
                "role": spec.role,
                "backend": spec.backend,
                "epic": spec.epic,
                "ws": spec.ws,
                "issue": spec.issue,
                "pr": spec.pr,
                "session": spec.session if not spec.has_epic_shape else None,
            }.items()
            if value is not None
        },
    )
    adapter, profile = validate(spec)

    # The role binds NORMALIZED; `agent` binds in the launch tails once minted.
    logcontext.bind(epic=spec.epic, ws=spec.ws, role=profile.role.value)

    root, repo_identity, url = resolve_spawn_identity(spec, bounds)
    logcontext.bind(repo=repo_identity.slug)

    checkout = profile.checkout
    if isinstance(checkout, roleprofile.PerRunReadOnlyTree):
        try:
            review_branch = (
                work_stream_branch(spec.epic, spec.ws)
                if spec.has_epic_shape
                else issue_branch(spec.issue, spec.session)
            )
        except ValueError as exc:
            raise _refusal(str(exc), exc=exc) from exc
        return _launch_reviewer(
            repo=repo_identity,
            branch=review_branch,
            source_repo=root,
            role=profile.role.value,
            adapter=adapter,
            bounds=bounds,
        )
    if isinstance(checkout, roleprofile.ExistingPrWriteTree):
        return _launch_existing_pr_write(
            repo=repo_identity,
            source_repo=root,
            github_url=url,
            role=profile.role.value,
            pr_number=spec.pr,
            backend=spec.backend,
            adapter=adapter,
            bounds=bounds,
        )
    if isinstance(checkout, roleprofile.NewWriteTree):
        tree_spec = plan_write_spec(spec, repo_identity, root, bounds)
        return _launch_write(
            tree_spec,
            source_repo=root,
            github_url=url,
            role=profile.role.value,
            issue=spec.issue,
            backend=spec.backend,
            adapter=adapter,
            bounds=bounds,
        )
    raise _refusal(
        f"role {profile.role.value!r} has checkout strategy "
        f"{type(checkout).__name__!r}, which has no detached launch tail.",
        role=profile.role.value,
    )


def validate(
    spec: SubagentSpec,
) -> tuple[backends.BackendAdapter, roleprofile.RoleProfile]:
    """Stage 1 — the shape gate (before any I/O). Returns (adapter, role profile)."""
    if spec.backend not in SUPPORTED_BACKENDS:
        supported = ", ".join(SUPPORTED_BACKENDS)
        raise _refusal(
            f"unsupported backend {spec.backend!r} (supported: {supported}); wiring a "
            "new backend is one entry in the adapter registry (ADR-0020).",
            backend=spec.backend,
        )
    adapter = backends.resolve(spec.backend)

    try:
        profile = roleprofile.validate_spawn(
            spec.role, roleprofile.LaunchContext.DETACHED
        )
    except roleprofile.RoleValidationError as exc:
        raise _refusal(
            str(exc),
            exc=exc,
            role=spec.role,
            requested_role=spec.role,
            launch_context=roleprofile.LaunchContext.DETACHED.value,
            refusal_reason="role-profile-validation",
        ) from exc

    if spec.has_epic_shape and (spec.epic is None or spec.ws is None):
        raise _refusal(
            "the epic shape needs both --epic and --ws "
            f"(got epic={spec.epic!r}, ws={spec.ws!r}); omit both for a standalone "
            "--issue Tree.",
            epic=spec.epic,
            ws=spec.ws,
        )
    if spec.has_epic_shape and spec.ws < 1:
        raise _refusal(
            f"--ws must be a positive integer (got {spec.ws})",
            epic=spec.epic,
            ws=spec.ws,
        )
    attachment_roles = ", ".join(
        role.value
        for role in roleprofile.roles_with_checkout_strategy(
            roleprofile.ExistingPrWriteTree
        )
    )
    if isinstance(profile.checkout, roleprofile.ExistingPrWriteTree):
        if spec.pr is None or spec.pr < 1:
            raise _refusal(
                "--pr must be a positive integer for an existing-PR attachment "
                f"role ({attachment_roles}; got {spec.pr})",
                role=spec.role,
            )
        if spec.has_epic_shape or spec.issue is not None:
            raise _refusal(
                "an existing-PR attachment role attaches with --pr only; do not "
                "pass --issue, --epic, or --ws, which belong to new-branch/review "
                f"shapes (attachment roles: {attachment_roles}).",
                role=spec.role,
                pr=spec.pr,
                issue=spec.issue,
                epic=spec.epic,
                ws=spec.ws,
            )
    elif spec.pr is not None:
        raise _refusal(
            "--pr is only valid for existing-PR attachment roles "
            f"({attachment_roles}; got role {profile.role.value!r})",
            role=profile.role.value,
            pr=spec.pr,
        )
    if isinstance(profile.checkout, roleprofile.NewWriteTree) and (
        spec.issue is None or spec.issue < 1
    ):
        raise _refusal(
            f"--issue must be a positive integer (got {spec.issue})", role=spec.role
        )
    if (
        isinstance(profile.checkout, roleprofile.PerRunReadOnlyTree)
        and not spec.has_epic_shape
        and spec.issue is None
    ):
        raise _refusal(
            "a reviewer needs a branch to review — give --epic E --ws N or --issue N.",
            role=spec.role,
        )
    if isinstance(profile.checkout, roleprofile.PerRunReadOnlyTree):
        review_backend = agent_backend.by_name(adapter.name)
        if not review_backend.has_funnel_identity:
            supported = ", ".join(
                backend.name for backend in agent_backend.funnel_backends()
            )
            raise _refusal(
                f"backend {adapter.name!r} has no captured review-service identity "
                f"(supported reviewer backends: {supported}); refused before any "
                "Tree is provisioned or a backend launched.",
                role=profile.role.value,
                backend=adapter.name,
            )
    return adapter, profile


def resolve_spawn_identity(
    spec: SubagentSpec, bounds: Boundaries
) -> tuple[str, identity.Repo, str]:
    """Stage 2 — ``(root, repo_identity, github_url)``; ``--repo`` guards the checkout."""
    root = bounds.repo_root()
    if not root:
        raise _refusal("not inside a git checkout")
    try:
        repo_identity = bounds.resolve_repo(root)
        url = bounds.remote_url(cwd=root)
    except (execrun.ExecError, ValueError) as exc:
        raise _refusal(str(exc), exc=exc) from exc

    if spec.repo.strip().lower() not in (repo_identity.name, repo_identity.slug):
        raise _refusal(
            f"--repo {spec.repo!r} but the ambient checkout is "
            f"{repo_identity.slug!r}; the skeleton spawns from the target checkout "
            "(multi-repo selection is a later WS)."
        )
    return root, repo_identity, url


def plan_write_spec(
    spec: SubagentSpec,
    repo_identity: identity.Repo,
    root: str,
    bounds: Boundaries,
) -> TreeSpec:
    if spec.has_epic_shape:
        try:
            umbrella_base = epic_umbrella_base(spec.epic)  # origin/E/umbrella
        except ValueError as exc:
            raise _refusal(str(exc), exc=exc) from exc
        umbrella_branch = umbrella_base.split("/", 1)[-1]  # E/umbrella
        try:
            umbrella_exists = bounds.remote_branch_exists(umbrella_branch, cwd=root)
        except execrun.ExecError as exc:
            raise _refusal(str(exc), exc=exc) from exc
        if not umbrella_exists:
            raise _refusal(
                f"epic base branch {umbrella_branch!r} does not exist "
                f"on origin; cannot cut work stream {spec.epic}/WS{spec.ws:02d} from "
                "it. Create the epic umbrella branch first — refusing to fall back to "
                "origin/main, which would target the WS PR at the wrong base "
                "(#176, fail-closed).",
                epic=spec.epic,
                ws=spec.ws,
            )
        return TreeSpec(
            repo=repo_identity,
            **new_tree_naming(agent_backend.by_name(spec.backend).binary),
            epic=spec.epic,
            ws=spec.ws,
        )
    try:
        issue_branch(spec.issue, spec.session)  # validation only; the spec re-plans it
    except ValueError as exc:
        raise _refusal(str(exc), exc=exc) from exc
    return TreeSpec(
        repo=repo_identity,
        **new_tree_naming(agent_backend.by_name(spec.backend).binary),
        issue=spec.issue,
        session=spec.session,
    )


def salvage_note(tree_path: str, bounds: Boundaries) -> str | None:
    """The Tree's uncommitted-change note, or ``None`` when clean or unreadable."""
    try:
        dirty = bounds.status_porcelain(cwd=tree_path)
    except (execrun.ExecError, OSError):
        logger.debug(
            "salvage probe failed on %s (never masks the refusal)",
            tree_path,
            exc_info=True,
        )
        return None
    if not dirty:
        return None
    count = len(dirty)
    logger.warning(
        "spawn subagent: the failed run left %d uncommitted change(s) in the tree",
        count,
        extra={"uncommitted": count},
    )
    return (
        f"the tree at {tree_path} holds {count} uncommitted change(s) "
        "(git status --porcelain) — the Run's work may be salvageable; inspect "
        "the tree before discarding it."
    )


def _read_optional_env_identity(env_prefix: Path) -> pixienv.EnvIdentity | None:
    try:
        return pixienv.read_env_identity(env_prefix)
    except Exception:  # noqa: BLE001 - optional metadata must never block launch.
        logger.warning(
            "spawn subagent: pixi env identity unreadable at %s; "
            "continuing without optional identity metadata",
            env_prefix,
            exc_info=True,
        )
        return None


def audit_handshake(
    pr: gh.HeadPr | gh.UnknownPr | None, *, branch: str, base_branch: str
) -> gh.HeadPr:
    """Stage 6 — require an OPEN, DRAFT PR on ``branch`` targeting ``base_branch``."""
    if pr is None:
        raise _refusal(
            f"child exited 0 but opened no PR on {branch!r}; "
            "the Run did not report back through a draft PR.",
            branch=branch,
        )
    if pr is gh.UNKNOWN:
        raise _refusal(
            f"child exited 0 but the PR state for {branch!r} "
            "could not be read (gh unreadable); not claiming success.",
            branch=branch,
        )
    if pr.state != "OPEN":
        raise _refusal(
            f"child exited 0 but the PR on {branch!r} is "
            f"{pr.state}, not OPEN; the Run did not report back through an open "
            "draft PR.",
            branch=branch,
            pr=pr.number,
            pr_state=pr.state,
        )
    if not pr.is_draft:
        raise _refusal(
            f"child exited 0 but the PR on {branch!r} is not a "
            "draft; the Run must report back through a draft PR (the turn-signal the "
            "coordinator drives).",
            branch=branch,
            pr=pr.number,
        )
    if pr.base_ref != base_branch:
        raise _refusal(
            f"child exited 0 but the PR on {branch!r} targets "
            f"base {pr.base_ref!r}, not the intended {base_branch!r}; the "
            "Run reported back against the wrong base.",
            branch=branch,
            pr=pr.number,
            pr_base=pr.base_ref,
        )
    return pr


def _run_child(
    cmd: list[str],
    *,
    tree: Tree,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
    role: str,
) -> launch.LaunchResult:
    events.emit(
        logger,
        "agent.spawned",
        "spawn subagent: launching %s child (role=%s) in the tree",
        adapter.name,
        role,
        extra={"backend": adapter.name, "role": role, "cwd": tree.path},
    )
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase agent_running for %s run",
        role,
        extra={"phase": "agent_running", "backend": adapter.name, "role": role},
    )
    launch_start = time.monotonic()
    try:
        result = launch.launch(
            cmd,
            cwd=tree.path,
            env=logcontext.env_export(adapter.child_env()),
            runner=bounds.runner,
        )
    except execrun.ExecError as exc:
        # A nonzero CHILD comes back as a LaunchResult, so this is always transport.
        raise _refusal(str(exc), exc=exc, backend=adapter.name) from exc
    child_ms = _elapsed_ms(launch_start)
    if result.returncode != 0:
        raise _refusal(
            launch.child_failure_detail(
                result,
                backend=adapter.name,
                tree_path=tree.path,
                duration_ms=child_ms,
            ),
            backend=adapter.name,
            rc=result.returncode,
            duration_ms=child_ms,
            stdout_bytes=len(result.stdout.encode("utf-8")),
            stderr_bytes=len(result.stderr.encode("utf-8")),
        )
    events.emit(
        logger,
        "agent.done",
        "spawn subagent: %s child exited 0 in %dms",
        adapter.name,
        child_ms,
        extra={
            "backend": adapter.name,
            "rc": result.returncode,
            "duration_ms": child_ms,
        },
    )
    return result


def _launch_write(
    spec: TreeSpec,
    *,
    source_repo: str,
    github_url: str,
    role: str,
    issue: int | None,
    backend: str,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
) -> SpawnResult:
    create_start = time.monotonic()
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase tree_provisioning for %s run",
        role,
        extra={"phase": "tree_provisioning", "role": role, "backend": backend},
    )
    try:
        tree = bounds.create_tree(spec, source_repo=source_repo, github_url=github_url)
    except (ValueError, execrun.ExecError, OSError) as exc:
        # Fail-closed: no worktree fallback, so this never launches against the parent.
        raise _refusal(
            f"tree creation failed: {exc}",
            exc=exc,
            duration_ms=_elapsed_ms(create_start),
        ) from exc

    # The Tree dir's `<id>` UUID doubles as the Run's `agent` identity.
    logcontext.bind(tree=tree.path, agent=spec.tree_id)
    create_ms = _elapsed_ms(create_start)
    logger.info(
        "spawn subagent: write tree assigned on %s (base %s) in %dms",
        tree.branch,
        tree.base,
        create_ms,
        extra={"branch": tree.branch, "base": tree.base, "duration_ms": create_ms},
    )
    base_branch = tree.base.split("/", 1)[-1] if "/" in tree.base else tree.base
    # A work-stream Run links `for`, not `closes`: the umbrella PR closes the issue.
    task = launch.write_task(
        role,
        issue=issue,
        branch=tree.branch,
        base_branch=base_branch,
        closes=spec.epic is None,
    )
    pixi_provisioned = pixienv.has_default_env(tree.path)
    env_prefix = Path(tree.path).joinpath(*pixienv.DEFAULT_ENV_DIR)
    work_env = workenv.resolve_write_run_env(
        repo=spec.repo,
        tree_path=tree.path,
        branch=tree.branch,
        base=tree.base,
        pixi_provisioned=pixi_provisioned,
        env_identity=(
            _read_optional_env_identity(env_prefix) if pixi_provisioned else None
        ),
    )
    logger.info(
        "spawn subagent: work env resolved — %s routing for the write tree",
        work_env.routing.value,
        extra=workenv.resolution_record(
            work_env,
            boundary="spawn.write-run",
            role=role,
        ),
    )
    cmd = launch.route_argv(adapter.build_command(task, role, cwd=tree.path), work_env)
    try:
        _run_child(cmd, tree=tree, adapter=adapter, bounds=bounds, role=role)

        events.emit(
            logger,
            "agent.phase",
            "spawn subagent: phase pr_audit for %s run",
            role,
            extra={"phase": "pr_audit", "role": role, "backend": backend},
        )
        pr = audit_handshake(
            bounds.pr_for_head(tree.branch, cwd=tree.path),
            branch=tree.branch,
            base_branch=base_branch,
        )
    except SpawnError as exc:
        # NOT re-routed through `_refusal`: both halves already logged.
        note = salvage_note(tree.path, bounds)
        if note is None:
            raise
        raise SpawnError(f"{exc}\n{note}") from exc
    result = SpawnResult(
        tree=tree.path,
        branch=tree.branch,
        base=tree.base,
        role=role,
        backend=backend,
        pr=pr.number,
        pr_state=pr.state,
        pr_is_draft=pr.is_draft,
    )
    _log_spawned(result)
    return result


def _resolve_pr_attachment(
    *,
    repo: identity.Repo,
    pr_number: int,
    bounds: Boundaries,
) -> gh.PrAttachment:
    try:
        pr = bounds.pr_for_number(pr_number, repo=repo.slug)
    except (execrun.ExecError, ValueError) as exc:
        raise _refusal(
            f"could not resolve pull request #{pr_number} for shepherd attachment: {exc}",
            exc=exc,
            pr=pr_number,
        ) from exc
    if pr.state != "OPEN":
        raise _refusal(
            f"pull request #{pr.number} is {pr.state}, not OPEN; refused before "
            "launching a shepherd.",
            pr=pr.number,
            pr_state=pr.state,
        )
    if pr.is_cross_repository:
        raise _refusal(
            f"pull request #{pr.number} is from a fork; refused before launching a "
            "shepherd because fork-head fetching and pushing are not supported by "
            "the existing-PR attachment.",
            pr=pr.number,
        )
    return pr


def _audit_existing_pr_head(
    pr: gh.HeadPr | gh.UnknownPr | None,
    *,
    expected_pr: gh.PrAttachment,
    branch: str,
) -> gh.HeadPr:
    if pr is None:
        raise _refusal(
            f"pull request #{expected_pr.number} head branch {branch!r} no longer "
            "has a pull request; refused before launching a shepherd.",
            branch=branch,
            pr=expected_pr.number,
        )
    if pr is gh.UNKNOWN:
        raise _refusal(
            f"could not determine the pull request for head branch {branch!r}; "
            "refused before launching a shepherd.",
            branch=branch,
            pr=expected_pr.number,
        )
    if pr.number != expected_pr.number:
        raise _refusal(
            f"head branch {branch!r} now belongs to PR #{pr.number}, not the "
            f"requested PR #{expected_pr.number}; refused before launching a shepherd.",
            branch=branch,
            pr=expected_pr.number,
        )
    if pr.state != "OPEN":
        raise _refusal(
            f"pull request #{pr.number} on head branch {branch!r} is {pr.state}, "
            "not OPEN; refused before launching a shepherd.",
            branch=branch,
            pr=pr.number,
            pr_state=pr.state,
        )
    if pr.base_ref != expected_pr.base_ref:
        raise _refusal(
            f"pull request #{pr.number} on head branch {branch!r} targets base "
            f"{pr.base_ref!r}, not the attachment base {expected_pr.base_ref!r}; "
            "refused before launching a shepherd.",
            branch=branch,
            pr=pr.number,
            pr_base=pr.base_ref,
        )
    return pr


def _launch_existing_pr_write(
    *,
    repo: identity.Repo,
    source_repo: str,
    github_url: str,
    role: str,
    pr_number: int | None,
    backend: str,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
) -> SpawnResult:
    """Shepherd tail: attach to an existing PR head and push fixes in place."""
    assert pr_number is not None  # validate() enforces this before dispatch.
    attach = _resolve_pr_attachment(repo=repo, pr_number=pr_number, bounds=bounds)
    branch = attach.head_ref
    base = f"origin/{branch}"
    tree_spec = TreeSpec(
        repo=repo,
        **new_tree_naming(agent_backend.by_name(backend).binary),
        branch=branch,
        base=base,
    )
    create_start = time.monotonic()
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase pr_attachment for %s run",
        role,
        extra={"phase": "pr_attachment", "role": role, "backend": backend},
    )
    try:
        tree = bounds.create_tree(
            tree_spec, source_repo=source_repo, github_url=github_url
        )
    except (ValueError, execrun.ExecError, OSError) as exc:
        # FileExistsError ⊂ OSError: with a per-Run UUID a collision is a real error.
        raise _refusal(
            f"existing-PR tree attachment failed: {exc}",
            exc=exc,
            pr=attach.number,
            duration_ms=_elapsed_ms(create_start),
        ) from exc

    logcontext.bind(tree=tree.path, agent=f"pr{attach.number}", pr=attach.number)
    create_ms = _elapsed_ms(create_start)
    logger.info(
        "spawn subagent: existing-PR write tree attached on %s for PR #%d in %dms",
        tree.branch,
        attach.number,
        create_ms,
        extra={
            "branch": tree.branch,
            "base": tree.base,
            "pr": attach.number,
            "duration_ms": create_ms,
        },
    )

    current = _audit_existing_pr_head(
        bounds.pr_for_head(tree.branch, cwd=tree.path),
        expected_pr=attach,
        branch=tree.branch,
    )

    pixi_provisioned = pixienv.has_default_env(tree.path)
    env_prefix = Path(tree.path).joinpath(*pixienv.DEFAULT_ENV_DIR)
    work_env = workenv.resolve_existing_pr_write_env(
        repo=repo,
        tree_path=tree.path,
        branch=tree.branch,
        base=tree.base,
        pixi_provisioned=pixi_provisioned,
        env_identity=(
            _read_optional_env_identity(env_prefix) if pixi_provisioned else None
        ),
    )
    logger.info(
        "spawn subagent: work env resolved — %s routing for the existing-PR write tree",
        work_env.routing.value,
        extra=workenv.resolution_record(
            work_env,
            boundary="spawn.existing-pr-write",
            role=role,
            extra={"pr": attach.number},
        ),
    )

    task = launch.shepherd_task(
        pr_number=attach.number,
        branch=tree.branch,
        base_branch=attach.base_ref,
    )
    cmd = launch.route_argv(adapter.build_command(task, role, cwd=tree.path), work_env)
    try:
        _run_child(cmd, tree=tree, adapter=adapter, bounds=bounds, role=role)
    except SpawnError as exc:
        note = salvage_note(tree.path, bounds)
        if note is None:
            raise
        raise SpawnError(f"{exc}\n{note}") from exc

    result = SpawnResult(
        tree=tree.path,
        branch=tree.branch,
        base=tree.base,
        role=role,
        backend=backend,
        pr=current.number,
        pr_state=current.state,
        pr_is_draft=current.is_draft,
    )
    _log_spawned(result)
    return result


def _launch_reviewer(
    *,
    repo: identity.Repo,
    branch: str,
    source_repo: str,
    role: str,
    adapter: backends.BackendAdapter,
    bounds: Boundaries,
) -> SpawnResult:
    """Reviewer tail: resolve the PR, then delegate capture + post to the service."""
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase review_service for reviewer run",
        extra={
            "phase": "review_service",
            "role": role,
            "backend": adapter.name,
        },
    )
    pr = bounds.pr_for_head(branch, cwd=source_repo)
    if pr is None:
        raise _refusal(
            f"review branch {branch!r} has no pull request; refused before Tree "
            "provisioning or backend launch.",
            branch=branch,
        )
    if isinstance(pr, gh.UnknownPr):
        raise _refusal(
            f"could not determine the pull request for review branch {branch!r}; "
            "refused before Tree provisioning or backend launch.",
            branch=branch,
        )
    if pr.state != "OPEN":
        raise _refusal(
            f"pull request #{pr.number} on review branch {branch!r} is {pr.state}, "
            "not OPEN; refused before Tree provisioning or backend launch.",
            branch=branch,
            pr=pr.number,
        )

    # The review service clones under this naming, so the reported `tree` is real.
    naming = new_tree_naming(agent_backend.by_name(adapter.name).binary)
    tree_path = str(readonly_plan(repo=repo, branch=branch, **naming).dir)
    logcontext.bind(
        tree=tree_path, agent=naming["tree_id"], pr=pr.number, repo=repo.slug
    )
    logger.info(
        "spawn subagent: delegating reviewer run on %s to the captured review service",
        branch,
        extra={"branch": branch, "base": f"origin/{branch}", "pr": pr.number},
    )

    review_backend = agent_backend.by_name(adapter.name)
    events.emit(
        logger,
        "agent.spawned",
        "spawn subagent: launching %s captured reviewer in the review service",
        adapter.name,
        extra={"backend": adapter.name, "role": role, "cwd": tree_path},
    )
    events.emit(
        logger,
        "agent.phase",
        "spawn subagent: phase agent_running for %s run",
        role,
        extra={"phase": "agent_running", "backend": adapter.name, "role": role},
    )
    review_start = time.monotonic()
    try:
        bounds.run_review(
            review_backend,
            PrId(repo=repo, number=pr.number),
            run_id=None,
            review_tree_naming=naming,
        )
    except Exception as exc:  # noqa: BLE001 - normalize the product boundary
        raise _refusal(
            f"captured review service failed for PR #{pr.number}: {exc}",
            exc=exc,
            branch=branch,
            pr=pr.number,
            backend=adapter.name,
            duration_ms=_elapsed_ms(review_start),
        ) from exc
    review_ms = _elapsed_ms(review_start)
    events.emit(
        logger,
        "agent.done",
        "spawn subagent: %s captured reviewer settled in %dms",
        adapter.name,
        review_ms,
        extra={"backend": adapter.name, "rc": 0, "duration_ms": review_ms},
    )

    result = SpawnResult(
        tree=tree_path,
        branch=branch,
        base=f"origin/{branch}",
        role=role,
        backend=adapter.name,
    )
    _log_spawned(result)
    return result


def _log_spawned(result: SpawnResult) -> None:
    logger.info(
        "spawn subagent: SPAWNED %s run on %s",
        result.role,
        result.branch,
        extra=dict(result.to_dict()),
    )
