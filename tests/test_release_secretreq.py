"""Secrets derivation (TOL02-WS02) — the pure requirement traversal.

Fixture-driven over the PRD Testing Decisions' named cases: the SYNC set
(what gh-setup provisions), the VALIDATION set (what preflight checks — see
``test_release_preflight.py`` for the plan-scoped side), ORPHANS, and
MISSING-SOURCE errors. Plus the two cannot-drift guards: the endpoint
registry keys mirror the closed config set, and the cross-org caller's
``secrets:`` block lists exactly the gh-setup sync set (story 46).
"""

import tomllib

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
endpoints = ["gh-release", "crates", "brew"]
sign = true
"""

PYTHON_PKG = """
[artifacts.dist]
build = ["python"]
endpoints = ["gh-release", "pypi"]
"""


# --------------------------------------------------------------------------
# The derived requirement set (stories 43/44)
# --------------------------------------------------------------------------


def test_registry_keys_mirror_the_closed_endpoint_set():
    # One declaration vocabulary: an endpoint parses iff it derives.
    assert tuple(secretreq.ENDPOINT_SECRETS) == config.ENDPOINTS


def test_rust_cli_shape_derives_the_sync_set_in_traversal_order():
    names = secretreq.required_names(_artifacts(RUST_CLI))
    assert names == (
        "RELEASE_TOKEN",  # prepare push — the repo declares endpoints
        "CRATES_IO_KEY",  # endpoint crates
        "HOMEBREW_TAP_TOKEN",  # endpoint brew
        "APPLE_CERTIFICATE",  # sign-mac: the ONE unified cert spelling
        "APPLE_CERTIFICATE_PASSWORD",
        "ASC_API_KEY_BASE64",
        "ASC_API_KEY_ID",
        "ASC_API_ISSUER_ID",
    )


def test_python_pkg_shape_derives_tokens_without_apple_names():
    names = secretreq.required_names(_artifacts(PYTHON_PKG))
    assert names == ("RELEASE_TOKEN", "PYPI_TOKEN")


def test_gh_release_endpoint_requires_nothing_beyond_prepare():
    # The ambient GITHUB_TOKEN publishes the GH release.
    arts = _artifacts('[artifacts.plugin]\nendpoints = ["gh-release"]\n')
    assert secretreq.required_names(arts) == ("RELEASE_TOKEN",)


def test_no_endpoints_means_nothing_required():
    # A repo that never releases has nothing required of it — gh-setup on a
    # docs-only repo must not demand a push token.
    assert secretreq.requirements(()) == ()
    arts = _artifacts('[artifacts.lib]\nbuild = ["python"]\n')
    assert secretreq.requirements(arts) == ()


def test_requirements_name_their_requiring_entry():
    reqs = secretreq.requirements(_artifacts(RUST_CLI))
    by_name = {req.name: req.required_by for req in reqs}
    assert by_name["RELEASE_TOKEN"] == "prepare push"
    assert by_name["CRATES_IO_KEY"] == "endpoint crates (artifact lex)"
    assert by_name["APPLE_CERTIFICATE"] == "sign-mac stage (artifact lex)"


def test_adding_an_endpoint_needs_no_derivation_change():
    # Story 43's contract: the traversal reads the registry; a new adapter is
    # an ENDPOINT_SECRETS entry (plus config.ENDPOINTS), never new code here.
    # Simulated via the registry itself: every closed endpoint derives.
    for endpoint, names in secretreq.ENDPOINT_SECRETS.items():
        assert isinstance(names, tuple)
        assert endpoint in config.ENDPOINTS


# --------------------------------------------------------------------------
# Missing sources (story 45 — the sync-time error)
# --------------------------------------------------------------------------


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
    # Two artifacts publish to crates: the report shows both declarations.
    arts = _artifacts(
        '[artifacts.a]\nendpoints = ["crates"]\n[artifacts.b]\nendpoints = ["crates"]\n'
    )
    sources = _sources('[secrets]\nRELEASE_TOKEN = { env = "GH_TOKEN" }\n')
    missing = secretreq.missing_sources(arts, sources)
    assert [m.required_by for m in missing] == [
        "endpoint crates (artifact a)",
        "endpoint crates (artifact b)",
    ]


# --------------------------------------------------------------------------
# Orphans (story 45 — the flag)
# --------------------------------------------------------------------------


def test_declared_source_nothing_requires_is_an_orphan():
    sources = _sources(
        "[secrets]\n"
        'RELEASE_TOKEN = { env = "GH_TOKEN" }\n'
        'PYPI_TOKEN = { doppler = "PYPI_TOKEN" }\n'
        'NPM_TOKEN = { doppler = "NPM_TOKEN" }\n'  # no npm endpoint anywhere
    )
    assert secretreq.orphans(_artifacts(PYTHON_PKG), sources) == ("NPM_TOKEN",)


def test_extra_required_names_are_not_orphans():
    # gh-setup's non-release requirements (the seeded App secrets) ride in.
    sources = _sources('[secrets]\nFUNNEL_PEM = { doppler = "FUNNEL_PEM" }\n')
    assert secretreq.orphans((), sources) == ("FUNNEL_PEM",)
    assert secretreq.orphans((), sources, extra_required=("FUNNEL_PEM",)) == ()


def test_tolerated_names_are_never_orphans():
    # SCCACHE_GCS_KEY: the optional build-cache credential. RELEASE_TOKEN:
    # provisioned ahead of the artifact map (shipit's own current state).
    sources = _sources(
        "[secrets]\n"
        'SCCACHE_GCS_KEY = { doppler = "SCCACHE_GCS_KEY" }\n'
        'RELEASE_TOKEN = { env = "GH_TOKEN", optional = true }\n'
    )
    assert secretreq.orphans((), sources) == ()


# --------------------------------------------------------------------------
# The cross-org caller block (story 46 — the third consumer)
# --------------------------------------------------------------------------


def test_secrets_block_lists_exactly_the_sync_set():
    # The one derivation feeds gh-setup's sync AND the caller block: asserted
    # equal, so the three consumers of the secret map cannot drift.
    arts = _artifacts(RUST_CLI)
    block = secretreq.secrets_block(arts)
    lines = block.splitlines()
    assert lines[0] == "secrets:"
    listed = tuple(line.split(":")[0].strip() for line in lines[1:])
    assert listed == secretreq.required_names(arts)


def test_secrets_block_maps_each_name_to_its_own_secret_ref():
    block = secretreq.secrets_block(_artifacts(PYTHON_PKG))
    assert block == (
        "secrets:\n"
        "  RELEASE_TOKEN: ${{ secrets.RELEASE_TOKEN }}\n"
        "  PYPI_TOKEN: ${{ secrets.PYPI_TOKEN }}"
    )


def test_secrets_block_is_empty_when_nothing_is_required():
    # A not-yet-release-capable map (no endpoints) derives no requirement, so
    # the block is omitted entirely — a bare `secrets:` key parses as
    # `secrets: null`, which GitHub Actions rejects (it wants a mapping).
    arts = _artifacts('[artifacts.lib]\nbuild = ["python"]\n')
    assert secretreq.secrets_block(arts) == ""
