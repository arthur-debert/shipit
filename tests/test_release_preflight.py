"""The release preflight planner (TOL02-WS02) — pure core + thin verb shell.

Fixture-driven pure-core coverage (PRD Testing Decisions, the lane planner's
release twin): the three declared shapes (rust-CLI, mac-app, python-pkg) →
full plan assertions; the RC guard consumed as plan shape (story 33); the
``--unsigned`` break-glass flip and refusal (story 29); presence validation
(story 28); the phantom-release refusal (the legacy python-pkg preflight
lesson). The verb tests drive :func:`shipit.verbs.release.run_preflight`
with injected git/env seams — no network, no real checkout state.
"""

import json
import tomllib

import pytest

from shipit import config
from shipit.release import ReleaseError, preflight, secretreq
from shipit.release.version import parse_spec, resolve
from shipit.verbs import release as release_verb


def _artifacts(text: str) -> tuple[config.Artifact, ...]:
    return config.load_artifacts(tomllib.loads(text))


def _resolved(raw: str) -> object:
    return resolve(parse_spec(raw), [])


RUST_CLI = _artifacts(
    """
[artifacts.lex]
build = [{ toolchain = "rust", package = "lex-cli" }]
platforms = ["darwin-arm64", "linux-x86_64", "linux-x86_64-musl", "windows-x86_64"]
bundle = { composition = "archive" }
endpoints = ["gh-release", "crates", "brew"]
sign = true
"""
)

MAC_APP = _artifacts(
    """
[artifacts.app]
build = ["npm", { toolchain = "rust" }]
platforms = ["darwin-arm64"]
bundle = { composition = "mac-app", command = ["tauri", "build"], source = "src-tauri/target/release/bundle" }
endpoints = ["gh-release"]
sign = true
"""
)

PYTHON_PKG = _artifacts(
    """
[artifacts.dist]
build = ["python"]
endpoints = ["gh-release", "pypi"]
"""
)


# --------------------------------------------------------------------------
# The three shapes — plan content (story 27)
# --------------------------------------------------------------------------


def test_platform_table_mirrors_the_closed_config_set():
    assert tuple(preflight.PLATFORM_MATRIX) == config.PLATFORMS


def test_rust_cli_shape_plans_matrix_stages_endpoints_secrets():
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3"))
    assert plan.artifacts == ("lex",)
    assert [e.platform for e in plan.matrix] == [
        "darwin-arm64",
        "linux-x86_64",
        "linux-x86_64-musl",
        "windows-x86_64",
    ]
    # The full per-entry vocabulary — the legacy setup-matrix fields, from
    # declarations instead of workflow inputs.
    assert plan.matrix[0].as_matrix_entry() == {
        "artifact": "lex",
        "platform": "darwin-arm64",
        "target": "aarch64-apple-darwin",
        "runner": "macos-latest",
        "sign": True,  # declared signing meets a darwin platform
        "bundle": True,  # the archive composition applies everywhere
        "ext_archive": ".tar.gz",
        "ext_bin": "",
        "package_arch": "arm64",
    }
    assert plan.matrix[3].as_matrix_entry() == {
        "artifact": "lex",
        "platform": "windows-x86_64",
        "target": "x86_64-pc-windows-msvc",
        "runner": "windows-latest",
        "sign": False,  # sign is darwin-only, resolved once per entry
        "bundle": True,  # the archive composition applies everywhere
        "ext_archive": ".zip",
        "ext_bin": ".exe",
        "package_arch": "amd64",
    }
    # The archive composition bundles on every leg → the bundle stages are
    # live, and the darwin leg's sign routes the signer's archive leg
    # (TOL02-WS08 #779: sign = true now requires a signable composition).
    assert plan.stages == (
        "preflight",
        "prepare",
        "bundle",
        "assert-bundle",
        "sign",
        "publish",
    )
    assert plan.endpoints == ("gh-release", "crates", "brew")
    # The demanded conjunction carries the cert pair only; the notary trios
    # ride the plan's either-satisfies requirement (#746).
    assert plan.secrets == (
        "RELEASE_TOKEN",
        "CARGO_REGISTRY_TOKEN",
        "HOMEBREW_TAP_TOKEN",
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
    )
    assert plan.secret_alternatives == (secretreq.NOTARY_SECRETS,)
    assert (plan.prerelease, plan.tag_only, plan.unsigned) == (False, False, False)


