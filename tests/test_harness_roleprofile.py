"""Role Profile registry (RPE01-WS01): totality, shapes, and the strict boundary.

The registry is the structural answer to "how may this Role run?" — these
tests pin the spec's invariants (docs/spec/role-profiles-work-env.md
§Testing): the registry is TOTAL and one-to-one over the closed Role enum,
every profile is complete and frozen, the checkout strategies are the five
distinct structured shapes (never a flat token), enforcement posture is
capability-shaped, the declared surfaces agree with the prompt generator's
constants (the WS02 migration target), and the strict public parse refuses
exactly what the lenient hook boundary tolerates.
"""

from __future__ import annotations

import dataclasses

import pytest

from shipit.harness import prompts, roleprofile
from shipit.harness.role import Role, resolve_role
from shipit.harness.roleprofile import (
    PROFILES,
    AmbientWorkingDir,
    ExistingPrWriteTree,
    LaunchContext,
    NewWriteTree,
    ResultChannel,
    RoleProfile,
    RoleValidationError,
    SessionTree,
    SharedReadOnlyTree,
    parse_role,
    profile_for,
    validate_spawn,
)

# ---------------------------------------------------------------------------
# Registry totality and completeness
# ---------------------------------------------------------------------------


def test_registry_is_total_over_the_closed_role_enum():
    """Every fixed Role has a profile; no extra keys. Adding a Role without a
    complete profile fails HERE (the spec's totality gate)."""
    assert set(PROFILES) == set(Role)


@pytest.mark.parametrize("role", list(Role))
def test_every_profile_is_complete_and_self_identifying(role):
    profile = profile_for(role)
    assert isinstance(profile, RoleProfile)
    assert profile.role is role  # one-to-one: the key IS the profile's role
    assert isinstance(profile.checkout, roleprofile.CheckoutStrategy)
    assert isinstance(profile.enforcement, roleprofile.EnforcementPosture)
    assert isinstance(profile.launch_contexts, frozenset)
    assert profile.launch_contexts  # every role is launchable somewhere
    assert all(isinstance(c, LaunchContext) for c in profile.launch_contexts)
    assert isinstance(profile.result_channel, ResultChannel)


def test_lookup_is_deterministic():
    """Pure value lookups: the same frozen profile object every time."""
    for role in Role:
        assert profile_for(role) is profile_for(role) is PROFILES[role]


def test_profiles_are_shipit_owned_frozen_values():
    """No consumer (or runtime) mutation surface (ADR-0047): the mapping is
    read-only and each profile (and its posture) is frozen."""
    with pytest.raises(TypeError):
        PROFILES[Role.EXPLORER] = PROFILES[Role.IMPLEMENTER]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile_for(Role.REVIEWER).generates_agent_def = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile_for(Role.REVIEWER).enforcement.checkout_mutation = True


# ---------------------------------------------------------------------------
# Checkout strategy — five distinct structured shapes, orthogonal axes
# ---------------------------------------------------------------------------


def test_checkout_strategies_separate_the_five_shapes():
    """Session / new-write / existing-PR-write / shared-read-only / ambient are
    DISTINCT shapes, one per role — never a flat four-token enum."""
    expected = {
        Role.COORDINATOR: SessionTree,
        Role.IMPLEMENTER: NewWriteTree,
        Role.SHEPHERD: ExistingPrWriteTree,
        Role.REVIEWER: SharedReadOnlyTree,
        Role.EXPLORER: AmbientWorkingDir,
    }
    for role, shape in expected.items():
        assert type(profile_for(role).checkout) is shape
    # One-to-one over the shapes too: no role shares another's checkout shape.
    assert len({type(p.checkout) for p in PROFILES.values()}) == len(Role)


def test_checkout_axes_encode_allocation_and_attachment_not_one_flag():
    """The orthogonal axes the historical flat enum collapsed: implementer and
    shepherd are BOTH writable but differ on attachment; reviewer is
    Tree-backed but immutable; explorer has no Tree at all."""
    implementer = profile_for(Role.IMPLEMENTER).checkout
    shepherd = profile_for(Role.SHEPHERD).checkout
    reviewer = profile_for(Role.REVIEWER).checkout
    explorer = profile_for(Role.EXPLORER).checkout
    coordinator = profile_for(Role.COORDINATOR).checkout

    assert implementer.writable and not implementer.attaches_to_existing_pr
    assert shepherd.writable and shepherd.attaches_to_existing_pr
    assert reviewer.tree_backed and not reviewer.writable
    assert reviewer.attaches_to_existing_pr  # branch-pinned to the PR head
    assert not explorer.tree_backed and not explorer.writable
    assert coordinator.tree_backed and coordinator.writable  # session lifetime


