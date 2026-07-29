"""The Work Env value: where, and with which activation, work runs.

A resolved composition of existing value objects, pure over supplied facts. One
boundary-specific constructor per boundary; nothing here launches or probes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .harness.roleprofile import (
    AmbientWorkingDir,
    CheckoutStrategy,
    ExistingPrWriteTree,
    NewWriteTree,
    PerRunReadOnlyTree,
    SessionTree,
)
from .identity import Repo, Revision, Sha, WorkingDir
from .pixienv import Activation, EnvIdentity


class ExecutionRouting(StrEnum):
    """The closed set of launch-routing decisions a Work Env can carry; each member names an EXISTING mechanism, and ``AMBIENT`` is explicit absence rather than a fallback."""

    PIXI_RUN = "pixi-run"
    ACTIVATION_SNAPSHOT = "activation-snapshot"
    AMBIENT = "ambient"


@dataclass(frozen=True)
class TreeProvenance:
    """What a Shipit-provisioned Tree adds BEYOND its WorkingDir: the branch it sits on and the base it was cut from (``None`` for a branch-pinned read-only Tree). No path field — the WorkingDir owns location."""

    branch: str
    base: str | None


@dataclass(frozen=True)
class WorkEnv:
    """The resolved execution context: where and with which activation work runs. ``tree`` is ``None`` for an ambient WorkingDir; ``activation`` and ``env_identity`` are pixi's own values when present and honestly ``None`` otherwise."""

    working_dir: WorkingDir
    tree: TreeProvenance | None
    checkout: CheckoutStrategy
    activation: Activation | None
    env_identity: EnvIdentity | None
    routing: ExecutionRouting


def checkout_strategy_name(checkout: CheckoutStrategy) -> str:
    if isinstance(checkout, SessionTree):
        return "session-tree"
    if isinstance(checkout, NewWriteTree):
        return "new-write-tree"
    if isinstance(checkout, ExistingPrWriteTree):
        return "existing-pr-write-tree"
    if isinstance(checkout, PerRunReadOnlyTree):
        return "per-run-read-only-tree"
    if isinstance(checkout, AmbientWorkingDir):
        return "ambient-working-dir"
    raise TypeError(f"unknown checkout strategy {checkout!r}")


def _repo_slug(repo: Repo) -> str:
    """Project a repo identity to ``owner/name`` for logs, tolerating a raw-string owner so adding a record cannot change behavior under test."""
    try:
        return repo.slug
    except AttributeError:
        return f"{repo.owner}/{repo.name}"


def _resolution_record(
    *,
    boundary: str,
    working_dir: str,
    working_dir_repo: str | None,
    checkout_strategy: str,
    routing: str,
    working_dir_branch: str | None = None,
    working_dir_commit: str | None = None,
    role: str | None = None,
    lane: str | None = None,
    pixi_environment_name: str | None = None,
    pixi_environment_lock_hash: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "work_env_boundary": boundary,
        "working_dir": working_dir,
        "working_dir_repo": working_dir_repo,
        "working_dir_branch": working_dir_branch,
        "working_dir_commit": working_dir_commit,
        "checkout_strategy": checkout_strategy,
        "routing": routing,
        "role": role,
        "lane": lane,
        "pixi_environment_name": pixi_environment_name,
        "pixi_environment_lock_hash": pixi_environment_lock_hash,
    }
    if extra:
        data.update(extra)
    return {name: value for name, value in data.items() if value is not None}


