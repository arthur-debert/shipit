import tomllib

import pytest

from shipit import config
from shipit.release import secretreq


def _artifacts(text: str) -> tuple[config.Artifact, ...]:
    return config.load_artifacts(tomllib.loads(text))


def _sources(text: str) -> list[config.SecretSource]:
    return config.load_secrets(tomllib.loads(text))


RUST_CLI = """
[artifacts.lex]
build = [{ toolchain = "rust", package = "lex-cli" }]
platforms = ["darwin-arm64", "linux-x86_64"]
bundle = { composition = "archive" }
endpoints = ["gh-release", "crates", "brew"]
sign = true
"""

PYTHON_PKG = """
[artifacts.dist]
build = ["python"]
endpoints = ["gh-release", "pypi"]
"""


def test_registry_keys_mirror_the_closed_endpoint_set():
    assert tuple(secretreq.ENDPOINT_SECRETS) == config.ENDPOINTS


def test_rust_cli_shape_derives_the_sync_set_in_traversal_order():
    names = secretreq.required_names(_artifacts(RUST_CLI))
    assert names == (
        "RELEASE_TOKEN",
        "CARGO_REGISTRY_TOKEN",
        "HOMEBREW_TAP_TOKEN",
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
    )


def test_accepted_names_append_both_notary_trios_for_a_signing_map():
    names = secretreq.accepted_names(_artifacts(RUST_CLI))
    assert names == (
        "RELEASE_TOKEN",
        "CARGO_REGISTRY_TOKEN",
        "HOMEBREW_TAP_TOKEN",
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "ASC_API_KEY_BASE64",
        "ASC_API_KEY_ID",
        "ASC_API_ISSUER_ID",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    )
    arts = _artifacts(PYTHON_PKG)
    assert secretreq.accepted_names(arts) == secretreq.required_names(arts)


def test_python_pkg_shape_derives_tokens_without_apple_names():
    names = secretreq.required_names(_artifacts(PYTHON_PKG))
    assert names == ("RELEASE_TOKEN", "PYPI_TOKEN")


def test_gh_release_endpoint_requires_nothing_beyond_prepare():
    arts = _artifacts('[artifacts.plugin]\nendpoints = ["gh-release"]\n')
    assert secretreq.required_names(arts) == ("RELEASE_TOKEN",)


def test_marketplace_endpoints_derive_their_pat_tokens():
    arts = _artifacts(
        '[artifacts.ext]\nendpoints = ["vscode-marketplace", "open-vsx"]\n'
    )
    assert secretreq.required_names(arts) == ("RELEASE_TOKEN", "VSCE_PAT", "OVSX_PAT")


def test_no_endpoints_means_nothing_required():
    assert secretreq.requirements(()) == ()
    arts = _artifacts('[artifacts.lib]\nbuild = ["python"]\n')
    assert secretreq.requirements(arts) == ()


def test_requirements_name_their_requiring_entry():
    reqs = secretreq.requirements(_artifacts(RUST_CLI))
    by_name = {req.name: req.required_by for req in reqs}
    assert by_name["RELEASE_TOKEN"] == "prepare push"
    assert by_name["CARGO_REGISTRY_TOKEN"] == "endpoint crates (artifact lex)"
    assert by_name["APPLE_CERTIFICATE"] == "sign-mac stage (artifact lex)"


def test_adding_an_endpoint_needs_no_derivation_change():
    for endpoint, names in secretreq.ENDPOINT_SECRETS.items():
        assert isinstance(names, tuple)
        assert endpoint in config.ENDPOINTS


def test_missing_source_names_the_requiring_entry():
    sources = _sources('[secrets]\nRELEASE_TOKEN = { env = "GH_TOKEN" }\n')
    missing = secretreq.missing_sources(_artifacts(PYTHON_PKG), sources)
    assert [(m.name, m.required_by) for m in missing] == [
        ("PYPI_TOKEN", "endpoint pypi (artifact dist)")
    ]


def test_fully_sourced_requirements_have_no_missing():
    sources = _sources(
        "[secrets]\n"
        'RELEASE_TOKEN = { env = "GH_TOKEN" }\n'
        'PYPI_TOKEN = { doppler = "PYPI_TOKEN" }\n'
    )
    assert secretreq.missing_sources(_artifacts(PYTHON_PKG), sources) == ()


def test_one_missing_name_reports_every_requiring_entry():
    arts = _artifacts(
        '[artifacts.a]\nendpoints = ["crates"]\n[artifacts.b]\nendpoints = ["crates"]\n'
    )
    sources = _sources('[secrets]\nRELEASE_TOKEN = { env = "GH_TOKEN" }\n')
    missing = secretreq.missing_sources(arts, sources)
    assert [m.required_by for m in missing] == [
        "endpoint crates (artifact a)",
        "endpoint crates (artifact b)",
    ]