def test_mac_app_shape_plans_bundle_and_sign():
    plan = preflight.plan(MAC_APP, _resolved("2.0.0"))
    # One artifact, one darwin platform — two build targets still mean ONE
    # matrix entry per platform (the artifact is the unit, not the leg).
    assert [(e.artifact, e.platform, e.sign) for e in plan.matrix] == [
        ("app", "darwin-arm64", True)
    ]
    assert plan.stages == (
        "preflight",
        "prepare",
        "bundle",
        "assert-bundle",
        "sign",
        "publish",
    )
    assert plan.endpoints == ("gh-release",)
    assert plan.secrets == ("RELEASE_TOKEN", *secretreq.SIGN_MAC_CERT_SECRETS)
    assert plan.secret_alternatives == (secretreq.NOTARY_SECRETS,)


ELECTRON_SIGNED = _artifacts(
    """
[artifacts.app]
build = ["npm"]
platforms = ["darwin-arm64", "linux-x86_64"]
bundle = { composition = "electron", command = ["npm", "run", "dist"], source = "release" }
endpoints = ["gh-release"]
sign = true
"""
)


def test_signed_electron_plans_the_sign_stage_and_apple_creds_via_the_standard_path():
    # WS14 #790: electron is signable, so a `sign = true` electron artifact
    # rides the SAME sign stage + Apple cert/notary derivation as mac-app — no
    # composition-keyed build-time secret. The darwin leg signs, the linux leg
    # does not.
    plan = preflight.plan(ELECTRON_SIGNED, _resolved("1.0.0"))
    assert [(e.platform, e.sign) for e in plan.matrix] == [
        ("darwin-arm64", True),
        ("linux-x86_64", False),
    ]
    assert "sign" in plan.stages
    assert plan.secrets == ("RELEASE_TOKEN", *secretreq.SIGN_MAC_CERT_SECRETS)
    assert plan.secret_alternatives == (secretreq.NOTARY_SECRETS,)
    # The loud missing-secret contract still holds (nothing electron-specific).
    missing = preflight.missing_secrets(plan, {"RELEASE_TOKEN": "t"})
    assert "APPLE_CERTIFICATE" in missing


def test_mixed_map_flags_bundle_per_entry_so_build_only_legs_skip_bundling():
    # codex, round 1: a bundled artifact beside a build-only one. `bundle`
    # is a plan-WIDE stage flag (live because SOME artifact bundles), but the
    # fan includes BOTH build-bearing artifacts. Each entry carries its own
    # `bundle` decision so wf-build bundles/uploads only the bundled leg —
    # the build-only leg would otherwise passthrough (stage nothing) and trip
    # the upload's `if-no-files-found: error`, and wf-publish's assert would
    # then fan over a `bundle-helper-*` artifact that was never uploaded.
    arts = _artifacts(
        """
[artifacts.tool]
build = [{ toolchain = "rust", package = "tool-cli" }]
platforms = ["linux-x86_64"]
bundle = { composition = "archive" }
endpoints = ["gh-release"]

[artifacts.helper]
build = [{ toolchain = "rust", package = "helper-cli" }]
platforms = ["linux-x86_64"]
endpoints = ["gh-release"]
"""
    )
    plan = preflight.plan(arts, _resolved("1.0.0"))
    # The plan-wide stage is live (some artifact bundles)...
    assert "bundle" in plan.stages
    assert "assert-bundle" in plan.stages
    # ...but the per-entry flag distinguishes the two legs.
    assert [(e.artifact, e.bundle) for e in plan.matrix] == [
        ("tool", True),
        ("helper", False),
    ]
    # The unsigned-matrix projection wf-prepare emits — `select((.sign | not)
    # and .bundle)` — carries ONLY the bundled leg, so the assert job never
    # tries to download a bundle the build-only leg never produced.
    unsigned_assert = [e for e in plan.matrix if not e.sign and e.bundle]
    assert [e.artifact for e in unsigned_assert] == ["tool"]


