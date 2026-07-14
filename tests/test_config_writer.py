"""Unit tests for the .shipit.toml manifest writer + content hashing."""

import tomllib

import pytest

from shipit import config


def test_content_hash_is_sha256_prefixed():
    h = config.content_hash(b"hello")
    assert h.startswith("sha256:")
    # Stable, content-addressed.
    assert h == config.content_hash(b"hello")
    assert h != config.content_hash(b"world")


def test_write_manifest_fresh_file_roundtrips(tmp_path):
    p = tmp_path / ".shipit.toml"
    managed = {
        "skills/to-spec/SKILL.md": "sha256:aaa",
        "AGENTS.md#shipit-block": "sha256:bbb",
        "bin/shipit": "sha256:ccc",
    }
    config.write_manifest(p, version="deadbeef", managed=managed)

    cfg = config.load(p)
    assert config.shipit_version(cfg) == "deadbeef"
    assert config.load_managed(cfg) == managed
    # The path keys with '/' and '#' survive a tomllib round-trip.
    raw = tomllib.loads(p.read_text())
    assert raw["managed"]["AGENTS.md#shipit-block"] == "sha256:bbb"


def test_write_manifest_preserves_existing_secrets(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        "[secrets]\n"
        'CARGO_REGISTRY_TOKEN = { doppler = "CRATES_IO_KEY" }\n'
        'GH_PAT = { env = "SHIPIT_GH_PAT" }\n'
    )
    config.write_manifest(p, version="v1", managed={"bin/shipit": "sha256:x"})

    cfg = config.load(p)
    # [secrets] is untouched, [shipit]/[managed] are added.
    secrets = config.load_secrets(cfg)
    names = {s.name for s in secrets}
    assert names == {"CARGO_REGISTRY_TOKEN", "GH_PAT"}
    assert config.shipit_version(cfg) == "v1"
    assert config.load_managed(cfg) == {"bin/shipit": "sha256:x"}


def test_write_manifest_preserves_consumer_lint_section(tmp_path):
    # Reconcile-safety crux (#484): the consumer-owned [lint].ignore seam lives
    # in .shipit.toml, so a `shipit install` manifest rewrite must NEVER clobber
    # it — write_manifest strips only [shipit]/[managed] and leaves [lint] verbatim.
    p = tmp_path / ".shipit.toml"
    p.write_text(
        "[lint]\n"
        'ignore = ["crates/lex-babel/tests/fixtures/**", "CHANGELOG.md"]\n'
        '\n[shipit]\nversion = "old"\n'
        '\n[managed]\n"bin/shipit" = "sha256:old"\n'
    )
    # Simulate a reconcile round-trip: rewrite the managed tables.
    config.write_manifest(p, version="new", managed={"bin/shipit": "sha256:new"})

    cfg = config.load(p)
    assert config.load_lint_ignore(cfg) == [
        "crates/lex-babel/tests/fixtures/**",
        "CHANGELOG.md",
    ]
    assert config.shipit_version(cfg) == "new"
    assert config.load_managed(cfg) == {"bin/shipit": "sha256:new"}


def test_write_manifest_replaces_prior_shipit_tables(tmp_path):
    p = tmp_path / ".shipit.toml"
    config.write_manifest(p, version="v1", managed={"a": "sha256:1", "b": "sha256:2"})
    config.write_manifest(p, version="v2", managed={"a": "sha256:9"})

    cfg = config.load(p)
    assert config.shipit_version(cfg) == "v2"
    # The stale "b" entry is gone — the section is replaced, not merged textually.
    assert config.load_managed(cfg) == {"a": "sha256:9"}
    # And only one [shipit] table exists.
    assert p.read_text().count("[shipit]") == 1


def test_write_manifest_strips_a_commented_managed_header(tmp_path):
    # A hand-edited `[managed]  # note` is still THE [managed] header (a
    # trailing comment is valid TOML after a header) — the re-stamp must strip
    # it, not leave a duplicate [managed] table behind (#617).
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[shipit]  # stamped by install\nversion = "old"\n'
        '\n[managed]  # pristine map\n"bin/shipit" = "sha256:old"\n'
    )
    config.write_manifest(p, version="new", managed={"bin/shipit": "sha256:new"})
    cfg = config.load(p)  # a duplicated table would fail to parse here
    assert config.shipit_version(cfg) == "new"
    assert config.load_managed(cfg) == {"bin/shipit": "sha256:new"}
    assert p.read_text().count("[managed]") == 1


