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
    PerRunReadOnlyTree,
    ResultChannel,
    RoleProfile,
    RoleValidationError,
    SessionTree,
    parse_role,
    profile_for,
    validate_spawn,
)


def test_registry_is_total_over_the_closed_role_enum():
    assert set(PROFILES) == set(Role)


@pytest.mark.parametrize("role", list(Role))
def test_every_profile_is_complete_and_self_identifying(role):
    profile = profile_for(role)
    assert isinstance(profile, RoleProfile)
    assert profile.role is role
    assert isinstance(profile.checkout, roleprofile.CheckoutStrategy)
    assert isinstance(profile.enforcement, roleprofile.EnforcementPosture)
    assert isinstance(profile.launch_contexts, frozenset)
    assert profile.launch_contexts
    assert all(isinstance(c, LaunchContext) for c in profile.launch_contexts)
    assert isinstance(profile.result_channel, ResultChannel)


def test_lookup_is_deterministic():
    for role in Role:
        assert profile_for(role) is profile_for(role) is PROFILES[role]


def test_profiles_are_shipit_owned_frozen_values():
    with pytest.raises(TypeError):
        PROFILES[Role.EXPLORER] = PROFILES[Role.IMPLEMENTER]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile_for(Role.REVIEWER).generates_agent_def = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile_for(Role.REVIEWER).enforcement.checkout_mutation = True


def test_checkout_strategies_separate_the_five_shapes():
    expected = {
        Role.COORDINATOR: SessionTree,
        Role.IMPLEMENTER: NewWriteTree,
        Role.SHEPHERD: ExistingPrWriteTree,
        Role.REVIEWER: PerRunReadOnlyTree,
        Role.EXPLORER: AmbientWorkingDir,
    }
    for role, shape in expected.items():
        assert type(profile_for(role).checkout) is shape
    assert len({type(p.checkout) for p in PROFILES.values()}) == len(Role)


def test_checkout_axes_encode_allocation_and_attachment_not_one_flag():
    implementer = profile_for(Role.IMPLEMENTER).checkout
    shepherd = profile_for(Role.SHEPHERD).checkout
    reviewer = profile_for(Role.REVIEWER).checkout
    explorer = profile_for(Role.EXPLORER).checkout
    coordinator = profile_for(Role.COORDINATOR).checkout

    assert implementer.writable and not implementer.attaches_to_existing_pr
    assert shepherd.writable and shepherd.attaches_to_existing_pr
    assert reviewer.tree_backed and not reviewer.writable
    assert reviewer.attaches_to_existing_pr
    assert not explorer.tree_backed and not explorer.writable
    assert coordinator.tree_backed and coordinator.writable


def test_checkout_strategy_inverse_lookup_is_registry_derived():
    assert roleprofile.roles_with_checkout_strategy(ExistingPrWriteTree) == (
        Role.SHEPHERD,
    )
    assert roleprofile.roles_with_checkout_strategy(NewWriteTree) == (Role.IMPLEMENTER,)
    assert roleprofile.roles_with_checkout_strategy(PerRunReadOnlyTree) == (
        Role.REVIEWER,
    )


def test_reviewer_posture_proves_capability_shape():
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
    assert profile_for(Role.COORDINATOR).enforcement.checkout_mutation
    assert not profile_for(Role.COORDINATOR).enforcement.code_authorship
    for role in (Role.IMPLEMENTER, Role.SHEPHERD):
        assert profile_for(role).enforcement.code_authorship
    for role in (Role.EXPLORER, Role.REVIEWER):
        assert not profile_for(role).enforcement.code_authorship


def test_delegates_code_authorship_is_the_capability_shaped_edit_guard():
    assert roleprofile.delegates_code_authorship(Role.COORDINATOR)
    for role in (Role.IMPLEMENTER, Role.SHEPHERD, Role.EXPLORER, Role.REVIEWER):
        assert not roleprofile.delegates_code_authorship(role)


