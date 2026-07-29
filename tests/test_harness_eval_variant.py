from __future__ import annotations

from shipit.harness.eval.variant import (
    VARIANT_LABEL_ENV,
    Variant,
    resolve_variant,
    role_of_meta,
    role_of_name,
    variant_of,
)
from shipit.harness.role import Role


def test_identical_prompts_hash_identically_so_runs_pool():
    a = variant_of("base + implementer overlay")
    b = variant_of("base + implementer overlay")
    assert a.content_hash == b.content_hash


def test_changed_prompt_hashes_differently_so_runs_separate():
    a = variant_of("base + implementer overlay")
    b = variant_of("base + implementer overlay!")
    assert a.content_hash != b.content_hash


def test_content_hash_is_the_pristine_hash_scheme():
    assert variant_of("x").content_hash.startswith("sha256:")


def test_label_rides_through_for_ab_runs():
    plain = variant_of("p")
    arm = variant_of("p", label="experiment-B")
    assert arm.content_hash == plain.content_hash
    assert arm.label == "experiment-B"
    assert plain.label is None


def test_as_record_is_the_stamped_dict():
    rec = Variant(content_hash="sha256:deadbeef", label="A").as_record()
    assert rec == {"content_hash": "sha256:deadbeef", "label": "A"}


def test_role_of_meta_maps_agent_type_to_role():
    assert role_of_meta({"agentType": "implementer"}) is Role.IMPLEMENTER
    assert role_of_meta({"agentType": "shepherd"}) is Role.SHEPHERD


def test_role_of_meta_defaults_to_coordinator_only_for_absent_or_blank():
    assert role_of_meta(None) is Role.COORDINATOR
    assert role_of_meta({}) is Role.COORDINATOR
    assert role_of_meta({"agentType": ""}) is Role.COORDINATOR
    assert role_of_meta({"agentType": "   "}) is Role.COORDINATOR


def test_role_of_meta_attributes_drifted_agent_type_to_a_worker_not_coordinator():
    assert role_of_meta({"agentType": "nonesuch"}) is Role.IMPLEMENTER
    assert role_of_meta({"agentType": "Implementer"}) is Role.IMPLEMENTER


def test_role_of_name_shares_the_meta_resolution_rules():
    assert role_of_name("implementer") is Role.IMPLEMENTER
    assert role_of_name("  Shepherd  ") is Role.SHEPHERD
    assert role_of_name(None) is Role.COORDINATOR
    assert role_of_name("") is Role.COORDINATOR
    assert role_of_name("   ") is Role.COORDINATOR
    assert role_of_name("some-future-role") is Role.IMPLEMENTER
    for name in ("implementer", "shepherd", "reviewer", "", None, "nonesuch"):
        assert role_of_name(name) is role_of_meta({"agentType": name})


def test_resolve_variant_hashes_the_real_role_prompt_and_is_stable():
    impl1 = resolve_variant({"agentType": "implementer"}, env={})
    impl2 = resolve_variant({"agentType": "implementer"}, env={})
    coord = resolve_variant(None, env={})
    assert impl1.content_hash == impl2.content_hash
    assert impl1.content_hash != coord.content_hash
    assert impl1.label is None


def test_resolve_variant_carries_the_env_label():
    v = resolve_variant({"agentType": "implementer"}, env={VARIANT_LABEL_ENV: "arm-2"})
    assert v.label == "arm-2"


def test_resolve_variant_normalizes_the_env_label():
    padded = resolve_variant(
        {"agentType": "implementer"}, env={VARIANT_LABEL_ENV: "  arm-2  "}
    )
    clean = resolve_variant(
        {"agentType": "implementer"}, env={VARIANT_LABEL_ENV: "arm-2"}
    )
    assert padded.label == "arm-2"
    assert padded == clean
    for blank in ("", "   "):
        v = resolve_variant(
            {"agentType": "implementer"}, env={VARIANT_LABEL_ENV: blank}
        )
        assert v.label is None