# --------------------------------------------------------------------------
# The [managed.decline] policy sub-table (#600) — consumer-owned, re-stamp-safe
# --------------------------------------------------------------------------


def test_load_declines_parses_the_keep_list(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[managed.decline]\nkeep = ["bin/shipit", "lefthook.yml"]\n')
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit", "lefthook.yml")
    # The policy sub-table is NOT a pristine entry.
    assert config.load_managed(cfg) == {}


def test_load_declines_defaults_empty():
    assert config.load_declines({}, "") == ()
    assert config.load_declines({"managed": {"bin/shipit": "sha256:x"}}, "") == ()


def test_load_declines_accepts_a_header_with_a_trailing_comment(tmp_path):
    # A trailing `# comment` is valid after a TOML header; the header-form check
    # must strip it, not read the commented line as a missing header and reject.
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[managed.decline]  # keep our own bin/shipit\nkeep = ["bin/shipit"]\n'
    )
    assert config.load_declines(config.load(p), p.read_text()) == ("bin/shipit",)


def test_load_declines_rejects_the_dotted_form(tmp_path):
    # A dotted `decline.keep` under [managed] parses to the same dict as the
    # header form, but the [managed] re-stamp would strip it — so install would
    # silently un-decline on the next run. Refuse it at parse rather than accept
    # a policy that evaporates (#600).
    p = tmp_path / ".shipit.toml"
    p.write_text('[managed]\ndecline.keep = ["bin/shipit"]\n')
    with pytest.raises(config.ConfigError, match="own header"):
        config.load_declines(config.load(p), p.read_text())


@pytest.mark.parametrize(
    "body",
    [
        "[managed]\ndecline = 42\n",  # scalar where the table is expected
        '[managed.decline]\nkeep = "bin/shipit"\n',  # scalar keep
        "[managed.decline]\nkeep = [1]\n",  # non-string entry
        '[managed.decline]\nkeep = [""]\n',  # empty key
        '[managed.decline]\nkept = ["bin/shipit"]\n',  # typo'd key dies at parse
    ],
)
def test_load_declines_rejects_malformed_shapes(tmp_path, body):
    p = tmp_path / ".shipit.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError):
        config.load_declines(config.load(p), p.read_text())


def test_write_manifest_preserves_managed_decline(tmp_path):
    # The decline is consumer-owned policy riding inside [managed]'s namespace:
    # the [shipit]/[managed] re-stamp strips only those two headers' bodies, so
    # the [managed.decline] header (and its keep list) must survive verbatim —
    # the whole point of the durable decline (#600) is outliving every reconcile.
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[managed.decline]\nkeep = ["bin/shipit"]\n'
        '\n[shipit]\nversion = "old"\n'
        '\n[managed]\n"bin/shipit" = "sha256:old"\n'
    )
    config.write_manifest(p, version="new", managed={"lefthook.yml": "sha256:new"})
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit",)
    assert config.shipit_version(cfg) == "new"
    # The declined unit's stale pristine entry was dropped with the re-stamp.
    assert config.load_managed(cfg) == {"lefthook.yml": "sha256:new"}


def test_write_manifest_preserves_a_trailing_managed_decline(tmp_path):
    # A consumer appends [managed.decline] at the very end (after the machine
    # tables install wrote) — the strip keeps it and the re-appended [managed]
    # after it is still valid TOML.
    p = tmp_path / ".shipit.toml"
    config.write_manifest(p, version="v1", managed={"bin/shipit": "sha256:x"})
    p.write_text(p.read_text() + '\n[managed.decline]\nkeep = ["bin/shipit"]\n')
    config.write_manifest(p, version="v2", managed={})
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit",)
    assert config.load_managed(cfg) == {}