def test_tarball_bundles_but_does_not_assert_a_binary():
    # TOL02-WS16 #792: a generated-parser tarball is a SOURCE composition — it
    # bundles (the stage is live) but has no main binary, so the scar-#2
    # assert-bundle guard is NOT live for it (running it over the source
    # `.tar.gz` would hard-fail with "no main binary").
    arts = _artifacts(
        """
[artifacts.parser]
build = ["tree-sitter"]
platforms = ["linux-x86_64"]
bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }
endpoints = ["gh-release", "notify-downstreams"]
downstreams = ["lex-fmt/vscode"]
"""
    )
    plan = preflight.plan(arts, _resolved("1.0.0"))
    assert "bundle" in plan.stages
    assert "assert-bundle" not in plan.stages
    assert [(e.artifact, e.bundle) for e in plan.matrix] == [("parser", True)]


def test_bundle_flag_is_platform_aware_for_a_platform_specific_composition():
    # Umbrella second-look (codex): ONE artifact whose composition is
    # platform-specific (deb → linux only) but which spans several platforms.
    # A whole-artifact `bundle is not None` flag would mark the darwin leg
    # bundle-bearing too; wf-build would then run `release bundle` there, the
    # verb would skip deb (composition does not apply to the target — same
    # `Composition.applies` predicate), stage nothing, and the upload would
    # trip `if-no-files-found: error`. The per-entry flag must mirror the
    # verb's skip: bundle only the leg the composition applies to.
    arts = _artifacts(
        """
[artifacts.tool]
build = [{ toolchain = "rust", package = "tool-cli" }]
platforms = ["darwin-arm64", "linux-x86_64"]
bundle = { composition = "deb" }
endpoints = ["gh-release"]
"""
    )
    plan = preflight.plan(arts, _resolved("1.0.0"))
    assert [(e.platform, e.bundle) for e in plan.matrix] == [
        ("darwin-arm64", False),  # deb does not apply to aarch64-apple-darwin
        ("linux-x86_64", True),  # deb applies to x86_64-unknown-linux-gnu
    ]
    # The bundle stage stays live (the artifact DOES bundle, on its linux leg).
    assert "bundle" in plan.stages
    assert "assert-bundle" in plan.stages


def test_composition_matching_no_declared_platform_is_a_loud_refusal():
    # The other half of the platform-aware flag: a composition (deb → linux)
    # declared on an artifact whose ONLY platform it cannot bundle (darwin)
    # produces no bundle on any leg. Rather than silently drop the stage (or,
    # pre-fix, trip the CI upload's `if-no-files-found: error`), preflight
    # refuses loudly — the same contract as `sign = true` demanding a darwin
    # lane. Keeps `bundle_live` and the matrix from ever disagreeing.
    arts = _artifacts(
        """
[artifacts.tool]
build = [{ toolchain = "rust", package = "tool-cli" }]
platforms = ["darwin-arm64"]
bundle = { composition = "deb" }
endpoints = ["gh-release"]
"""
    )
    with pytest.raises(ReleaseError, match="applies to none"):
        preflight.plan(arts, _resolved("1.0.0"))


def test_python_pkg_shape_defaults_to_the_linux_lane_and_skips_apple_names():
    plan = preflight.plan(PYTHON_PKG, _resolved("0.3.1"))
    assert [(e.platform, e.runner, e.sign) for e in plan.matrix] == [
        ("linux-x86_64", "ubuntu-latest", False)
    ]
    assert plan.stages == ("preflight", "prepare", "publish")
    assert plan.endpoints == ("gh-release", "pypi")
    # Signing not declared → only signing-relevant names are NOT checked
    # (story 28's flip side: one definition, no disagreeing checks).
    assert plan.secrets == ("RELEASE_TOKEN", "PYPI_TOKEN")


