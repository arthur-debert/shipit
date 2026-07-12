"""Role Profile registry — the structural answer to "how may this Role run?".

RPE01-WS01, governed by ``docs/spec/role-profiles-work-env.md`` and ADR-0047:
one fixed, Shipit-owned, EXHAUSTIVE mapping from the closed :class:`Role`
vocabulary to each role's structural execution shape. A profile is structural
only — checkout strategy, enforcement posture, generated/brief surfaces,
supported launch contexts, result channel; behavioral prose stays in the Lex
Role definitions (ADR-0011). There is deliberately NO consumer configuration
surface (ADR-0047): the registry is a module-level frozen value, not config.

The checkout strategy is a STRUCTURED closed value, not the historical flat
``session | write | read-only | ambient`` token list (which mixed allocation,
attachment, lifetime, and mutation): one shape per role — the coordinator's
ephemeral session Tree (ADR-0027), the implementer's new write Tree + branch,
the shepherd's write attachment to an EXISTING PR (ADR-0035), the reviewer's
shared read-only Tree pinned to a PR head (ADR-0018), and the explorer's
ambient WorkingDir with no Tree at all. Enforcement posture is
capability-shaped (per operation and resource), never a single mutation
boolean — a reviewer posts its review (GitHub mutation) while its checkout
stays immutable.

Two role-parsing boundaries exist ON PURPOSE and must not converge:

- :func:`parse_role` / :func:`validate_spawn` here are the STRICT public and
  programmatic boundary — an unknown role or an unsupported role/launch pair
  is a :class:`RoleValidationError` raised BEFORE any Tree provisioning or
  backend launch, naming the role and the requested context.
- :func:`shipit.harness.role.resolve_role` is the deliberately LENIENT native
  hook boundary — an unknown non-empty native subagent identity stays an
  unknown worker (never the coordinator), because the hook must govern
  whatever identity the host hands it. That leniency never makes the unknown
  identity spawnable here.

Everything in this module is pure and deterministic: plain value lookups over
frozen data — no filesystem mutation, process launch, provisioning, or network
work (the spec's performance/purity invariant). Consumers with effects (spawn,
prompts, enforcement) log their own decisions at their own seams.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

from .role import Role


class LaunchContext(StrEnum):
    """The closed set of ways a Run can be launched today.

    ``HOST_SESSION`` is the human-facing top-level session a host starts
    (the coordinator's only context). ``DETACHED`` is ``shipit spawn
    subagent`` — a headless backend child rooted in a shipit-provisioned
    Tree (ADR-0019). ``NATIVE_SUBAGENT`` is a host-native in-session spawn
    (e.g. the Claude Code Agent tool) governed through the hook boundary.
    """

    HOST_SESSION = "host-session"
    DETACHED = "detached"
    NATIVE_SUBAGENT = "native-subagent"


class ResultChannel(StrEnum):
    """The closed set of channels a Run's result travels back through."""

    #: The coordinator's result IS the human-facing orchestration session.
    ORCHESTRATION_SESSION = "orchestration-session"
    #: One verified draft PR opened from the Run's branch (ADR-0019 §6).
    DRAFT_PR = "draft-pr"
    #: Commits + resolved review threads on the EXISTING PR, across rounds.
    EXISTING_PR_ROUNDS = "existing-pr-rounds"
    #: A captured structured review posted through the existing PR (ADR-0018).
    POSTED_REVIEW = "posted-review"
    #: A report handed back to the coordinator in-session; nothing lands.
    COORDINATOR_REPORT = "coordinator-report"


# ---------------------------------------------------------------------------
# Checkout strategy — a structured closed value, one shape per allocation +
# attachment pairing. The class-level axes let a consumer ask the orthogonal
# questions (is a Tree materialized? may the checkout mutate? does it attach
# to an existing PR?) without matching shape tokens — the exact confusion the
# historical flat enum caused (spec §Design Decisions).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionTree:
    """The coordinator's checkout: an ephemeral per-session write Tree (ADR-0027).

    Writable like an implementer Tree but with session lifetime and branch
    behavior — minted at launch, switched to whatever branch the session
    discovers it needs, never shared between concurrent sessions.
    """

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = True
    attaches_to_existing_pr: ClassVar[bool] = False


@dataclass(frozen=True)
class NewWriteTree:
    """The implementer's checkout: a new write Tree on a freshly cut branch.

    Allocation AND attachment are new: the branch is cut from the intended
    base (``origin/main`` or the epic umbrella) and the Run's draft-PR
    handshake creates the PR the coordinator drives (ADR-0019 §6).
    """

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = True
    attaches_to_existing_pr: ClassVar[bool] = False


@dataclass(frozen=True)
class ExistingPrWriteTree:
    """The shepherd's checkout: a write Tree ATTACHED to an existing PR head.

    Writable like the implementer's, but it attaches to the PR a prior Run
    opened and persists across review rounds (ADR-0035) — never a new branch,
    never a second draft-PR handshake.
    """

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = True
    attaches_to_existing_pr: ClassVar[bool] = True


@dataclass(frozen=True)
class SharedReadOnlyTree:
    """The reviewer's checkout: the shared read-only Tree pinned to a PR head.

    Shared per ``(repo, branch)`` (ADR-0018), checked out read-only and left
    unprovisioned — branch pinning, not read-only behavior alone, is why a
    Tree exists at all.
    """

    tree_backed: ClassVar[bool] = True
    writable: ClassVar[bool] = False
    attaches_to_existing_pr: ClassVar[bool] = True


@dataclass(frozen=True)
class AmbientWorkingDir:
    """The explorer's checkout: the ambient WorkingDir — no Tree, ever.

    Open-ended investigation stays cheap and cannot accidentally become a
    write Run; a detached spawn (which would mint a Tree) is refused at
    preflight, not merely discouraged.
    """

    tree_backed: ClassVar[bool] = False
    writable: ClassVar[bool] = False
    attaches_to_existing_pr: ClassVar[bool] = False


#: The closed union of checkout shapes — the "Tree Profile" vocabulary's
#: structured implementation (spec §Design Decisions).
CheckoutStrategy = (
    SessionTree
    | NewWriteTree
    | ExistingPrWriteTree
    | SharedReadOnlyTree
    | AmbientWorkingDir
)


@dataclass(frozen=True)
class EnforcementPosture:
    """A role's required capabilities, by operation and resource.

    A POLICY INPUT, not a sandbox claim (spec §Design Decisions): the
    read-only Tree remains the load-bearing checkout guard for reviewers and
    backend-native restrictions remain defense in depth. Capability-shaped on
    purpose — a reviewer needs network reads and review posting while its
    checkout stays immutable, which one mutation boolean cannot express.
    """

    #: May the Run mutate its checkout (edit/commit in the working tree)?
    checkout_mutation: bool
    #: May the Run execute commands (builds, tests, git/gh reads via Bash)?
    command_execution: bool
    #: Does the Run require network access (gh API, pushes, fetches)?
    network_access: bool
    #: May the Run mutate Git remotes / GitHub (push, open PRs, post reviews)?
    github_mutation: bool
    #: May the Run write temporary or artifact output outside the checkout?
    scratch_writes: bool
    #: May the Run AUTHOR code changes itself (edit code paths), or must it
    #: delegate implementation? Orthogonal to ``checkout_mutation`` on purpose:
    #: the coordinator mutates its checkout (it commits docs, planning, config)
    #: yet must NOT author code (ADR-0012) — one mutation flag cannot say both,
    #: which is exactly why posture is capability-shaped. The harness edit guard
    #: (:mod:`shipit.harness.policy`) reads this pairing via
    #: :func:`delegates_code_authorship` instead of naming a role.
    code_authorship: bool


@dataclass(frozen=True)
class RoleProfile:
    """One fixed Role's structural execution shape — never its prose.

    References the Role it profiles; the Lex Role definition (composed by
    :mod:`shipit.harness.prompts`) stays the sole source of behavioral
    content. ``generates_agent_def`` / ``has_brief_template`` declare the
    generated and brief SURFACES; the generators themselves migrate onto
    these flags in a later workstream (RPE01-WS02).
    """

    role: Role
    checkout: CheckoutStrategy
    enforcement: EnforcementPosture
    generates_agent_def: bool
    has_brief_template: bool
    launch_contexts: frozenset[LaunchContext]
    result_channel: ResultChannel


#: A full-trust write posture — the implementer and the shepherd, the two roles
#: that AUTHOR code (``code_authorship=True``).
_WRITE_POSTURE = EnforcementPosture(
    checkout_mutation=True,
    command_execution=True,
    network_access=True,
    github_mutation=True,
    scratch_writes=True,
    code_authorship=True,
)

#: The coordinator's posture: full write EXCEPT code authorship. It mutates its
#: checkout (commits docs, planning, config) and drives GitHub, but delegates
#: CODE changes rather than implementing them (ADR-0012) — the one posture whose
#: ``checkout_mutation and not code_authorship`` pairing the edit guard fires on.
_ORCHESTRATOR_POSTURE = EnforcementPosture(
    checkout_mutation=True,
    command_execution=True,
    network_access=True,
    github_mutation=True,
    scratch_writes=True,
    code_authorship=False,
)

#: The registry — TOTAL over the closed Role vocabulary, one profile per
#: role, Shipit-owned (ADR-0047: no consumer configuration surface reads
#: into this value). Wrapped read-only so a consumer cannot patch a profile
#: at runtime; totality and one-to-one-ness are pinned by tests.
PROFILES: Mapping[Role, RoleProfile] = MappingProxyType(
    {
        Role.COORDINATOR: RoleProfile(
            role=Role.COORDINATOR,
            checkout=SessionTree(),
            enforcement=_ORCHESTRATOR_POSTURE,
            # The coordinator is the top-level session: no agent-def, no
            # brief — its prompt rides the injected context + deny reason.
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
            # Native only TODAY: the dev cycle parks/resumes a shepherd as a
            # native subagent (ADR-0035). A detached spawn would route the
            # shepherd through the new-branch/draft-PR implementer handshake
            # — the exact invalid combination this registry exists to refuse
            # (spec §Migration) — so DETACHED joins only when the existing-PR
            # attachment lifecycle lands (RPE01-WS04).
            launch_contexts=frozenset({LaunchContext.NATIVE_SUBAGENT}),
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
                # Read-only investigation: never authors code.
                code_authorship=False,
            ),
            generates_agent_def=True,
            has_brief_template=False,
            # Ambient native investigation ONLY — a detached spawn would mint
            # a write Tree the explorer must never have.
            launch_contexts=frozenset({LaunchContext.NATIVE_SUBAGENT}),
            result_channel=ResultChannel.COORDINATOR_REPORT,
        ),
        Role.REVIEWER: RoleProfile(
            role=Role.REVIEWER,
            checkout=SharedReadOnlyTree(),
            enforcement=EnforcementPosture(
                # The one posture that PROVES capability shape: the reviewed
                # checkout is immutable while the review itself is posted
                # through GitHub and captured output may land as artifacts.
                checkout_mutation=False,
                command_execution=True,
                network_access=True,
                github_mutation=True,
                scratch_writes=True,
                # The review posts through GitHub, but never authors code.
                code_authorship=False,
            ),
            generates_agent_def=True,
            has_brief_template=False,
            launch_contexts=frozenset(
                {LaunchContext.DETACHED, LaunchContext.NATIVE_SUBAGENT}
            ),
            result_channel=ResultChannel.POSTED_REVIEW,
        ),
    }
)


