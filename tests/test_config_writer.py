"""Unit tests for the .shipit.toml manifest writer + content hashing."""

import tomllib

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
        "skills/shipt-to-prd/SKILL.md": "sha256:aaa",
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
        '[secrets]\n'
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