def test_checkout_strategy_inverse_lookup_is_registry_derived():
    """Lifecycle-family call sites can ask the registry instead of restating roles."""
    assert roleprofile.roles_with_checkout_strategy(ExistingPrWriteTree) == (
        Role.SHEPHERD,
    )
    assert roleprofile.roles_with_checkout_strategy(NewWriteTree) == (Role.IMPLEMENTER,)
    assert roleprofile.roles_with_checkout_strategy(SharedReadOnlyTree) == (
        Role.REVIEWER,
    )


# ---------------------------------------------------------------------------
# Enforcement posture — capability-shaped, never a mutation boolean
# ---------------------------------------------------------------------------


def test_reviewer_posture_proves_capability_shape():
    """The spec's own counterexample to a single mutation flag: the reviewed
    checkout is immutable while review POSTING (GitHub mutation), network,
    and captured output stay allowed."""
    posture = profile_for(Role.REVIEWER).enforcement
    assert not posture.checkout_mutation
    assert posture.github_mutation
    assert posture.network_access
    assert posture.scratch_writes


def test_write_roles_carry_the_full_write_posture():
    for role in (Role.COORDINATOR, Role.IMPLEMENTER, Role.SHEPHERD):
        posture = profile_for(role).enforcement
        assert posture.checkout_mutation
        assert posture.github_mutation


def test_code_authorship_is_orthogonal_to_checkout_mutation():
    """The capability a single mutation flag could not express (RPE01-WS02): the
    coordinator MUTATES its checkout (it commits docs/planning) yet must NOT author
    code (ADR-0012), so checkout_mutation and code_authorship are independent axes.
    Only the two implementing roles author code; the coordinator and both read-only
    roles do not."""
    assert profile_for(Role.COORDINATOR).enforcement.checkout_mutation
    assert not profile_for(Role.COORDINATOR).enforcement.code_authorship
    for role in (Role.IMPLEMENTER, Role.SHEPHERD):
        assert profile_for(role).enforcement.code_authorship
    for role in (Role.EXPLORER, Role.REVIEWER):
        assert not profile_for(role).enforcement.code_authorship


def test_delegates_code_authorship_is_the_capability_shaped_edit_guard():
    """The posture the harness edit guard reads instead of naming a role: a
    writable checkout that must not author code. Exactly the coordinator today,
    and NEVER a read-only role (whose checkout cannot mutate at all — its tools and
    read-only Tree are the guard, per the spec)."""
    assert roleprofile.delegates_code_authorship(Role.COORDINATOR)
    for role in (Role.IMPLEMENTER, Role.SHEPHERD, Role.EXPLORER, Role.REVIEWER):
        assert not roleprofile.delegates_code_authorship(role)


def test_explorer_posture_is_read_scoped():
    """Ambient reading through Bash without becoming a write Run: command
    execution only — no checkout, GitHub, network, or artifact effects."""
    posture = profile_for(Role.EXPLORER).enforcement
    assert posture.command_execution
    assert not posture.checkout_mutation
    assert not posture.github_mutation
    assert not posture.network_access
    assert not posture.scratch_writes


# ---------------------------------------------------------------------------
# Generated + brief surfaces agree with the prompt generator's constants
# ---------------------------------------------------------------------------


def test_agent_def_surface_agrees_with_the_prompt_generator():
    """The profile metadata IS the WS02 migration target: the roles declaring
    a generated agent-def must be exactly prompts.SUBAGENT_ROLES today."""
    declared = {r for r in Role if profile_for(r).generates_agent_def}
    assert declared == set(prompts.SUBAGENT_ROLES)
    assert not profile_for(Role.COORDINATOR).generates_agent_def


def test_brief_surface_agrees_with_the_prompt_generator():
    declared = {r for r in Role if profile_for(r).has_brief_template}
    assert declared == set(prompts.BRIEF_ROLES)


# ---------------------------------------------------------------------------
# Launch contexts and result channels — the current dev cycle's contracts
# ---------------------------------------------------------------------------


