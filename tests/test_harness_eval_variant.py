"""Variant resolver: hash stability / poolability (module #4).

The variant is the run's harness-version attribution, so the tests pin its
EXTERNAL behavior — identical role prompts hash identically (runs pool), a changed
prompt hashes differently (runs separate), and an explicit A/B label rides through
— never the hashing internals.
"""

from __future__ import annotations

from shipit.harness.eval.variant import (
    VARIANT_LABEL_ENV,
    Variant,
    resolve_variant,
    role_of_meta,
    variant_of,
)
from shipit.harness.role import Role


def test_identical_prompts_hash_identically_so_runs_pool():
    # Two runs of the SAME prompt → one variant → they pool in aggregation.
    a = variant_of("base + implementer overlay")
    b = variant_of("base + implementer overlay")
    assert a.content_hash == b.content_hash


def test_changed_prompt_hashes_differently_so_runs_separate():
    # A one-character prompt change → a different variant → runs separate.
    a = variant_of("base + implementer overlay")
    b = variant_of("base + implementer overlay!")
    assert a.content_hash != b.content_hash


def test_content_hash_is_the_pristine_hash_scheme():
    # Reuses config.content_hash — the `sha256:` key the install reconciler uses.
    assert variant_of("x").content_hash.startswith("sha256:")


def test_label_rides_through_for_ab_runs():
    plain = variant_of("p")
    arm = variant_of("p", label="experiment-B")
    # Same prompt → same hash; the label is what separates the A/B arms.
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
    # Only an absent/blank agentType is the coordinator (no meta, no agent-def
    # prompt of its own) — the same default the record builder stamps.
    assert role_of_meta(None) is Role.COORDINATOR
    assert role_of_meta({}) is Role.COORDINATOR
    assert role_of_meta({"agentType": ""}) is Role.COORDINATOR
    assert role_of_meta({"agentType": "   "}) is Role.COORDINATOR


def test_role_of_meta_attributes_drifted_agent_type_to_a_worker_not_coordinator():
    # A present-but-unrecognized agentType is still a subagent: it must NOT pool
    # under the coordinator prompt hash. Mirrors role.resolve_role's worker
    # fallback so the two resolvers agree.
    assert role_of_meta({"agentType": "nonesuch"}) is Role.IMPLEMENTER
    assert role_of_meta({"agentType": "Implementer"}) is Role.IMPLEMENTER


def test_resolve_variant_hashes_the_real_role_prompt_and_is_stable():
    # Boundary: reads the bundled fragments. Two resolves of the same role pool;
    # distinct roles (distinct overlays) separate.
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
    # Accidental whitespace from shell quoting / CI templating must not split an
    # arm from itself, and an all-whitespace/empty label is no label at all.
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
