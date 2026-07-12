"""``workenv`` — the Work Env value: where, and with which activation, work runs.

RPE01-WS05, governed by ``docs/spec/role-profiles-work-env.md`` (§Proposed
Shape, §Design Decisions "Work Env composes existing value objects"): a
**Work Env** is a small RESOLVED value over the existing abstractions — never
another executor. It composes:

- a :class:`~shipit.identity.WorkingDir` — the ONE checkout identity (path,
  repo, revision). A Tree *has* a WorkingDir (ADR-0024); Work Env never mints
  a parallel checkout identity;
- optional :class:`TreeProvenance` — whether Shipit provisioned the checkout
  and what the Tree adds BEYOND its WorkingDir (the branch it was cut onto and
  the base it was cut from). Deliberately no path field: the WorkingDir owns
  location, provenance only annotates it;
- the structured checkout strategy (:data:`shipit.harness.roleprofile.CheckoutStrategy`
  — the Role Profile registry's closed value, RPE01-WS01), naming how this
  checkout was allocated and attached;
- optional pixi :class:`~shipit.pixienv.Activation` and
  :class:`~shipit.pixienv.EnvIdentity` — BORROWED through the existing pixi
  adapter's value objects (ADR-0022), never re-derived. Absence is explicit
  and valid (a non-pixi repo, a reviewer's unprovisioned read-only Tree);
- an :class:`ExecutionRouting` decision — which EXISTING launch mechanism the
  caller should use. Work Env *carries* the decision; the owners keep their
  jobs: Exec stays the only external-process seam (ADR-0028), the pixi adapter
  keeps run-wrapping and activation (ADR-0022), Tool adapters keep command
  knowledge (ADR-0039).

Everything here is PURE and deterministic over supplied facts (the spec's
resolution invariant): no process launch, filesystem mutation, provisioning,
or network work. Expensive facts — "does this Tree carry a provisioned pixi
env?", the on-disk :class:`~shipit.pixienv.EnvIdentity` — are supplied by the
boundary that already obtained them (the spawn write tail probes via
:func:`shipit.pixienv.has_default_env` / :func:`shipit.pixienv.read_env_identity`
at its own effectful seam and hands the results in).

Resolution is boundary-specific by design (spec §Design Decisions): this
module exposes per-boundary constructors behind the one common value rather
than one oversized universal resolver. WS05 landed the write-Run walking
skeleton (:func:`resolve_write_run_env`, consumed by
:func:`shipit.spawn.subagent._launch_write` and routed by
:func:`shipit.spawn.launch.route_argv`). WS04 adds the sibling existing-PR
write resolver for shepherd attachment (:func:`resolve_existing_pr_write_env`)
with the same pixi routing contract but the shepherd checkout strategy;
RPE01-WS06 adds the coordinator session Tree, reviewer shared read-only Tree,
and explorer ambient WorkingDir boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .harness.roleprofile import (
    AmbientWorkingDir,
    CheckoutStrategy,
    ExistingPrWriteTree,
    NewWriteTree,
    SessionTree,
    SharedReadOnlyTree,
)
from .identity import Repo, Revision, Sha, WorkingDir
from .pixienv import Activation, EnvIdentity


class ExecutionRouting(StrEnum):
    """The closed set of launch-routing decisions a Work Env can carry.

    Each member names an EXISTING mechanism — Work Env selects, it never
    executes (spec §Proposed Shape):

    - ``PIXI_RUN`` — wrap the child argv through the checkout's own pixi env
      (:func:`shipit.pixienv.run_argv`, the ADR-0019-amendment write-Run
      routing); pixi owns activation inside the child.
    - ``ACTIVATION_SNAPSHOT`` — consume a captured ``pixi shell-hook --json``
      snapshot (the coordinator's borrow, :mod:`shipit.harness.activation`);
      resolved by the session boundary in RPE01-WS06.
    - ``AMBIENT`` — launch bare: the checkout carries no pixi env (a non-pixi
      repo, a reviewer's unprovisioned read-only Tree), so the child keeps the
      ambient tools it inherited. Explicit absence, not a fallback.
    """

    PIXI_RUN = "pixi-run"
    ACTIVATION_SNAPSHOT = "activation-snapshot"
    AMBIENT = "ambient"


@dataclass(frozen=True)
class TreeProvenance:
    """What a Shipit-provisioned Tree adds BEYOND its WorkingDir — never a rival to it.

    ``branch`` is the branch the Tree is checked out on. ``base`` is the ref a
    Tree branch was cut from when such a ref exists (for write and session
    Trees, e.g. ``origin/E/umbrella``). It is ``None`` for a shared read-only
    reviewer Tree: that checkout is pinned to an existing PR-head branch and
    does not cut a new branch from a base. There is deliberately NO path field:
    the composed :class:`~shipit.identity.WorkingDir` is the one checkout
    identity (spec: "Tree provenance and WorkingDir identity compose rather
    than duplicate one another"), so provenance can never drift from the
    location it annotates.
    """

    branch: str
    base: str | None


@dataclass(frozen=True)
class WorkEnv:
    """The resolved execution context: WHERE and WITH WHICH ACTIVATION work runs.

    A thin frozen composition of the existing value objects (spec §Proposed
    Shape) — a description the existing launch/planning paths consume, not a
    runner. ``tree`` is ``None`` for a Main checkout / ambient WorkingDir
    (Shipit did not provision it). ``activation`` and ``env_identity`` are
    pixi's OWN value objects when present (ADR-0022's borrow) and honestly
    ``None`` when the context has no pixi env — never a fabricated stand-in;
    Work Env carries neither a PATH computation nor an environment UUID (pixi
    provides neither).
    """

    working_dir: WorkingDir
    tree: TreeProvenance | None
    checkout: CheckoutStrategy
    activation: Activation | None
    env_identity: EnvIdentity | None
    routing: ExecutionRouting


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
    """Resolve a writable Tree Work Env; callers supply the checkout strategy.

    The composed :class:`~shipit.identity.WorkingDir` carries the Tree's path,
    repo, and branch; its revision commit is ``None`` — honest best-effort
    (:class:`~shipit.identity.Revision`'s contract), since the boundary
    supplied no HEAD read and resolution must not add one.

    Routing follows the provisioning fact, mirroring the ADR-0019-amendment
    gate: a provisioned write Tree routes ``PIXI_RUN`` (the child launches
    through the existing pixi-run wrapping and environment scrub); an
    unprovisioned one — a non-pixi repo — is honestly ``AMBIENT`` with no
    activation and no env identity, preserving the existing bare-launch
    behavior. ``activation`` is always ``None`` for a write Run: ``pixi run``
    computes activation inside the child, so there is no snapshot to borrow —
    absent-not-fabricated. An ``env_identity`` supplied WITHOUT
    ``pixi_provisioned`` is contradictory (an identity file inside an env that
    does not exist) and raises :class:`ValueError` loudly rather than resolving
    an incoherent Work Env.
    """
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
    """Resolve the Work Env for a NEW write Run's freshly materialized Tree.

    The write-Run boundary constructor (RPE01-WS05's walking skeleton): pure
    and deterministic over the facts the spawn write tail already holds — the
    Tree's coordinates (``tree_path``/``branch``/``base``, straight from
    :class:`~shipit.tree.create.Tree`), the checkout's :class:`~shipit.identity.Repo`,
    and the two pixi facts the boundary probed at its own effectful seam:
    ``pixi_provisioned`` (:func:`shipit.pixienv.has_default_env` — the same
    sentinel every routing site keys on) and the optional on-disk
    ``env_identity`` (:func:`shipit.pixienv.read_env_identity`). No probe,
    process, or provisioning happens HERE.

    Routing follows the provisioning fact, mirroring the ADR-0019-amendment
    gate: a provisioned write Tree routes ``PIXI_RUN`` (the child launches
    through the existing pixi-run wrapping and environment scrub); an
    unprovisioned one — a non-pixi repo — is honestly ``AMBIENT`` with no
    activation and no env identity, preserving the existing bare-launch
    behavior. ``activation`` is always ``None`` for a write Run: ``pixi run``
    computes activation inside the child, so there is no snapshot to borrow —
    absent-not-fabricated.
    """
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
    """Resolve the Work Env for a shepherd's writable existing-PR attachment.

    This is the same write-Tree execution posture as :func:`resolve_write_run_env`
    with a different checkout strategy: the Tree is attached to an existing PR
    head and may be resumed across review rounds. The resolver remains pure over
    supplied facts; the spawn shepherd tail owns PR resolution, Tree create/reuse,
    and any refresh before calling here.
    """
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
    """Resolve the Work Env for the coordinator's ephemeral session Tree.

    Claude and Codex reach the session Tree through different host seams
    (``claude --worktree`` vs ``shipit session codex``), but once the boundary
    supplies the Tree coordinates and optional pixi activation snapshot, the
    resolved Work Env is the same: a :class:`SessionTree` with
    :class:`ExecutionRouting.ACTIVATION_SNAPSHOT` when an existing
    ``pixi shell-hook --json`` snapshot was captured, or honest
    :class:`ExecutionRouting.AMBIENT` absence for a non-pixi checkout.

    The activation is BORROWED from pixi's own :class:`Activation` value
    object. This resolver never computes PATH, shells out, detects manifests,
    or provisions anything. An ``env_identity`` without an activation snapshot
    is incoherent for this boundary and is refused: a non-pixi session is
    represented by both values being absent.
    """
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
    """Resolve the Work Env for a reviewer shared read-only Tree.

    A reviewer Tree is branch-pinned and Shipit-provisioned, but deliberately
    unprovisioned for pixi (ADR-0018): no ``.treeinclude``, no pixi env, no
    write-run activation. The Work Env therefore records Tree provenance
    (branch, with no cut-from base), a :class:`SharedReadOnlyTree` checkout
    strategy, absent ``activation``/``env_identity``, and
    :class:`ExecutionRouting.AMBIENT` so the existing reviewer launcher keeps
    using ambient read tools over the chmod'd checkout guard.
    """
    return WorkEnv(
        working_dir=WorkingDir(
            path=tree_path,
            repo=repo,
            revision=Revision(branch=branch, commit=commit),
        ),
        tree=TreeProvenance(branch=branch, base=None),
        checkout=SharedReadOnlyTree(),
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
    """Resolve the Work Env for an explorer's ambient WorkingDir.

    Explorer work is ambient by design: no provisioned Tree, no detached write
    path, no pixi activation supplied by Shipit, and no environment identity to
    fabricate. The caller supplies only the already-known checkout identity
    facts; resolution is a pure value construction over them.
    """
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
