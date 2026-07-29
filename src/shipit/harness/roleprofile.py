"""``harness/roleprofile`` — the fixed, total registry of each Role's structural run shape.

See docs/adr/0047-role-profiles-and-work-env-are-not-consumer-config.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

from .role import Role


class LaunchContext(StrEnum):
    """The closed set of ways a Run can be launched."""

    HOST_SESSION = "host-session"
    DETACHED = "detached"
    NATIVE_SUBAGENT = "native-subagent"


class ResultChannel(StrEnum):
    """The closed set of channels a Run's result travels back through."""

    ORCHESTRATION_SESSION = "orchestration-session"
    DRAFT_PR = "draft-pr"
    EXISTING_PR_ROUNDS = "existing-pr-rounds"
    POSTED_REVIEW = "posted-review"
    COORDINATOR_REPORT = "coordinator-report"


@dataclass(frozen=True)
class SessionTree:
    """The coordinator's checkout: an ephemeral, never-shared per-session write Tree."""

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = True
    attaches_to_existing_pr: ClassVar[bool] = False


@dataclass(frozen=True)
class NewWriteTree:
    """The implementer's checkout: a new write Tree on a branch cut from the intended base."""

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = True
    attaches_to_existing_pr: ClassVar[bool] = False


@dataclass(frozen=True)
class ExistingPrWriteTree:
    """The shepherd's checkout: a write Tree attached to an existing PR head across rounds."""

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = True
    attaches_to_existing_pr: ClassVar[bool] = True


@dataclass(frozen=True)
class PerRunReadOnlyTree:
    """The reviewer's checkout: a per-Run, ``chmod``'d read-only Tree pinned to a PR head."""

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = False
    attaches_to_existing_pr: ClassVar[bool] = True


@dataclass(frozen=True)
class AmbientWorkingDir:
    """The explorer's checkout: the ambient WorkingDir — no Tree, ever."""

    tree_backed: ClassVar[bool] = False
    writable: ClassVar[bool] = False
    attaches_to_existing_pr: ClassVar[bool] = False


CheckoutStrategy = (
    SessionTree
    | NewWriteTree
    | ExistingPrWriteTree
    | PerRunReadOnlyTree
    | AmbientWorkingDir
)


@dataclass(frozen=True)
class EnforcementPosture:
    """A role's required capabilities, by operation and resource — a policy input, not a sandbox."""

    checkout_mutation: bool
    command_execution: bool
    network_access: bool
    github_mutation: bool
    scratch_writes: bool
    #: May the Run AUTHOR code itself? Orthogonal to ``checkout_mutation``: the
    #: coordinator commits docs and config yet must not author code.
    code_authorship: bool


@dataclass(frozen=True)
class RoleProfile:
    """One fixed Role's structural execution shape; behavioral prose lives in its Lex definition."""

    role: Role
    checkout: CheckoutStrategy
    enforcement: EnforcementPosture
    generates_agent_def: bool
    has_brief_template: bool
    launch_contexts: frozenset[LaunchContext]
    result_channel: ResultChannel


#: A full-trust write posture — the roles that author code.
_WRITE_POSTURE = EnforcementPosture(
    checkout_mutation=True,
    command_execution=True,
    network_access=True,
    github_mutation=True,
    scratch_writes=True,
    code_authorship=True,
)

#: Full write EXCEPT code authorship — the pairing the edit guard fires on.
_ORCHESTRATOR_POSTURE = EnforcementPosture(
    checkout_mutation=True,
    command_execution=True,
    network_access=True,
    github_mutation=True,
    scratch_writes=True,
    code_authorship=False,
)