def test_write_manifest_preserves_a_commented_managed_decline(tmp_path):
    # The sibling #617 failure mode: a `[managed.decline]  # comment` header
    # went unrecognized by the strip's line scan, so it failed to terminate the
    # [managed] body skip and the consumer's decline policy was silently
    # dropped by the re-stamp.
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[managed]\n"bin/shipit" = "sha256:old"\n'
        '\n[managed.decline]  # keep our own bin/shipit\nkeep = ["bin/shipit"]\n'
    )
    config.write_manifest(p, version="new", managed={"lefthook.yml": "sha256:new"})
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit",)
    assert config.load_managed(cfg) == {"lefthook.yml": "sha256:new"}
    # The header's own trailing comment survives verbatim.
    assert "# keep our own bin/shipit" in p.read_text()


def test_shipit_pin_reads_the_stamped_version(tmp_path):
    # The [shipit].version pin `shipit install` stamps IS the bootstrapped marker
    # (ADR-0033) — the pin gate Tree provisioning fails closed on reads it here.
    p = tmp_path / ".shipit.toml"
    config.write_manifest(p, version="a" * 40, managed={"bin/shipit": "sha256:x"})
    assert config.shipit_pin(p) == "a" * 40


def test_shipit_pin_none_for_policy_only_config(tmp_path):
    # Policy config ([secrets]/[reviewers]/[project]) but no pin — PINLESS, so
    # Tree provisioning must refuse toward the bootstrap install.
    p = tmp_path / ".shipit.toml"
    p.write_text('[secrets]\nGH_PAT = { env = "X" }\n\n[reviewers]\ncopilot = {}\n')
    assert config.shipit_pin(p) is None

    # A bare [managed] table without a [shipit].version is pinless too: the
    # launcher would have no build to exec.
    q = tmp_path / "managed-only.toml"
    q.write_text("[managed]\n")
    assert config.shipit_pin(q) is None


def test_shipit_pin_none_when_file_missing_or_malformed(tmp_path):
    assert config.shipit_pin(tmp_path / "nope.toml") is None
    p = tmp_path / "broken.toml"
    p.write_text("[shipit\nversion=")
    assert config.shipit_pin(p) is None
    q = tmp_path / "mistyped.toml"
    q.write_text('shipit = "not a table"\n')
    assert config.shipit_pin(q) is None


@pytest.mark.parametrize(
    "version",
    [
        "0.0.1",  # the retired static package version — identifies nothing
        "seed",  # a sentinel that is not a commit
        "a" * 39,  # abbreviated / wrong-length hex
        "a" * 41,
        "z" * 40,  # right length, non-hex
        "",
    ],
)
def test_shipit_pin_none_for_non_sha_version(tmp_path, version):
    # The pin gate must fail CLOSED on any [shipit].version that is not a full
    # git sha (ADR-0033): a bogus pin left provisioning proceed and the launcher
    # hand uv a non-commit ref instead of refusing toward the bootstrap.
    p = tmp_path / ".shipit.toml"
    p.write_text(f'[shipit]\nversion = "{version}"\n')
    assert config.shipit_pin(p) is None


def test_shipit_pin_accepts_full_sha256(tmp_path):
    # A 64-hex SHA-256 object id is a valid full sha too.
    p = tmp_path / ".shipit.toml"
    p.write_text(f'[shipit]\nversion = "{"b" * 64}"\n')
    assert config.shipit_pin(p) == "b" * 64


# --------------------------------------------------------------------------
# Seed-if-absent consumer policy ([secrets] App mappings + [reviewers] set)
# --------------------------------------------------------------------------