def test_declared_source_nothing_requires_is_an_orphan():
    sources = _sources(
        "[secrets]\n"
        'RELEASE_TOKEN = { env = "GH_TOKEN" }\n'
        'PYPI_TOKEN = { doppler = "PYPI_TOKEN" }\n'
        'NPM_TOKEN = { doppler = "NPM_TOKEN" }\n'
    )
    assert secretreq.orphans(_artifacts(PYTHON_PKG), sources) == ("NPM_TOKEN",)


def test_reviewer_declared_app_secrets_are_not_orphans():
    sources = _sources(
        "[secrets]\n"
        'CODEX_REVIEW_APP_PRIVATE_KEY = { doppler = "CODEX_REVIEW_APP_PRIVATE_KEY" }\n'
        'CODEX_REVIEW_APP_ID = { doppler = "CODEX_REVIEW_APP_ID" }\n'
    )
    assert secretreq.orphans((), sources) == (
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
    )
    assert secretreq.orphans((), sources, reviewers=("codex",)) == ()


def test_tolerated_names_are_never_orphans():
    sources = _sources(
        "[secrets]\n"
        'SCCACHE_GCS_KEY = { doppler = "SCCACHE_GCS_KEY" }\n'
        'RELEASE_TOKEN = { env = "GH_TOKEN", optional = true }\n'
    )
    assert secretreq.orphans((), sources) == ()


ASC_SOURCES = (
    'ASC_API_KEY_BASE64 = { doppler = "ASC_API_KEY_BASE64" }\n'
    'ASC_API_KEY_ID = { doppler = "ASC_API_KEY_ID" }\n'
    'ASC_API_ISSUER_ID = { doppler = "ASC_API_ISSUER_ID" }\n'
)

APPLE_ID_SOURCES = (
    'APPLE_ID = { doppler = "APPLE_ID" }\n'
    'APPLE_PASSWORD = { doppler = "APPLE_PASSWORD" }\n'
    'APPLE_TEAM_ID = { doppler = "APPLE_TEAM_ID" }\n'
)


def test_signing_artifacts_contribute_one_notary_alternative_requirement():
    reqs = secretreq.alternative_requirements(_artifacts(RUST_CLI))
    assert [(r.sets.label, r.required_by) for r in reqs] == [
        ("notary credentials", "sign-mac stage (artifact lex)")
    ]
    assert secretreq.alternative_requirements(_artifacts(PYTHON_PKG)) == ()


@pytest.mark.parametrize(
    "sources_toml",
    [
        ASC_SOURCES,
        APPLE_ID_SOURCES,
        ASC_SOURCES + APPLE_ID_SOURCES,
        'ASC_API_KEY_ID = { doppler = "ASC_API_KEY_ID" }\n' + APPLE_ID_SOURCES,
    ],
)
def test_either_complete_sourced_trio_satisfies_the_notary_requirement(sources_toml):
    sources = _sources("[secrets]\n" + sources_toml)
    assert secretreq.unsatisfied_alternatives(_artifacts(RUST_CLI), sources) == ()


@pytest.mark.parametrize(
    "sources_toml",
    [
        "",
        'ASC_API_KEY_ID = { doppler = "ASC_API_KEY_ID" }\n'
        'APPLE_ID = { doppler = "APPLE_ID" }\n',
    ],
)
def test_no_complete_trio_is_one_gap_naming_both_alternatives(sources_toml):
    sources = _sources("[secrets]\n" + sources_toml)
    gaps = secretreq.unsatisfied_alternatives(_artifacts(RUST_CLI), sources)
    assert [g.required_by for g in gaps] == ["sign-mac stage (artifact lex)"]
    detail = gaps[0].sets.describe_gap({s.name for s in sources})
    assert detail.startswith("notary credentials: one complete set needed — ")
    assert "ASC API-key trio (missing: " in detail
    assert "Apple-ID trio (missing: " in detail
    for name in ("ASC_API_KEY_BASE64", "APPLE_PASSWORD", "APPLE_TEAM_ID"):
        assert name in detail


def test_notary_trio_sources_are_never_orphans_on_a_signing_map():
    sources = _sources("[secrets]\n" + ASC_SOURCES + APPLE_ID_SOURCES)
    assert secretreq.orphans(_artifacts(RUST_CLI), sources) == ()