def test_endpointless_build_artifact_emits_matrix_but_no_endpoints():
    # A helper artifact rides the matrix; the endpoint set comes from the
    # artifacts that declare distribution.
    arts = PYTHON_PKG + _artifacts('[artifacts.docs]\nbuild = ["python"]\n')
    plan = preflight.plan(arts, _resolved("0.3.1"))
    assert plan.artifacts == ("dist", "docs")
    assert [e.artifact for e in plan.matrix] == ["dist", "docs"]
    assert plan.endpoints == ("gh-release", "pypi")


def test_declaration_order_rides_the_matrix_and_registry_order_the_endpoints():
    arts = _artifacts(
        '[artifacts.b]\nbuild = ["python"]\nendpoints = ["brew", "gh-release"]\n'
    )
    plan = preflight.plan(arts, _resolved("1.0.0"))
    # Endpoints normalize to the closed registry's canonical order
    # (gh-release first, brew — the derived endpoint — last).
    assert plan.endpoints == ("gh-release", "brew")


def test_to_dict_is_the_declared_json_surface():
    plan = preflight.plan(PYTHON_PKG, _resolved("0.3.1"), event="local")
    payload = plan.to_dict()
    assert set(payload) == {
        "version",
        "tag",
        "prerelease",
        "tag_only",
        "event",
        "unsigned",
        "artifacts",
        "matrix",
        "stages",
        "endpoints",
        "secrets",
        "secret_alternatives",
    }
    assert payload["event"] == "local"
    assert payload["matrix"][0]["target"] == "x86_64-unknown-linux-gnu"
    # A non-signing plan carries no either-satisfies requirement.
    assert payload["secret_alternatives"] == []


def test_to_dict_projects_the_notary_alternatives_for_a_signing_plan():
    payload = preflight.plan(RUST_CLI, _resolved("1.2.3")).to_dict()
    assert payload["secret_alternatives"] == [
        {
            "label": "notary credentials",
            "alternatives": [
                {
                    "label": "ASC API-key trio",
                    "names": [
                        "ASC_API_KEY_BASE64",
                        "ASC_API_KEY_ID",
                        "ASC_API_ISSUER_ID",
                    ],
                },
                {
                    "label": "Apple-ID trio",
                    "names": ["APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"],
                },
            ],
        }
    ]


# --------------------------------------------------------------------------
# The RC guard as plan shape (story 33)
# --------------------------------------------------------------------------


def test_release_rc_plans_gh_release_only_with_prerelease_marked():
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3-release-rc"))
    # External endpoints are ABSENT from the plan, not filtered later in
    # YAML; the guard's enforcement stays central in the publish verb (WS05).
    assert plan.endpoints == ("gh-release",)
    assert plan.prerelease is True
    assert plan.tag_only is True
    # The endpoint secrets follow the collapsed set; sign still runs (a
    # live-fire rc exercises the whole pipeline).
    assert plan.secrets == ("RELEASE_TOKEN", *secretreq.SIGN_MAC_CERT_SECRETS)
    assert plan.secret_alternatives == (secretreq.NOTARY_SECRETS,)
    assert "sign" in plan.stages


def test_plain_rc_prerelease_keeps_the_declared_endpoints():
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3-rc.1"))
    assert plan.prerelease is True
    assert plan.tag_only is False
    assert plan.endpoints == ("gh-release", "crates", "brew")


# --------------------------------------------------------------------------
# --unsigned break-glass (story 29)
# --------------------------------------------------------------------------


def test_unsigned_flips_the_plan_to_the_unsigned_path():
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3"), unsigned=True)
    assert plan.unsigned is True
    assert "sign" not in plan.stages
    assert all(e.sign is False for e in plan.matrix)
    # The Apple names drop out of the required set with the stage — and so
    # does the notary either-set requirement.
    assert plan.secrets == (
        "RELEASE_TOKEN",
        "CARGO_REGISTRY_TOKEN",
        "HOMEBREW_TAP_TOKEN",
    )
    assert plan.secret_alternatives == ()


