from __future__ import annotations

from shipit.agent import invocation as inv
from shipit.agent.invocation import (
    Invocation,
    Model,
    Provider,
    ReasoningLevel,
)


def test_model_identity_is_the_id_alone():
    bare = Model(id="gpt-5.5")
    enriched = Model(
        id="gpt-5.5",
        provider=Provider.OPENAI,
        reasoning_capability=frozenset({ReasoningLevel.HIGH}),
    )
    assert bare == enriched
    assert hash(bare) == hash(enriched)
    assert Model(id="gpt-5.4-mini") != bare


def test_provider_and_reasoning_coerce_tolerantly():
    assert Provider.coerce("openai") is Provider.OPENAI
    assert Provider.coerce(Provider.GOOGLE) is Provider.GOOGLE
    assert Provider.coerce("ANTHROPIC") is Provider.ANTHROPIC
    assert Provider.coerce("") is None
    assert Provider.coerce("nope") is None
    assert ReasoningLevel.coerce("high") is ReasoningLevel.HIGH
    assert ReasoningLevel.coerce(None) is None
    assert ReasoningLevel.coerce("MEDIUM") is ReasoningLevel.MEDIUM


def test_model_of_id_fills_known_providers():
    assert inv.model_of_id("gpt-5.5").provider is Provider.OPENAI
    assert inv.model_of_id("Gemini 3.1 Pro (High)").provider is Provider.GOOGLE
    assert inv.model_of_id("claude-sonnet-4").provider is Provider.ANTHROPIC
    assert inv.model_of_id("some-unknown-model").provider is None
    assert inv.model_of_id(None) is None
    assert inv.model_of_id("") is None


def test_cross_provider_backend_model_pairing_is_expressible():
    anthropic_model = Model(id="claude-opus", provider=Provider.ANTHROPIC)
    invocation = Invocation(
        backend="codex",
        model=anthropic_model,
        reasoning_level=ReasoningLevel.HIGH,
        permission_mode="bypassPermissions",
    )
    assert invocation.model is anthropic_model
    assert inv.supports("codex", anthropic_model) is False
    assert inv.supports("codex", inv.model_of_id("gpt-5.5")) is True
    assert inv.supports("antigravity", inv.model_of_id("Gemini 3.1 Pro (High)")) is True
    assert inv.supports("nope", inv.model_of_id("gpt-5.5")) is False
    assert inv.supports("codex", None) is False


def test_invocation_as_record_is_flat_and_null_safe():
    empty = Invocation().as_record()
    assert empty == {
        "backend": None,
        "model": None,
        "provider": None,
        "reasoning_level": None,
        "permission_mode": None,
    }
    full = Invocation(
        backend="codex",
        model=Model(id="gpt-5.5", provider=Provider.OPENAI),
        reasoning_level=ReasoningLevel.LOW,
        permission_mode="bypassPermissions",
    ).as_record()
    assert full == {
        "backend": "codex",
        "model": "gpt-5.5",
        "provider": "openai",
        "reasoning_level": "low",
        "permission_mode": "bypassPermissions",
    }


def test_observed_from_meta_reads_the_run_config():
    obs = inv.observed_from_meta(
        {
            "model": "gpt-5.5",
            "spawnMode": "bypassPermissions",
            "reasoning": "high",
            "backend": "codex",
        }
    )
    assert obs.backend == "codex"
    assert obs.model.id == "gpt-5.5"
    assert obs.model.provider is Provider.OPENAI
    assert obs.reasoning_level is ReasoningLevel.HIGH
    assert obs.permission_mode == "bypassPermissions"


def test_observed_from_meta_defaults_backend_to_claude_and_tolerates_gaps():
    obs = inv.observed_from_meta({"model": "claude-sonnet-4"})
    assert obs.backend == "claude"
    assert obs.model.provider is Provider.ANTHROPIC
    assert obs.reasoning_level is None
    assert obs.permission_mode is None
    assert inv.observed_from_meta(None).backend == "claude"
    assert inv.observed_from_meta({}).model is None


def test_intended_from_meta_is_a_seam_none_until_stamped():
    assert inv.intended_from_meta({"model": "gpt-5.5"}) is None
    assert inv.intended_from_meta(None) is None
    intent = inv.intended_from_meta(
        {
            "invocation": {
                "backend": "codex",
                "model": "gpt-5.5",
                "reasoning_level": "high",
                "permission_mode": "bypassPermissions",
            }
        }
    )
    assert intent.backend == "codex"
    assert intent.model.id == "gpt-5.5"
    assert intent.model.provider is Provider.OPENAI
    assert intent.reasoning_level is ReasoningLevel.HIGH