def test_tree_backed_is_the_mandatory_isolation_input():
    """The spawn guard reads `tree_backed`, so `explorer` must be the ONE role that passes by construction."""
    exempt = {r for r in Role if not profile_for(r).checkout.tree_backed}
    assert exempt == {Role.EXPLORER}


@pytest.mark.parametrize("bogus", ["fork", "general-purpose", "claude", "Explore"])
def test_harness_native_subagent_types_are_not_registry_roles(bogus):
    """The spawn guard cannot gate what resolves to no profile, so these must stay unparseable."""
    with pytest.raises(RoleValidationError):
        parse_role(bogus)


def test_explorer_posture_is_read_scoped():
    posture = profile_for(Role.EXPLORER).enforcement
    assert posture.command_execution
    assert not posture.checkout_mutation
    assert not posture.github_mutation
    assert not posture.network_access
    assert not posture.scratch_writes


def test_agent_def_surface_agrees_with_the_prompt_generator():
    declared = {r for r in Role if profile_for(r).generates_agent_def}
    assert declared == set(prompts.SUBAGENT_ROLES)
    assert not profile_for(Role.COORDINATOR).generates_agent_def


def test_brief_surface_agrees_with_the_prompt_generator():
    declared = {r for r in Role if profile_for(r).has_brief_template}
    assert declared == set(prompts.BRIEF_ROLES)


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
    assert contexts[Role.REVIEWER] == {LaunchContext.DETACHED}


def test_result_channels_are_role_distinct():
    assert profile_for(Role.IMPLEMENTER).result_channel is ResultChannel.DRAFT_PR
    assert profile_for(Role.SHEPHERD).result_channel is ResultChannel.EXISTING_PR_ROUNDS
    assert profile_for(Role.REVIEWER).result_channel is ResultChannel.POSTED_REVIEW
    assert profile_for(Role.EXPLORER).result_channel is ResultChannel.COORDINATOR_REPORT
    assert (
        profile_for(Role.COORDINATOR).result_channel
        is ResultChannel.ORCHESTRATION_SESSION
    )


@pytest.mark.parametrize("role", list(Role))
def test_parse_role_accepts_every_registry_role(role):
    assert parse_role(role.value) is role
    assert parse_role(f"  {role.value.upper()}  ") is role


@pytest.mark.parametrize("bogus", ["wizard", "general-purpose", "", "   ", "review er"])
def test_parse_role_refuses_arbitrary_strings(bogus):
    with pytest.raises(RoleValidationError):
        parse_role(bogus)


def test_parse_role_refusal_names_the_input_and_the_closed_set():
    with pytest.raises(RoleValidationError, match=r"'wizard'") as excinfo:
        parse_role("wizard")
    for role in Role:
        assert role.value in str(excinfo.value)


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
    with pytest.raises(RoleValidationError, match=r"empty role.*detached") as excinfo:
        validate_spawn("", LaunchContext.DETACHED)
    assert "unknown role" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("role", "context"),
    [
        ("explorer", LaunchContext.DETACHED),
        ("reviewer", LaunchContext.NATIVE_SUBAGENT),
        ("coordinator", LaunchContext.DETACHED),
        ("coordinator", LaunchContext.NATIVE_SUBAGENT),
    ],
)
def test_validate_spawn_refuses_unsupported_role_context_pairs(role, context):
    with pytest.raises(RoleValidationError) as excinfo:
        validate_spawn(role, context)
    message = str(excinfo.value)
    assert role in message
    assert context.value in message
    assert "supported" in message


def test_unknown_hook_worker_is_governed_but_never_spawnable():
    unknown = "general-purpose"
    assert resolve_role({"agent_type": unknown}) is not Role.COORDINATOR
    with pytest.raises(RoleValidationError):
        parse_role(unknown)
    with pytest.raises(RoleValidationError):
        validate_spawn(unknown, LaunchContext.DETACHED)