def test_seeded_secrets_derivation_is_golden():
    """#313: the App-secret seed names and the ``[secrets]`` scaffold DERIVE from
    the Backend registry (``funnel_backends()``) — no hand-written key literal in
    config. This pins the rendered output for the CURRENT registry byte-identical
    to the former literals (the scaffold seeds real Doppler-backed projects), so
    the derivation is provably a refactor."""
    assert config.seeded_app_secrets() == (
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    )
    assert config.secrets_scaffold() == (
        "# [secrets] — repo Actions secrets. Each table key is the GitHub secret NAME; the\n"
        '# value names exactly one source ({ doppler = "KEY" } / { env = "VAR" } /\n'
        "# { prompt = true }). Seeded with shipit's local-reviewer (codex/agy) GitHub App\n"
        "# credentials, each sourced from Doppler github/prd. `shipit gh-setup` pushes an\n"
        "# App credential only when its reviewer is declared in [reviewers]; an undeclared\n"
        "# pair is flagged as an orphan (not pushed), so seeding is safe before opt-in.\n"
        "[secrets]\n"
        'CODEX_REVIEW_APP_PRIVATE_KEY = { doppler = "CODEX_REVIEW_APP_PRIVATE_KEY" }\n'
        'CODEX_REVIEW_APP_ID          = { doppler = "CODEX_REVIEW_APP_ID" }\n'
        'AGY_REVIEW_APP_PRIVATE_KEY   = { doppler = "AGY_REVIEW_APP_PRIVATE_KEY" }\n'
        'AGY_REVIEW_APP_ID            = { doppler = "AGY_REVIEW_APP_ID" }\n'
    )


def test_secrets_scaffold_with_no_funnel_backends_is_header_only(monkeypatch):
    """An empty Backend registry (no funnel backends) renders a header-only
    ``[secrets]`` table instead of raising (``max()`` over the empty name set)."""
    from shipit.agent import backend

    monkeypatch.setattr(backend, "REGISTRY", ())
    assert config.seeded_app_secrets() == ()
    scaffold = config.secrets_scaffold()
    assert scaffold.endswith("[secrets]\n")
    assert tomllib.loads(scaffold) == {"secrets": {}}


def test_plan_policy_seed_fresh_lists_secrets_and_reviewers(tmp_path):
    p = tmp_path / ".shipit.toml"  # absent
    seeded = config.plan_policy_seed(p)
    assert "[reviewers]" in seeded
    for name in config.seeded_app_secrets():
        assert f"[secrets].{name}" in seeded
    # Pure: planning twice gives the same answer and writes nothing.
    assert config.plan_policy_seed(p) == seeded
    assert not p.exists()


def test_apply_policy_seed_is_idempotent(tmp_path):
    p = tmp_path / ".shipit.toml"
    first = config.apply_policy_seed(p)
    assert first  # something was seeded
    # The seeded file is valid and carries both tables.
    cfg = config.load(p)
    assert {s.name for s in config.load_secrets(cfg)} == set(
        config.seeded_app_secrets()
    )
    assert "reviewers" in cfg

    again = config.apply_policy_seed(p)
    assert again == []  # nothing left to seed
    assert config.plan_policy_seed(p) == []


def test_apply_policy_seed_merges_into_existing_secrets(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[secrets]\nMY = { env = "MY" }\nCODEX_REVIEW_APP_ID = { doppler = "CUSTOM" }\n'
    )
    seeded = config.apply_policy_seed(p)
    # The already-present App secret is NOT re-seeded; the rest are.
    assert "[secrets].CODEX_REVIEW_APP_ID" not in seeded

    secrets = {s.name: s for s in config.load_secrets(config.load(p))}
    assert secrets["MY"].kind == "env"  # consumer entry preserved
    assert secrets["CODEX_REVIEW_APP_ID"].key == "CUSTOM"  # not clobbered
    assert {
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    } <= set(secrets)