class RoleValidationError(ValueError):
    """A strict-boundary role refusal, minted BEFORE any provisioning or launch.

    Raised by :func:`parse_role` / :func:`validate_spawn` when a public or
    programmatic boundary receives an unknown role or an unsupported
    role/launch-context pairing. The message names the offending role, the
    requested context (when known), and the supported alternatives — the
    spec's error contract. Never raised by the lenient hook boundary
    (:func:`shipit.harness.role.resolve_role`), which stays fail-safe instead
    of fail-closed on purpose.
    """


def _known_roles() -> str:
    """The closed vocabulary, rendered for refusal messages."""
    return ", ".join(role.value for role in Role)


def profile_for(role: Role) -> RoleProfile:
    """The profile lookup — total over :class:`Role`, pure, deterministic."""
    return PROFILES[role]


def delegates_code_authorship(role: Role) -> bool:
    """True iff ``role`` may mutate its checkout but must NOT author code itself.

    The capability-shaped form of the ADR-0012 edit guard (spec §"Enforcement
    posture is capability-shaped, not a mutation flag"): a role with a WRITABLE
    checkout — it commits docs, planning, config — that still delegates CODE
    changes rather than implementing them. Derived from posture
    (``checkout_mutation and not code_authorship``), so the harness edit guard
    (:mod:`shipit.harness.policy`) consumes profile posture instead of naming the
    coordinator. It is exactly the coordinator today; any future role with the
    same posture is guarded with no edit to the policy module, and a read-only
    role (whose checkout cannot mutate at all) is NOT caught here — its tools and
    read-only Tree are the load-bearing guard, as the spec requires. Pure.
    """
    posture = PROFILES[role].enforcement
    return posture.checkout_mutation and not posture.code_authorship


def parse_role(name: str) -> Role:
    """Parse a public/programmatic role input STRICTLY to the closed registry.

    Normalizes whitespace and case (GitHub-style, matching the hook
    resolver's normalization) but never falls back: an input outside the
    closed vocabulary raises :class:`RoleValidationError` naming it. This is
    the boundary that keeps an unknown native worker identity (which the hook
    tolerates) from ever becoming a spawnable Role.
    """
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
    """The spawn preflight: parse the role, check the launch context, or refuse.

    The registry-driven gate every spawn boundary runs BEFORE Tree
    provisioning or backend launch (the acceptance invariant): an unknown
    role, or a known role whose profile does not support ``context`` (a
    detached explorer, a detached coordinator, a detached shepherd until
    WS04), is a :class:`RoleValidationError` naming the role, the requested
    context, and the supported alternatives. Returns the role's profile on
    success. Pure: a value check, no I/O of any kind.
    """
    try:
        role = parse_role(name)
    except RoleValidationError as exc:
        # Preserve parse_role's specific diagnosis (empty vs unknown role) and
        # append the launch context — rewriting every parse failure into
        # "unknown role" would mislabel an empty input and diverge from
        # parse_role's contract.
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