def resolution_record(
    work_env: WorkEnv,
    *,
    boundary: str,
    role: str | None = None,
    lane: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flat structured fields for one resolved Work Env decision — a projection only, absent-not-null, with pixi activation reduced to a presence marker."""
    revision = work_env.working_dir.revision
    boundary_extra: dict[str, Any] = {}
    if work_env.tree is not None:
        boundary_extra.update(
            {
                "tree_branch": work_env.tree.branch,
                "tree_base": work_env.tree.base,
            }
        )
    if work_env.activation is not None:
        boundary_extra["pixi_activation"] = "present"
    if extra:
        boundary_extra.update(extra)
    return _resolution_record(
        boundary=boundary,
        working_dir=work_env.working_dir.path,
        working_dir_repo=_repo_slug(work_env.working_dir.repo),
        working_dir_branch=revision.branch,
        working_dir_commit=str(revision.commit) if revision.commit else None,
        checkout_strategy=checkout_strategy_name(work_env.checkout),
        routing=work_env.routing.value,
        role=role,
        lane=lane,
        pixi_environment_name=(
            work_env.env_identity.environment_name if work_env.env_identity else None
        ),
        pixi_environment_lock_hash=(
            work_env.env_identity.environment_lock_file_hash
            if work_env.env_identity
            else None
        ),
        extra=boundary_extra,
    )


def ci_lane_resolution_record(
    *,
    working_dir: str,
    repo: str | None,
    lane: str,
    pixi_environment_name: str,
    ci_event: str,
    runner: str,
    required: bool,
) -> dict[str, Any]:
    """Resolution evidence for a CI Lane planned in the existing checkout; CI does not execute through a :class:`WorkEnv`, so its planner supplies the facts directly."""
    return _resolution_record(
        boundary="ci.lane-job",
        working_dir=working_dir,
        working_dir_repo=repo,
        checkout_strategy="direct-checkout",
        routing=ExecutionRouting.PIXI_RUN.value,
        lane=lane,
        pixi_environment_name=pixi_environment_name,
        extra={"ci_event": ci_event, "runner": runner, "required": required},
    )


def _resolve_write_env(
    *,
    repo: Repo,
    tree_path: str,
    branch: str,
    base: str,
    checkout: CheckoutStrategy,
    pixi_provisioned: bool,
    env_identity: EnvIdentity | None = None,
) -> WorkEnv:
    """Resolve a writable Tree Work Env; the caller supplies the checkout strategy. Routing follows ``pixi_provisioned``, ``activation`` is always ``None``, and an ``env_identity`` without provisioning raises :class:`ValueError`."""
    if env_identity is not None and not pixi_provisioned:
        raise ValueError(
            "incoherent write-run facts: an EnvIdentity was supplied for a tree "
            "with no provisioned pixi env (pixi_provisioned=False); the identity "
            "is read from INSIDE the provisioned env, so these facts cannot both "
            "be true."
        )
    working_dir = WorkingDir(
        path=tree_path,
        repo=repo,
        revision=Revision(branch=branch, commit=None),
    )
    return WorkEnv(
        working_dir=working_dir,
        tree=TreeProvenance(branch=branch, base=base),
        checkout=checkout,
        activation=None,
        env_identity=env_identity,
        routing=(
            ExecutionRouting.PIXI_RUN if pixi_provisioned else ExecutionRouting.AMBIENT
        ),
    )


def resolve_write_run_env(
    *,
    repo: Repo,
    tree_path: str,
    branch: str,
    base: str,
    pixi_provisioned: bool,
    env_identity: EnvIdentity | None = None,
) -> WorkEnv:
    return _resolve_write_env(
        repo=repo,
        tree_path=tree_path,
        branch=branch,
        base=base,
        checkout=NewWriteTree(),
        pixi_provisioned=pixi_provisioned,
        env_identity=env_identity,
    )


def resolve_existing_pr_write_env(
    *,
    repo: Repo,
    tree_path: str,
    branch: str,
    base: str,
    pixi_provisioned: bool,
    env_identity: EnvIdentity | None = None,
) -> WorkEnv:
    """Resolve the Work Env for a shepherd's writable existing-PR attachment — the write-Tree posture with a resumable checkout strategy."""
    return _resolve_write_env(
        repo=repo,
        tree_path=tree_path,
        branch=branch,
        base=base,
        checkout=ExistingPrWriteTree(),
        pixi_provisioned=pixi_provisioned,
        env_identity=env_identity,
    )


def resolve_session_env(
    *,
    repo: Repo,
    tree_path: str,
    branch: str,
    base: str,
    activation: Activation | None,
    env_identity: EnvIdentity | None = None,
) -> WorkEnv:
    """Resolve the Work Env for the coordinator's ephemeral session Tree; an ``env_identity`` without an ``activation`` snapshot is incoherent and raises :class:`ValueError`."""
    if env_identity is not None and activation is None:
        raise ValueError(
            "incoherent session facts: an EnvIdentity was supplied without an "
            "Activation snapshot; a non-pixi or unactivated session must carry "
            "neither."
        )
    return WorkEnv(
        working_dir=WorkingDir(
            path=tree_path,
            repo=repo,
            revision=Revision(branch=branch, commit=None),
        ),
        tree=TreeProvenance(branch=branch, base=base),
        checkout=SessionTree(),
        activation=activation,
        env_identity=env_identity,
        routing=(
            ExecutionRouting.ACTIVATION_SNAPSHOT
            if activation is not None
            else ExecutionRouting.AMBIENT
        ),
    )


def resolve_readonly_review_env(
    *,
    repo: Repo,
    tree_path: str,
    branch: str,
    commit: Sha | None = None,
) -> WorkEnv:
    """Resolve the Work Env for a reviewer per-Run read-only Tree: branch-pinned, deliberately unprovisioned for pixi, hence ambient routing."""
    return WorkEnv(
        working_dir=WorkingDir(
            path=tree_path,
            repo=repo,
            revision=Revision(branch=branch, commit=commit),
        ),
        tree=TreeProvenance(branch=branch, base=None),
        checkout=PerRunReadOnlyTree(),
        activation=None,
        env_identity=None,
        routing=ExecutionRouting.AMBIENT,
    )


def resolve_ambient_env(
    *,
    repo: Repo,
    path: str,
    branch: str | None = None,
    commit: Sha | None = None,
) -> WorkEnv:
    """Resolve the Work Env for an explorer's ambient WorkingDir — no Tree, no activation, no environment identity."""
    return WorkEnv(
        working_dir=WorkingDir(
            path=path,
            repo=repo,
            revision=Revision(branch=branch, commit=commit),
        ),
        tree=None,
        checkout=AmbientWorkingDir(),
        activation=None,
        env_identity=None,
        routing=ExecutionRouting.AMBIENT,
    )