def test_unsigned_is_refused_when_nothing_would_sign():
    # No declared signing → nothing to break-glass. (A `sign = true` map that
    # never meets darwin cannot even be constructed — config refuses it at
    # parse; see test_config_artifacts.py.)
    with pytest.raises(ReleaseError, match="no sign stage to skip"):
        preflight.plan(PYTHON_PKG, _resolved("0.3.1"), unsigned=True)


# --------------------------------------------------------------------------
# Refusals and presence validation (stories 27/28)
# --------------------------------------------------------------------------


def test_zero_endpoint_map_is_a_phantom_release_refusal():
    arts = _artifacts('[artifacts.lib]\nbuild = ["python"]\n')
    with pytest.raises(ReleaseError, match="phantom release"):
        preflight.plan(arts, _resolved("1.0.0"))
    with pytest.raises(ReleaseError, match="phantom release"):
        preflight.plan((), _resolved("1.0.0"))


def test_unknown_event_is_a_caller_bug():
    with pytest.raises(ValueError, match="unknown release event"):
        preflight.plan(PYTHON_PKG, _resolved("1.0.0"), event="push")


#: Base env satisfying RUST_CLI's plan conjunction (everything but notary).
_SIGNING_BASE_ENV = {
    "RELEASE_TOKEN": "t",
    "CARGO_REGISTRY_TOKEN": "c",
    "HOMEBREW_TAP_TOKEN": "h",
    "APPLE_CERTIFICATE": "cert",
    "APPLE_CERTIFICATE_PASSWORD": "pw",
}

_ASC_ENV = {"ASC_API_KEY_BASE64": "k", "ASC_API_KEY_ID": "i", "ASC_API_ISSUER_ID": "u"}
_APPLE_ID_ENV = {"APPLE_ID": "a", "APPLE_PASSWORD": "p", "APPLE_TEAM_ID": "t"}


def test_missing_secrets_reports_absent_and_empty_names_in_plan_order():
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3"))
    env = {
        "RELEASE_TOKEN": "t",
        "CARGO_REGISTRY_TOKEN": "",  # empty is absent — an empty token cannot publish
        "APPLE_CERTIFICATE": "c",
        "APPLE_CERTIFICATE_PASSWORD": "pw",  # non-empty here — not the name under test
        **_ASC_ENV,
    }
    # APPLE_CERTIFICATE_PASSWORD is NOT reported: an empty value is valid for it
    # (#892), but here it is set anyway; the empty-value assertion is the
    # dedicated test below.
    assert preflight.missing_secrets(plan, env) == (
        "CARGO_REGISTRY_TOKEN",
        "HOMEBREW_TAP_TOKEN",
    )


def test_missing_secrets_accepts_an_empty_apple_certificate_password():
    # sign.py's contract: a passwordless .p12 is legal PKCS#12, so an EMPTY
    # APPLE_CERTIFICATE_PASSWORD is valid presence — preflight must not demand
    # it non-empty or it would strand a repo the signer would sign fine (#892).
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3"))
    empty = {**_SIGNING_BASE_ENV, **_ASC_ENV, "APPLE_CERTIFICATE_PASSWORD": ""}
    assert preflight.missing_secrets(plan, empty) == ()
    # Even entirely ABSENT (the caller forwarded nothing) is accepted — the
    # signer defaults it to empty and never fails on it.
    absent = {k: v for k, v in empty.items() if k != "APPLE_CERTIFICATE_PASSWORD"}
    assert preflight.missing_secrets(plan, absent) == ()
    # But its non-empty-required counterpart APPLE_CERTIFICATE is still demanded.
    no_cert = {k: v for k, v in empty.items() if k != "APPLE_CERTIFICATE"}
    assert "APPLE_CERTIFICATE" in preflight.missing_secrets(plan, no_cert)