def test_apply_policy_seed_preserves_existing_reviewers(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text("[reviewers]\ncodex = {}\n")
    seeded = config.apply_policy_seed(p)
    # [reviewers] present → not reseeded; only the missing secrets are added.
    assert "[reviewers]" not in seeded
    assert config.load(p)["reviewers"] == {"codex": {}}


def test_apply_policy_seed_preserves_consumer_lint_section(tmp_path):
    # The other .shipit.toml writer install runs on a reconcile (seed-if-absent
    # policy) must also leave the consumer [lint] seam untouched (#484): it only
    # appends/merges its own tables, never rewrites the consumer's.
    p = tmp_path / ".shipit.toml"
    p.write_text('[lint]\nignore = ["tests/fixtures/**"]\n')
    seeded = config.apply_policy_seed(p)
    # A tracked [lint] table is never re-seeded and never clobbered.
    assert "[lint].ignore" not in seeded
    assert config.load_lint_ignore(config.load(p)) == ["tests/fixtures/**"]


def test_plan_policy_seed_fresh_seeds_lint_ignore(tmp_path):
    # A virgin repo's plan seeds [lint].ignore alongside the other policy — the
    # onboarding-snag fix (#484): a freshly-onboarded repo would otherwise take a
    # latent lint-gate failure on its generated CHANGELOG / lockfiles.
    p = tmp_path / ".shipit.toml"  # absent
    seeded = config.plan_policy_seed(p)
    assert "[lint].ignore" in seeded
    # Pure: planning twice is stable and writes nothing.
    assert config.plan_policy_seed(p) == seeded
    assert not p.exists()


def test_apply_policy_seed_seeds_exact_lint_globs(tmp_path):
    # Applying the seed on a virgin repo lands EXACTLY the four generated-path
    # globs, and the seeded table parses back through the gate's own reader (#484).
    p = tmp_path / ".shipit.toml"  # absent
    seeded = config.apply_policy_seed(p)
    assert "[lint].ignore" in seeded
    assert config.load_lint_ignore(config.load(p)) == [
        "CHANGELOG.md",
        "CHANGELOG/**",
        "package-lock.json",
        "pnpm-lock.yaml",
    ]


def test_apply_policy_seed_lint_is_idempotent(tmp_path):
    # A re-install NOOPs the [lint] seed: no clobber, no duplicate — the table is
    # tracked after the first apply, so the second seeds nothing for it.
    p = tmp_path / ".shipit.toml"  # absent
    config.apply_policy_seed(p)
    before = p.read_text(encoding="utf-8")
    again = config.apply_policy_seed(p)
    assert "[lint].ignore" not in again
    assert p.read_text(encoding="utf-8") == before  # byte-identical re-run
    # Exactly one [lint] table survives — no duplicate appended.
    assert before.count("\n[lint]\n") == 1


def test_seeded_reviewers_resolve_to_required_set(tmp_path):
    from shipit.prstate import reviewers_config as rcfg

    p = tmp_path / ".shipit.toml"
    config.apply_policy_seed(p)
    roster = rcfg.load_roster(str(tmp_path))
    # The install scaffold is rendered from the SINGLE required-reviewer default
    # (ADR-0025 / COR01-WS02), so a freshly-seeded repo requires exactly what the
    # code-default requires — Copilot only. codex/agy are opt-in per repo (their
    # review Apps are not installed everywhere), never seeded by default.
    assert roster.required_names == tuple(rcfg.DEFAULT_REVIEWERS)
    assert roster.required_names == ("copilot",)


def test_plan_policy_seed_raises_on_malformed(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text("this is = not valid = toml\n")
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p)


def test_apply_policy_seed_merges_under_header_with_comment(tmp_path):
    # A normally-formatted header that carries a trailing comment (and a spaced
    # variant) must still be found and merged under — not appended at the root.
    p = tmp_path / ".shipit.toml"
    p.write_text('[ secrets ]  # my repo secrets\nMY = { env = "MY" }\n')
    config.apply_policy_seed(p)
    secrets = {s.name: s for s in config.load_secrets(config.load(p))}
    assert secrets["MY"].kind == "env"  # preserved
    assert set(config.seeded_app_secrets()) <= set(secrets)  # merged in, parses


@pytest.mark.parametrize(
    "body",
    [
        'secrets = "disabled"\n',  # scalar where a table is expected
        "reviewers = 42\n",  # scalar reviewers
    ],
)
def test_seed_refuses_scalar_policy_value(tmp_path, body):
    # A scalar `secrets`/`reviewers` can't be merged or re-headed without
    # redefining the key into invalid TOML — refuse, don't corrupt.
    p = tmp_path / ".shipit.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p)
    with pytest.raises(config.ConfigError):
        config.apply_policy_seed(p)
    assert p.read_text() == body  # untouched


@pytest.mark.parametrize(
    "body",
    [
        'secrets = { CODEX_REVIEW_APP_ID = { doppler = "X" } }\n',  # inline table
        'secrets.CODEX_REVIEW_APP_ID = { doppler = "X" }\n',  # dotted keys
    ],
)
def test_seed_refuses_secrets_without_literal_header(tmp_path, body):
    # `secrets` IS a table here, but there is no `[secrets]` header to merge the
    # missing App mappings under — refuse rather than append them at the root.
    p = tmp_path / ".shipit.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p)
    assert p.read_text() == body  # untouched


# --------------------------------------------------------------------------
# The [toolchains] seed (TOL01-WS08 #578) — manifest-derived, seed-when-absent
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("manifest", "toolchain"),
    [
        ("Cargo.toml", "rust"),
        ("go.mod", "go"),
        ("pyproject.toml", "python"),
        ("package.json", "npm"),
    ],
)
def test_derive_toolchains_maps_each_root_manifest(tmp_path, manifest, toolchain):
    (tmp_path / manifest).write_text("x = 1\n")
    assert config.derive_toolchains(tmp_path) == ((".", toolchain),)


