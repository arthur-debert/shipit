"""agent.backend — the ONE agent-backend identity/alias registry (ADR-0025 / COR01-WS02).

These tests pin the single-identity contract: every alias (spawn token, funnel login,
check-run, Doppler keys, model aliases) derives from ONE definition, and the two axes
(launch + funnel) read the SAME registry rather than carrying duplicate tables.
"""

from __future__ import annotations

import pytest

from shipit.agent import backend


def test_registry_is_the_closed_backend_set():
    assert [b.name for b in backend.REGISTRY] == ["claude", "codex", "antigravity"]
    assert backend.by_name("codex") is backend.CODEX
    assert backend.by_name("antigravity") is backend.ANTIGRAVITY
    with pytest.raises(KeyError):
        backend.by_name("nope")


def test_codex_identity_aliases_all_derive_from_one_name():
    b = backend.CODEX
    # canonical name == spawn --backend token; binary on PATH.
    assert b.name == "codex"
    assert b.binary == "codex"
    # funnel aliases all derive from the funnel_agent, defined once.
    assert b.funnel_agent == "codex"
    assert b.funnel_login == "adr-codex-review[bot]"
    assert b.bot_slug_fragment == "codex-review"
    assert b.check_run_name == "codex-local"
    assert b.doppler_pem_key == "CODEX_REVIEW_APP_PRIVATE_KEY"
    assert b.doppler_app_id_key == "CODEX_REVIEW_APP_ID"


def test_antigravity_has_three_surface_names_one_identity():
    b = backend.ANTIGRAVITY
    # The --backend token is `antigravity`, the CLI binary is `agy`, the funnel agent
    # is `agy` — three surface names, one identity object.
    assert b.name == "antigravity"
    assert b.binary == "agy"
    assert b.funnel_agent == "agy"
    assert b.funnel_login == "adr-agy-review[bot]"
    assert b.check_run_name == "agy-local"
    assert b.doppler_pem_key == "AGY_REVIEW_APP_PRIVATE_KEY"


def test_claude_has_no_funnel_identity_and_raises_if_asked():
    b = backend.CLAUDE
    assert b.has_funnel_identity is False
    assert b.funnel_agent is None
    # Asking a non-funnel backend for a funnel alias fails loud, never fabricates one.
    for prop in (
        "funnel_login",
        "bot_slug_fragment",
        "check_run_name",
        "doppler_pem_key",
        "doppler_app_id_key",
    ):
        with pytest.raises(ValueError):
            getattr(b, prop)


def test_funnel_backends_are_exactly_the_app_reviewers():
    assert [b.name for b in backend.funnel_backends()] == ["codex", "antigravity"]
    assert backend.by_funnel_agent("codex") is backend.CODEX
    assert backend.by_funnel_agent("agy") is backend.ANTIGRAVITY
    with pytest.raises(KeyError):
        backend.by_funnel_agent("claude")


def test_funnel_doppler_keys_is_the_single_source_for_ghauth():
    keys = backend.funnel_doppler_keys()
    assert keys == {
        "codex": {
            "pem": "CODEX_REVIEW_APP_PRIVATE_KEY",
            "app_id": "CODEX_REVIEW_APP_ID",
        },
        "agy": {"pem": "AGY_REVIEW_APP_PRIVATE_KEY", "app_id": "AGY_REVIEW_APP_ID"},
    }


def test_ghauth_reads_the_registry_not_a_duplicate_table():
    # The funnel axis must reference the ONE registry — no duplicated alias table.
    from shipit.review import ghauth

    assert ghauth._DOPPLER_KEYS == backend.funnel_doppler_keys()


def test_launch_adapters_read_the_registry_not_a_duplicate_table():
    # The launch axis must reference the ONE registry — no duplicated MODEL_ALIASES.
    from shipit.spawn.backends import antigravity as agy_adapter
    from shipit.spawn.backends import codex as codex_adapter

    assert codex_adapter.MODEL_ALIASES is backend.CODEX.model_aliases
    assert codex_adapter.DEFAULT_MODEL == backend.CODEX.default_model
    assert agy_adapter.MODEL_ALIASES is backend.ANTIGRAVITY.model_aliases
    assert agy_adapter.DEFAULT_MODEL == backend.ANTIGRAVITY.default_model


def test_resolve_model_maps_aliases_and_passes_verbatim():
    assert backend.CODEX.resolve_model("pro") == "gpt-5.5"
    assert backend.CODEX.resolve_model("gpt-5.5") == "gpt-5.5"  # verbatim passthrough
    assert backend.CODEX.resolve_model(None) == "gpt-5.5"  # default
    assert backend.ANTIGRAVITY.resolve_model("pro") == "Gemini 3.1 Pro (High)"
    with pytest.raises(ValueError):
        backend.CLAUDE.resolve_model()  # no default model


def test_backend_identity_is_the_name_alone():
    # Two references to the same backend compare/hash equal regardless of alias data.
    assert backend.by_name("codex") == backend.CODEX
    assert hash(backend.by_name("codex")) == hash(backend.CODEX)
    # Model aliases are read-only (a single shared table, not a mutable copy).
    with pytest.raises(TypeError):
        backend.CODEX.model_aliases["pro"] = "x"  # type: ignore[index]
