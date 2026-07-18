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


def test_ghauth_reads_the_registry_not_a_duplicate_table():
    # The funnel axis must reference the ONE registry — ghauth resolves the Doppler
    # key names off the Backend identity itself, no duplicated alias table.
    from shipit.review import ghauth

    assert ghauth._doppler_keys(backend.CODEX) == {
        "pem": "CODEX_REVIEW_APP_PRIVATE_KEY",
        "app_id": "CODEX_REVIEW_APP_ID",
    }
    assert ghauth._doppler_keys(backend.ANTIGRAVITY) == {
        "pem": "AGY_REVIEW_APP_PRIVATE_KEY",
        "app_id": "AGY_REVIEW_APP_ID",
    }
    # A backend with no funnel App fails loud, never fabricates key names.
    with pytest.raises(ghauth.ReviewAuthError):
        ghauth._doppler_keys(backend.CLAUDE)


def test_check_run_name_inverse_is_a_registry_lookup():
    # COR02-WS03: a funnel reviewer name resolves back to its backend through the
    # registry — the inverse of `check_run_name` — never by slicing a `-local`
    # suffix off a string.
    assert backend.by_check_run_name("codex-local") is backend.CODEX
    assert backend.by_check_run_name("agy-local") is backend.ANTIGRAVITY
    with pytest.raises(KeyError):
        backend.by_check_run_name("copilot-local")


def test_app_slug_and_funnel_login_derive_from_one_alias():
    assert backend.CODEX.app_slug == "adr-codex-review"
    assert backend.CODEX.funnel_login == "adr-codex-review[bot]"
    assert backend.ANTIGRAVITY.app_slug == "adr-agy-review"
    with pytest.raises(ValueError):
        backend.CLAUDE.app_slug  # noqa: B018 - the raise IS the assertion


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


def test_adding_a_backend_needs_only_a_registry_entry(monkeypatch):
    """COR02-WS03 acceptance: wiring a NEW funnel backend is ONE registry entry —
    every derived name the funnel path uses (check-run name, funnel login, App slug,
    Doppler keys) falls out of the entry, and the funnel layers consume the Backend
    value object directly, so no other module needs a matching edit."""
    newbot = backend.Backend(
        name="newbot",
        binary="newbot-cli",
        funnel_agent="newbot",
        doppler_app_prefix="NEWBOT_REVIEW_APP",
    )

    # Every alias derives from the one entry — nothing else to define anywhere.
    assert newbot.check_run_name == "newbot-local"
    assert newbot.funnel_login == "adr-newbot-review[bot]"
    assert newbot.app_slug == "adr-newbot-review"
    assert newbot.doppler_pem_key == "NEWBOT_REVIEW_APP_PRIVATE_KEY"
    assert newbot.doppler_app_id_key == "NEWBOT_REVIEW_APP_ID"

    # The funnel check-run layer names + authors the run purely off the entry:
    # with the token mint + REST seam faked, `checkrun.create(newbot, …)` opens
    # `review: newbot-local` with NO newbot-specific code anywhere in the funnel.
    from shipit.review import checkrun

    monkeypatch.setattr(
        checkrun.ghauth, "installation_token", lambda b, repo: f"ghs_{b.name}"
    )
    seen: dict = {}

    def fake_rest(path, *, method=None, body=None, token=None):
        seen.update(path=path, body=body, token=token)
        return {"id": 7}

    monkeypatch.setattr(checkrun.gh, "rest", fake_rest)
    assert checkrun.create(newbot, "owner/repo", "deadbeef") == 7
    assert seen["body"]["name"] == "review: newbot-local"
    assert seen["token"] == "ghs_newbot"

    # And the auth layer resolves the Doppler key names off the SAME entry.
    from shipit.review import ghauth

    assert ghauth._doppler_keys(newbot) == {
        "pem": "NEWBOT_REVIEW_APP_PRIVATE_KEY",
        "app_id": "NEWBOT_REVIEW_APP_ID",
    }

    # The CONFIG seam (#313): the install-seeded App-secret names and the
    # `[secrets]` scaffold are DERIVED from `funnel_backends()`, so the new
    # backend's Doppler keys appear in both with ZERO config edits.
    from shipit import config

    monkeypatch.setattr(backend, "REGISTRY", (*backend.REGISTRY, newbot))
    seeds = config.seeded_app_secrets()
    assert "NEWBOT_REVIEW_APP_PRIVATE_KEY" in seeds
    assert "NEWBOT_REVIEW_APP_ID" in seeds
    scaffold = config.secrets_scaffold()
    assert '{ doppler = "NEWBOT_REVIEW_APP_PRIVATE_KEY" }' in scaffold
    assert '{ doppler = "NEWBOT_REVIEW_APP_ID" }' in scaffold


def test_backend_identity_is_the_name_alone():
    # Two references to the same backend compare/hash equal regardless of alias data.
    assert backend.by_name("codex") == backend.CODEX
    assert hash(backend.by_name("codex")) == hash(backend.CODEX)
    # Model aliases are read-only (a single shared table, not a mutable copy).
    with pytest.raises(TypeError):
        backend.CODEX.model_aliases["pro"] = "x"  # type: ignore[index]
