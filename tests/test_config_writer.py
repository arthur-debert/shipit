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
        "skills/shipit-to-prd/SKILL.md": "sha256:aaa",
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


def test_is_onboarded_true_when_shipit_or_managed_block_present(tmp_path):
    # The [shipit]/[managed] block `shipit install` writes IS the onboarded marker.
    p = tmp_path / ".shipit.toml"
    config.write_manifest(p, version="v1", managed={"bin/shipit": "sha256:x"})
    assert config.is_onboarded(p) is True

    # A bare [managed] table (no [shipit]) is also the marker.
    q = tmp_path / "managed-only.toml"
    q.write_text("[managed]\n")
    assert config.is_onboarded(q) is True


def test_is_onboarded_false_for_policy_only_config(tmp_path):
    # shipit-self's case: policy config ([secrets]/[reviewers]/[project]) but no
    # managed block — NOT onboarded, so Tree provisioning must not reconcile/onboard.
    p = tmp_path / ".shipit.toml"
    p.write_text('[secrets]\nGH_PAT = { env = "X" }\n\n[reviewers]\ncopilot = {}\n')
    assert config.is_onboarded(p) is False


def test_is_onboarded_false_when_file_missing(tmp_path):
    assert config.is_onboarded(tmp_path / "nope.toml") is False


# --------------------------------------------------------------------------
# Seed-if-absent consumer policy ([secrets] App mappings + [reviewers] set)
# --------------------------------------------------------------------------


def test_plan_policy_seed_fresh_lists_secrets_and_reviewers(tmp_path):
    p = tmp_path / ".shipit.toml"  # absent
    seeded = config.plan_policy_seed(p)
    assert "[reviewers]" in seeded
    for name in config.SEEDED_APP_SECRETS:
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
    assert {s.name for s in config.load_secrets(cfg)} == set(config.SEEDED_APP_SECRETS)
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


def test_seeded_reviewers_resolve_to_required_set(tmp_path):
    from shipit.prstate import reviewers_config as rcfg

    p = tmp_path / ".shipit.toml"
    config.apply_policy_seed(p)
    override = rcfg.load_override(str(tmp_path))
    assert rcfg.resolve_required_names(override) == ("copilot", "codex", "agy")


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
    assert set(config.SEEDED_APP_SECRETS) <= set(secrets)  # merged in, parses


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