def test_notary_trio_sources_are_normal_orphans_without_a_sign_declaration():
    sources = _sources("[secrets]\n" + APPLE_ID_SOURCES)
    assert secretreq.orphans(_artifacts(PYTHON_PKG), sources) == (
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    )


def test_partial_trio_sources_are_accepted_not_orphaned_on_a_signing_map():
    sources = _sources(
        "[secrets]\n"
        + APPLE_ID_SOURCES
        + 'ASC_API_KEY_ID = { doppler = "ASC_API_KEY_ID" }\n'
    )
    assert secretreq.orphans(_artifacts(RUST_CLI), sources) == ()
    assert secretreq.unsatisfied_alternatives(_artifacts(RUST_CLI), sources) == ()


def test_notary_names_are_never_individually_missing():
    missing = secretreq.missing_sources(_artifacts(RUST_CLI), [])
    notary = set(secretreq.NOTARY_SECRETS.names())
    assert notary.isdisjoint({m.name for m in missing})


def test_empty_valid_secret_is_never_reported_missing_on_a_signing_map():
    missing = {m.name for m in secretreq.missing_sources(_artifacts(RUST_CLI), [])}
    assert "APPLE_CERTIFICATE_PASSWORD" not in missing
    assert "APPLE_CERTIFICATE" in missing


def test_empty_valid_secret_still_rides_the_forwarded_accepted_set():
    assert "APPLE_CERTIFICATE_PASSWORD" in secretreq.required_names(
        _artifacts(RUST_CLI)
    )
    assert "APPLE_CERTIFICATE_PASSWORD" in secretreq.accepted_names(
        _artifacts(RUST_CLI)
    )
    assert "APPLE_CERTIFICATE_PASSWORD" in secretreq.secrets_block(_artifacts(RUST_CLI))


def test_declared_funnel_reviewers_contribute_their_credential_pairs():
    names = secretreq.required_names((), reviewers=("codex", "agy"))
    assert names == (
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    )


def test_hosted_reviewers_contribute_no_credentials():
    assert secretreq.required_names((), reviewers=("copilot",)) == ()
    assert secretreq.requirements((), reviewers=("coderabbit", "gemini")) == ()


def test_reviewer_requirements_name_their_declaring_reviewer():
    reqs = secretreq.requirements((), reviewers=("codex",))
    assert [(r.name, r.required_by) for r in reqs] == [
        ("CODEX_REVIEW_APP_PRIVATE_KEY", "reviewer codex ([reviewers] declaration)"),
        ("CODEX_REVIEW_APP_ID", "reviewer codex ([reviewers] declaration)"),
    ]


def test_reviewer_requirements_ride_after_the_artifact_traversal():
    names = secretreq.required_names(_artifacts(PYTHON_PKG), reviewers=("agy",))
    assert names == (
        "RELEASE_TOKEN",
        "PYPI_TOKEN",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    )


def test_declared_reviewer_with_unsourced_credentials_is_missing():
    sources = _sources(
        "[secrets]\n"
        'CODEX_REVIEW_APP_PRIVATE_KEY = { doppler = "CODEX_REVIEW_APP_PRIVATE_KEY" }\n'
    )
    missing = secretreq.missing_sources((), sources, reviewers=("codex",))
    assert [(m.name, m.required_by) for m in missing] == [
        ("CODEX_REVIEW_APP_ID", "reviewer codex ([reviewers] declaration)")
    ]


def test_secrets_block_never_carries_reviewer_credentials():
    block = secretreq.secrets_block(_artifacts(RUST_CLI))
    for backend_name in secretreq.reviewer_requirements(("codex", "agy")):
        assert backend_name.name not in block


def test_secrets_block_lists_exactly_the_accepted_set():
    arts = _artifacts(RUST_CLI)
    block = secretreq.secrets_block(arts)
    lines = block.splitlines()
    assert lines[0] == "secrets:"
    listed = tuple(line.split(":")[0].strip() for line in lines[1:])
    assert listed == secretreq.accepted_names(arts)
    for name in (*secretreq.ASC_NOTARY_SECRETS, *secretreq.APPLE_ID_NOTARY_SECRETS):
        assert name in listed


def test_secrets_block_maps_each_name_to_its_own_secret_ref():
    block = secretreq.secrets_block(_artifacts(PYTHON_PKG))
    assert block == (
        "secrets:\n"
        "  RELEASE_TOKEN: ${{ secrets.RELEASE_TOKEN }}\n"
        "  PYPI_TOKEN: ${{ secrets.PYPI_TOKEN }}"
    )


def test_secrets_block_is_empty_when_nothing_is_required():
    arts = _artifacts('[artifacts.lib]\nbuild = ["python"]\n')
    assert secretreq.secrets_block(arts) == ""