def test_empty_valid_secrets_are_pinned_to_the_signer_contract():
    # ONE authority for what "present" means per name (#892): the empty-valid
    # set must name exactly the signer's empty-accepting password secret, so
    # preflight and sign.py can never drift back into contradiction.
    from shipit.release import sign

    assert secretreq.EMPTY_VALID_SECRETS == frozenset({sign.CERT_PASSWORD_SECRET})
    # And an empty-valid name is still a genuine requirement (synced/forwarded),
    # just one whose VALUE check is relaxed — never dropped from the set.
    assert sign.CERT_PASSWORD_SECRET in secretreq.SIGN_MAC_CERT_SECRETS


def test_missing_secrets_is_empty_when_the_plan_is_fully_provisioned():
    plan = preflight.plan(PYTHON_PKG, _resolved("0.3.1"))
    env = {"RELEASE_TOKEN": "t", "PYPI_TOKEN": "p"}
    assert preflight.missing_secrets(plan, env) == ()


@pytest.mark.parametrize(
    "notary_env",
    [
        _ASC_ENV,  # ASC-only
        _APPLE_ID_ENV,  # Apple-ID-only: the first-class CI alternative (#746)
        {**_ASC_ENV, **_APPLE_ID_ENV},  # both (the signer prefers ASC)
        # Partial ASC beside a COMPLETE Apple-ID trio: satisfied.
        {"ASC_API_KEY_ID": "i", **_APPLE_ID_ENV},
    ],
)
def test_missing_secrets_accepts_either_complete_notary_trio(notary_env):
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3"))
    assert preflight.missing_secrets(plan, {**_SIGNING_BASE_ENV, **notary_env}) == ()


@pytest.mark.parametrize(
    "notary_env",
    [
        {},  # neither trio at all
        # Both trios incomplete (an empty value counts as absent).
        {"ASC_API_KEY_ID": "i", "APPLE_ID": "a", "APPLE_PASSWORD": ""},
    ],
)
def test_missing_secrets_reports_one_notary_gap_naming_both_trios(notary_env):
    # No complete trio → ONE diagnostic entry naming what is missing from
    # EVERY alternative — never the six names demanded one by one.
    plan = preflight.plan(RUST_CLI, _resolved("1.2.3"))
    missing = preflight.missing_secrets(plan, {**_SIGNING_BASE_ENV, **notary_env})
    assert len(missing) == 1
    (gap,) = missing
    assert gap.startswith("notary credentials: one complete set needed — ")
    assert "ASC API-key trio (missing: " in gap
    assert "Apple-ID trio (missing: " in gap
    assert "ASC_API_KEY_BASE64" in gap
    assert "APPLE_TEAM_ID" in gap


# --------------------------------------------------------------------------
# The verb shell — injected seams, ADR-0030 rendering
# --------------------------------------------------------------------------