#: The registry — TOTAL over the closed Role vocabulary, read-only at runtime.
PROFILES: Mapping[Role, RoleProfile] = MappingProxyType(
    {
        Role.COORDINATOR: RoleProfile(
            role=Role.COORDINATOR,
            checkout=SessionTree(),
            enforcement=_ORCHESTRATOR_POSTURE,
            generates_agent_def=False,
            has_brief_template=False,
            launch_contexts=frozenset({LaunchContext.HOST_SESSION}),
            result_channel=ResultChannel.ORCHESTRATION_SESSION,
        ),
        Role.IMPLEMENTER: RoleProfile(
            role=Role.IMPLEMENTER,
            checkout=NewWriteTree(),
            enforcement=_WRITE_POSTURE,
            generates_agent_def=True,
            has_brief_template=True,
            launch_contexts=frozenset(
                {LaunchContext.DETACHED, LaunchContext.NATIVE_SUBAGENT}
            ),
            result_channel=ResultChannel.DRAFT_PR,
        ),
        Role.SHEPHERD: RoleProfile(
            role=Role.SHEPHERD,
            checkout=ExistingPrWriteTree(),
            enforcement=_WRITE_POSTURE,
            generates_agent_def=True,
            has_brief_template=True,
            launch_contexts=frozenset(
                {LaunchContext.DETACHED, LaunchContext.NATIVE_SUBAGENT}
            ),
            result_channel=ResultChannel.EXISTING_PR_ROUNDS,
        ),
        Role.EXPLORER: RoleProfile(
            role=Role.EXPLORER,
            checkout=AmbientWorkingDir(),
            enforcement=EnforcementPosture(
                checkout_mutation=False,
                command_execution=True,
                network_access=False,
                github_mutation=False,
                scratch_writes=False,
                code_authorship=False,
            ),
            generates_agent_def=True,
            has_brief_template=False,
            launch_contexts=frozenset({LaunchContext.NATIVE_SUBAGENT}),
            result_channel=ResultChannel.COORDINATOR_REPORT,
        ),
        Role.REVIEWER: RoleProfile(
            role=Role.REVIEWER,
            checkout=PerRunReadOnlyTree(),
            enforcement=EnforcementPosture(
                checkout_mutation=False,
                command_execution=True,
                network_access=True,
                github_mutation=True,
                scratch_writes=True,
                code_authorship=False,
            ),
            generates_agent_def=True,
            has_brief_template=False,
            launch_contexts=frozenset({LaunchContext.DETACHED}),
            result_channel=ResultChannel.POSTED_REVIEW,
        ),
    }
)


class RoleValidationError(ValueError):
    """A strict-boundary role refusal, minted BEFORE any provisioning or launch."""


def _known_roles() -> str:
    return ", ".join(role.value for role in Role)


def profile_for(role: Role) -> RoleProfile:
    return PROFILES[role]


def roles_with_checkout_strategy(checkout_type: type) -> tuple[Role, ...]:
    return tuple(
        role
        for role, profile in PROFILES.items()
        if isinstance(profile.checkout, checkout_type)
    )


def delegates_code_authorship(role: Role) -> bool:
    """True iff ``role`` may mutate its checkout but must NOT author code itself."""
    posture = PROFILES[role].enforcement
    return posture.checkout_mutation and not posture.code_authorship


def parse_role(name: str) -> Role:
    """Parse a role input STRICTLY: whitespace/case normalized, but never a fallback."""
    normalized = (name or "").strip().lower()
    if not normalized:
        raise RoleValidationError(
            f"empty role — roles are a closed registry (known: {_known_roles()})."
        )
    try:
        return Role(normalized)
    except ValueError:
        raise RoleValidationError(
            f"unknown role {name!r} — roles are a closed registry "
            f"(known: {_known_roles()}); arbitrary role strings are refused."
        ) from None


def validate_spawn(name: str, context: LaunchContext) -> RoleProfile:
    """The spawn preflight, run BEFORE any provisioning: the profile, or a refusal."""
    try:
        role = parse_role(name)
    except RoleValidationError as exc:
        # Preserve parse_role's diagnosis (empty vs unknown) and append the context.
        raise RoleValidationError(
            f"{exc} Refused for a {context.value} launch before any Tree is "
            "provisioned or a backend launched."
        ) from None
    profile = PROFILES[role]
    if context not in profile.launch_contexts:
        supported = ", ".join(sorted(c.value for c in profile.launch_contexts))
        raise RoleValidationError(
            f"role {role.value!r} does not support a {context.value} launch "
            f"(supported: {supported}); refused before any Tree is provisioned "
            "or a backend launched."
        )
    return profile