def test_derive_toolchains_empty_when_no_manifest_signals(tmp_path):
    # No recognized root manifest → nothing to seed; the Tool verbs keep their
    # pointed missing-map refusal (ADR-0007 — never a dispatch fallback).
    assert config.derive_toolchains(tmp_path) == ()


def test_derive_toolchains_first_signal_wins_for_the_root(tmp_path):
    # "." maps to ONE toolchain, so precedence is SIGNAL_MANIFESTS order —
    # the same order the verbs' missing-map error picks its example from.
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "package.json").write_text("{}\n")
    assert config.derive_toolchains(tmp_path) == ((".", "rust"),)


def test_signal_toolchains_are_registry_names():
    # Every toolchain SIGNAL_MANIFESTS can seed must be a name the closed
    # registry knows — a seeded map must parse through load_toolchains, never
    # plant a config the Tool verbs immediately refuse.
    from shipit.tools import registry

    assert {tc for _, tc in config.SIGNAL_MANIFESTS} <= set(registry.names())


def test_plan_policy_seed_with_toolchains_lists_the_map(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.plan_policy_seed(p, toolchains=((".", "python"),))
    assert "[toolchains]" in seeded
    assert not p.exists()  # plan is pure


def test_apply_policy_seed_seeds_a_parseable_toolchains_map(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.apply_policy_seed(p, toolchains=((".", "python"),))
    assert "[toolchains]" in seeded
    entries = config.load_toolchains(config.load(p))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "python")]
    # Idempotent: the map is in place, so a re-run seeds nothing more.
    assert config.plan_policy_seed(p, toolchains=((".", "python"),)) == []


def test_seed_without_toolchain_entries_seeds_no_map(tmp_path):
    # The default (no derived entries) — the pre-#578 behavior, byte-for-byte:
    # no [toolchains] table appears.
    p = tmp_path / ".shipit.toml"
    config.apply_policy_seed(p)
    assert "toolchains" not in tomllib.loads(p.read_text())


def test_apply_policy_seed_never_clobbers_a_consumer_toolchains_map(tmp_path):
    # Seed-when-absent (the [lint] precedent): a consumer-edited map — even one
    # disagreeing with what the manifests would derive — is never overwritten.
    p = tmp_path / ".shipit.toml"
    p.write_text('[toolchains]\n"." = "go"\n')
    seeded = config.apply_policy_seed(p, toolchains=((".", "rust"),))
    assert "[toolchains]" not in seeded
    entries = config.load_toolchains(config.load(p))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "go")]


def test_apply_policy_seed_respects_an_empty_toolchains_table(tmp_path):
    # An EMPTY [toolchains] table is still a consumer edit (an explicit
    # declaration), not an absence — nothing is merged into it.
    p = tmp_path / ".shipit.toml"
    p.write_text("[toolchains]\n")
    config.apply_policy_seed(p, toolchains=((".", "rust"),))
    assert tomllib.loads(p.read_text())["toolchains"] == {}


def test_seed_refuses_scalar_toolchains(tmp_path):
    # A scalar `toolchains` can't be re-headed without redefining the key into
    # invalid TOML — refuse, don't corrupt (the secrets/reviewers stance).
    p = tmp_path / ".shipit.toml"
    p.write_text('toolchains = "rust"\n')
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p, toolchains=((".", "rust"),))
    with pytest.raises(config.ConfigError):
        config.apply_policy_seed(p, toolchains=((".", "rust"),))
    assert p.read_text() == 'toolchains = "rust"\n'  # untouched