def test_launch_contracts_match_the_current_dev_cycle():
    contexts = {role: profile_for(role).launch_contexts for role in Role}
    assert contexts[Role.COORDINATOR] == {LaunchContext.HOST_SESSION}
    assert contexts[Role.IMPLEMENTER] == {
        LaunchContext.DETACHED,
        LaunchContext.NATIVE_SUBAGENT,
    }
    assert contexts[Role.SHEPHERD] == {
        LaunchContext.DETACHED,
        LaunchContext.NATIVE_SUBAGENT,
    }
    assert contexts[Role.EXPLORER] == {LaunchContext.NATIVE_SUBAGENT}
    assert contexts[Role.REVIEWER] == {
        LaunchContext.DETACHED,
        LaunchContext.NATIVE_SUBAGENT,
    }


def test_result_channels_are_role_distinct():
    assert profile_for(Role.IMPLEMENTER).result_channel is ResultChannel.DRAFT_PR
    assert profile_for(Role.SHEPHERD).result_channel is ResultChannel.EXISTING_PR_ROUNDS
    assert profile_for(Role.REVIEWER).result_channel is ResultChannel.POSTED_REVIEW
    assert profile_for(Role.EXPLORER).result_channel is ResultChannel.COORDINATOR_REPORT
    assert (
        profile_for(Role.COORDINATOR).result_channel
        is ResultChannel.ORCHESTRATION_SESSION
    )


# ---------------------------------------------------------------------------
# Strict public parse — parse_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_parse_role_accepts_every_registry_role(role):
    assert parse_role(role.value) is role
    assert parse_role(f"  {role.value.upper()}  ") is role  # normalized, strict


@pytest.mark.parametrize("bogus", ["wizard", "general-purpose", "", "   ", "review er"])
def test_parse_role_refuses_arbitrary_strings(bogus):
    with pytest.raises(RoleValidationError):
        parse_role(bogus)


def test_parse_role_refusal_names_the_input_and_the_closed_set():
    with pytest.raises(RoleValidationError, match=r"'wizard'") as excinfo:
        parse_role("wizard")
    for role in Role:
        assert role.value in str(excinfo.value)


# ---------------------------------------------------------------------------
# Spawn preflight — validate_spawn
# ---------------------------------------------------------------------------


def test_validate_spawn_returns_the_profile_for_supported_pairs():
    assert (
        validate_spawn("implementer", LaunchContext.DETACHED)
        is PROFILES[Role.IMPLEMENTER]
    )
    assert validate_spawn("reviewer", LaunchContext.DETACHED) is PROFILES[Role.REVIEWER]
    assert validate_spawn("shepherd", LaunchContext.DETACHED) is PROFILES[Role.SHEPHERD]
    assert (
        validate_spawn("explorer", LaunchContext.NATIVE_SUBAGENT)
        is PROFILES[Role.EXPLORER]
    )


def test_validate_spawn_names_role_and_context_for_unknown_roles():
    with pytest.raises(RoleValidationError, match=r"'wizard'.*detached"):
        validate_spawn("wizard", LaunchContext.DETACHED)


def test_validate_spawn_preserves_the_empty_vs_unknown_diagnosis():
    # An empty input keeps parse_role's specific "empty role" diagnosis (not
    # rewritten to "unknown role ''") while still naming the launch context.
    with pytest.raises(RoleValidationError, match=r"empty role.*detached") as excinfo:
        validate_spawn("", LaunchContext.DETACHED)
    assert "unknown role" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("role", "context"),
    [
        ("explorer", LaunchContext.DETACHED),  # ambient — never a write Tree
        ("coordinator", LaunchContext.DETACHED),  # the host session itself
        ("coordinator", LaunchContext.NATIVE_SUBAGENT),
    ],
)
def test_validate_spawn_refuses_unsupported_role_context_pairs(role, context):
    with pytest.raises(RoleValidationError) as excinfo:
        validate_spawn(role, context)
    message = str(excinfo.value)
    assert role in message  # names the Role...
    assert context.value in message  # ...and the requested context
    assert "supported" in message  # ...and the supported alternatives


# ---------------------------------------------------------------------------
# The two boundaries stay deliberately different (spec §Design Decisions)
# ---------------------------------------------------------------------------


def test_unknown_hook_worker_is_governed_but_never_spawnable():
    """The native hook boundary keeps its safe fallback — an unknown non-empty
    identity is an unknown WORKER (never the coordinator) — while the strict
    registry boundary refuses the same string outright: hook leniency never
    mints a spawnable Role."""
    unknown = "general-purpose"
    assert resolve_role({"agent_type": unknown}) is not Role.COORDINATOR
    with pytest.raises(RoleValidationError):
        parse_role(unknown)
    with pytest.raises(RoleValidationError):
        validate_spawn(unknown, LaunchContext.DETACHED)