class FakeGit:
    """The two reads preflight makes: repo root and existing tags."""

    def __init__(self, root, tags=()):
        self._root = root
        self._tags = list(tags)

    def repo_root(self, *, cwd):
        return self._root

    def list_tags(self, *, cwd):
        return list(self._tags)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(
        '[artifacts.dist]\nbuild = ["python"]\nendpoints = ["gh-release", "pypi"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_run_preflight_emits_the_json_plan(repo, capsys):
    rc = release_verb.run_preflight(
        parse_spec("1.0.0"),
        as_json=True,
        gitio=FakeGit(str(repo)),
        env={"RELEASE_TOKEN": "t", "PYPI_TOKEN": "p"},
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "1.0.0"
    assert payload["endpoints"] == ["gh-release", "pypi"]
    assert payload["secrets"] == ["RELEASE_TOKEN", "PYPI_TOKEN"]


def test_run_preflight_resolves_bump_words_like_prepare_will(repo, capsys):
    rc = release_verb.run_preflight(
        parse_spec("minor"),
        as_json=True,
        gitio=FakeGit(str(repo), tags=["v1.2.3"]),
        env={"RELEASE_TOKEN": "t", "PYPI_TOKEN": "p"},
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["version"] == "1.3.0"


def test_run_preflight_hard_fails_on_missing_secrets(repo, capsys):
    rc = release_verb.run_preflight(
        parse_spec("1.0.0"),
        gitio=FakeGit(str(repo)),
        env={"RELEASE_TOKEN": "t"},  # PYPI_TOKEN absent
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "PYPI_TOKEN" in err


def test_run_preflight_plan_only_skips_presence_never_the_facts(repo, capsys):
    # Per-stage dispatch (#780): the stage blocks' standalone plan job runs
    # preflight --plan-only in an environment that deliberately carries no
    # secrets — the facts (matrix, stages, endpoints, secret NAMES) still
    # compute; only the presence hard-fail is skipped. Presence was the
    # source run's preflight's job, and each stage's verb still validates
    # its own names before acting.
    rc = release_verb.run_preflight(
        parse_spec("1.0.0"),
        plan_only=True,
        as_json=True,
        gitio=FakeGit(str(repo)),
        env={},  # every required secret absent
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "1.0.0"
    assert payload["endpoints"] == ["gh-release", "pypi"]
    # The requirement NAMES still ride the plan — only presence is skipped.
    assert payload["secrets"] == ["RELEASE_TOKEN", "PYPI_TOKEN"]


def test_run_preflight_text_rendering_carries_the_plan_summary(repo, capsys):
    rc = release_verb.run_preflight(
        parse_spec("1.0.0-release-rc"),
        gitio=FakeGit(str(repo)),
        env={"RELEASE_TOKEN": "t", "PYPI_TOKEN": "p"},
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "release preflight: 1.0.0-release-rc (prerelease" in out
    assert "endpoints  gh-release" in out
    assert "rc guard" in out


def test_run_preflight_unsigned_records_the_break_glass_event(repo, capsys, caplog):
    (repo / ".shipit.toml").write_text(
        "[artifacts.app]\n"
        'build = ["rust"]\n'
        'platforms = ["darwin-arm64"]\n'
        'bundle = { composition = "archive" }\n'
        'endpoints = ["gh-release"]\n'
        "sign = true\n",
        encoding="utf-8",
    )
    with caplog.at_level("INFO", logger="shipit.release"):
        rc = release_verb.run_preflight(
            parse_spec("1.0.0"),
            unsigned=True,
            gitio=FakeGit(str(repo)),
            env={"RELEASE_TOKEN": "t"},
        )
    assert rc == 0
    events = [
        r for r in caplog.records if getattr(r, "_event", None) == "release.unsigned"
    ]
    assert len(events) == 1  # every use is recorded, exactly once
    out = capsys.readouterr().out
    assert "UNSIGNED" in out


def test_run_preflight_refused_unsigned_records_no_event(repo, capsys, caplog):
    with caplog.at_level("INFO", logger="shipit.release"):
        rc = release_verb.run_preflight(
            parse_spec("1.0.0"),
            unsigned=True,
            gitio=FakeGit(str(repo)),
            env={"RELEASE_TOKEN": "t", "PYPI_TOKEN": "p"},
        )
    assert rc == 1  # nothing to break-glass — refused before any record
    assert not [
        r for r in caplog.records if getattr(r, "_event", None) == "release.unsigned"
    ]


def test_run_preflight_outside_a_checkout_is_a_domain_refusal(repo, capsys):
    rc = release_verb.run_preflight(parse_spec("1.0.0"), gitio=FakeGit(None), env={})
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The @vN pin gate (#917) — a missing floating-major branch fails loud here,
# never as a raw HTTP 422 at GitHub's dispatch-time workflow resolution.
# --------------------------------------------------------------------------


def _write_caller(repo, ref="v1"):
    wfdir = repo / ".github" / "workflows"
    wfdir.mkdir(parents=True, exist_ok=True)
    (wfdir / "shipit-release.yml").write_text(
        "on: workflow_dispatch\njobs:\n"
        f"  release:\n    uses: o/r/.github/workflows/wf-release.yml@{ref}\n",
        encoding="utf-8",
    )


def _preflight_env():
    return {"RELEASE_TOKEN": "t", "PYPI_TOKEN": "p"}


def test_run_preflight_refuses_when_a_vn_pin_does_not_resolve(repo, capsys):
    _write_caller(repo)
    rc = release_verb.run_preflight(
        parse_spec("1.0.0"),
        gitio=FakeGit(str(repo)),
        env=_preflight_env(),
        resolve_ref=lambda repo_slug, ref: False,  # the floating v1 is missing
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 422" in err and "o/r @ v1" in err
    # The one-command bootstrap remediation is named.
    assert "refs/heads/v1" in err


def test_run_preflight_passes_when_every_pin_resolves(repo, capsys):
    _write_caller(repo)
    seen = []
    rc = release_verb.run_preflight(
        parse_spec("1.0.0"),
        as_json=True,
        gitio=FakeGit(str(repo)),
        env=_preflight_env(),
        resolve_ref=lambda repo_slug, ref: seen.append((repo_slug, ref)) or True,
    )
    assert rc == 0
    assert seen == [("o/r", "v1")]  # the pin was probed exactly once
    assert json.loads(capsys.readouterr().out)["version"] == "1.0.0"


def test_run_preflight_plan_only_skips_the_pin_gate(repo, capsys):
    # The stage blocks' standalone plan job runs INSIDE an already-resolved
    # dispatch — its pins provably resolve, so the gate (and its network probe)
    # is skipped entirely.
    _write_caller(repo)

    def _boom(repo_slug, ref):
        raise AssertionError("plan_only must not probe pins")

    rc = release_verb.run_preflight(
        parse_spec("1.0.0"),
        plan_only=True,
        as_json=True,
        gitio=FakeGit(str(repo)),
        env={},
        resolve_ref=_boom,
    )
    assert rc == 0


def test_missing_pin_refusal_names_every_pin_and_its_bootstrap():
    text = preflight.missing_pin_refusal([("o/r", "v1"), ("o/r2", "v2")])
    assert "HTTP 422" in text
    assert "  - o/r @ v1" in text and "  - o/r2 @ v2" in text
    assert "refs/heads/v1" in text and "refs/heads/v2" in text
    assert "advance-major" in text


# --------------------------------------------------------------------------
# The dogfood declaration (#774): shipit's OWN release surface
# --------------------------------------------------------------------------


def _dogfood_artifacts() -> tuple[config.Artifact, ...]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return _artifacts((root / ".shipit.toml").read_text(encoding="utf-8"))


def test_shipit_declares_the_tag_is_the_payload_surface():
    # The #774 cutover pin: shipit's own [artifacts] map is ONE no-build
    # artifact publishing to gh-release only — consumers ride the git pin
    # (ADR-0033) and the @v1 workflow refs (ADR-0010), so the tag IS the
    # payload and any build/bundle/sign declaration here would be a phantom
    # asset. Growing this surface is an explicit decision, not a drift.
    artifacts = _dogfood_artifacts()
    assert [a.name for a in artifacts] == ["shipit"]
    (shipit,) = artifacts
    assert shipit.build == ()
    assert shipit.endpoints == ("gh-release",)
    assert shipit.bundle is None
    assert shipit.sign is False


@pytest.mark.parametrize("raw", ["1.0.0", "1.0.0-release-rc"])
def test_shipit_dogfood_plan_is_publishable_tag_only_release(raw):
    # The phantom-release refusal is exactly what #774 fixes: the dogfood
    # declaration must PLAN — for both the live-fire rc and the real 1.0.0
    # — as the tag-only shape: empty matrix (nothing builds), stages
    # preflight → prepare → publish, gh-release as the sole endpoint, and
    # RELEASE_TOKEN as the sole required secret (gh-release rides
    # GITHUB_TOKEN).
    release_plan = preflight.plan(_dogfood_artifacts(), _resolved(raw))
    assert release_plan.artifacts == ("shipit",)
    assert release_plan.matrix == ()
    assert release_plan.stages == ("preflight", "prepare", "publish")
    assert release_plan.endpoints == ("gh-release",)
    assert release_plan.secrets == ("RELEASE_TOKEN",)
    assert release_plan.secret_alternatives == ()
