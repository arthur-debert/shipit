import tomllib

import pytest

from shipit import config


def test_content_hash_is_sha256_prefixed():
    h = config.content_hash(b"hello")
    assert h.startswith("sha256:")
    assert h == config.content_hash(b"hello")
    assert h != config.content_hash(b"world")


def test_write_manifest_fresh_file_roundtrips(tmp_path):
    p = tmp_path / ".shipit.toml"
    managed = {
        ".agents/skills/to-spec/SKILL.md": "sha256:aaa",
        "AGENTS.md#shipit-block": "sha256:bbb",
        "bin/shipit": "sha256:ccc",
    }
    config.write_manifest(p, version="deadbeef", managed=managed)

    cfg = config.load(p)
    assert config.shipit_version(cfg) == "deadbeef"
    assert config.load_managed(cfg) == managed
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
    secrets = config.load_secrets(cfg)
    names = {s.name for s in secrets}
    assert names == {"CARGO_REGISTRY_TOKEN", "GH_PAT"}
    assert config.shipit_version(cfg) == "v1"
    assert config.load_managed(cfg) == {"bin/shipit": "sha256:x"}


def test_write_manifest_preserves_consumer_lint_section(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        "[lint]\n"
        'ignore = ["crates/lex-babel/tests/fixtures/**", "CHANGELOG.md"]\n'
        '\n[shipit]\nversion = "old"\n'
        '\n[managed]\n"bin/shipit" = "sha256:old"\n'
    )
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
    assert config.load_managed(cfg) == {"a": "sha256:9"}
    assert p.read_text().count("[shipit]") == 1


def test_write_manifest_strips_a_commented_managed_header(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[shipit]  # stamped by install\nversion = "old"\n'
        '\n[managed]  # pristine map\n"bin/shipit" = "sha256:old"\n'
    )
    config.write_manifest(p, version="new", managed={"bin/shipit": "sha256:new"})
    cfg = config.load(p)
    assert config.shipit_version(cfg) == "new"
    assert config.load_managed(cfg) == {"bin/shipit": "sha256:new"}
    assert p.read_text().count("[managed]") == 1


def test_load_declines_parses_the_keep_list(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[managed.decline]\nkeep = ["bin/shipit", "lefthook.yml"]\n')
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit", "lefthook.yml")
    assert config.load_managed(cfg) == {}


def test_load_declines_defaults_empty():
    assert config.load_declines({}, "") == ()
    assert config.load_declines({"managed": {"bin/shipit": "sha256:x"}}, "") == ()


def test_load_declines_accepts_a_header_with_a_trailing_comment(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[managed.decline]  # keep our own bin/shipit\nkeep = ["bin/shipit"]\n'
    )
    assert config.load_declines(config.load(p), p.read_text()) == ("bin/shipit",)


def test_load_declines_rejects_the_dotted_form(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[managed]\ndecline.keep = ["bin/shipit"]\n')
    with pytest.raises(config.ConfigError, match="own header") as excinfo:
        config.load_declines(config.load(p), p.read_text())
    collapsed = " ".join(str(excinfo.value).split())
    assert "[managed.decline] keep" not in collapsed
    assert "TWO lines" in collapsed


@pytest.mark.parametrize(
    "body",
    [
        "[managed]\ndecline = 42\n",
        '[managed.decline]\nkeep = "bin/shipit"\n',
        "[managed.decline]\nkeep = [1]\n",
        '[managed.decline]\nkeep = [""]\n',
        '[managed.decline]\nkept = ["bin/shipit"]\n',
    ],
)
def test_load_declines_rejects_malformed_shapes(tmp_path, body):
    p = tmp_path / ".shipit.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError):
        config.load_declines(config.load(p), p.read_text())


def test_write_manifest_preserves_managed_decline(tmp_path):
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
    assert config.load_managed(cfg) == {"lefthook.yml": "sha256:new"}


def test_write_manifest_preserves_a_trailing_managed_decline(tmp_path):
    p = tmp_path / ".shipit.toml"
    config.write_manifest(p, version="v1", managed={"bin/shipit": "sha256:x"})
    p.write_text(p.read_text() + '\n[managed.decline]\nkeep = ["bin/shipit"]\n')
    config.write_manifest(p, version="v2", managed={})
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit",)
    assert config.load_managed(cfg) == {}


def test_write_manifest_preserves_a_commented_managed_decline(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[managed]\n"bin/shipit" = "sha256:old"\n'
        '\n[managed.decline]  # keep our own bin/shipit\nkeep = ["bin/shipit"]\n'
    )
    config.write_manifest(p, version="new", managed={"lefthook.yml": "sha256:new"})
    cfg = config.load(p)
    assert config.load_declines(cfg, p.read_text()) == ("bin/shipit",)
    assert config.load_managed(cfg) == {"lefthook.yml": "sha256:new"}
    assert "# keep our own bin/shipit" in p.read_text()


def test_shipit_pin_reads_the_stamped_version(tmp_path):
    p = tmp_path / ".shipit.toml"
    config.write_manifest(p, version="a" * 40, managed={"bin/shipit": "sha256:x"})
    assert config.shipit_pin(p) == "a" * 40


def test_shipit_pin_none_for_policy_only_config(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[secrets]\nGH_PAT = { env = "X" }\n\n[reviewers]\ncopilot = {}\n')
    assert config.shipit_pin(p) is None

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
        "0.0.1",
        "seed",
        "a" * 39,
        "a" * 41,
        "z" * 40,
        "",
    ],
)
def test_shipit_pin_none_for_non_sha_version(tmp_path, version):
    p = tmp_path / ".shipit.toml"
    p.write_text(f'[shipit]\nversion = "{version}"\n')
    assert config.shipit_pin(p) is None


def test_shipit_pin_accepts_full_sha256(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(f'[shipit]\nversion = "{"b" * 64}"\n')
    assert config.shipit_pin(p) == "b" * 64


def test_seeded_secrets_derivation_is_golden():
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
    from shipit.agent import backend

    monkeypatch.setattr(backend, "REGISTRY", ())
    assert config.seeded_app_secrets() == ()
    scaffold = config.secrets_scaffold()
    assert scaffold.endswith("[secrets]\n")
    assert tomllib.loads(scaffold) == {"secrets": {}}


def test_plan_policy_seed_fresh_lists_secrets_and_reviewers(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.plan_policy_seed(p)
    assert "[reviewers]" in seeded
    for name in config.seeded_app_secrets():
        assert f"[secrets].{name}" in seeded
    assert config.plan_policy_seed(p) == seeded
    assert not p.exists()


def test_apply_policy_seed_is_idempotent(tmp_path):
    p = tmp_path / ".shipit.toml"
    first = config.apply_policy_seed(p)
    assert first
    cfg = config.load(p)
    assert {s.name for s in config.load_secrets(cfg)} == set(
        config.seeded_app_secrets()
    )
    assert "reviewers" in cfg

    again = config.apply_policy_seed(p)
    assert again == []
    assert config.plan_policy_seed(p) == []


def test_apply_policy_seed_merges_into_existing_secrets(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[secrets]\nMY = { env = "MY" }\nCODEX_REVIEW_APP_ID = { doppler = "CUSTOM" }\n'
    )
    seeded = config.apply_policy_seed(p)
    assert "[secrets].CODEX_REVIEW_APP_ID" not in seeded

    secrets = {s.name: s for s in config.load_secrets(config.load(p))}
    assert secrets["MY"].kind == "env"
    assert secrets["CODEX_REVIEW_APP_ID"].key == "CUSTOM"
    assert {
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    } <= set(secrets)


def test_apply_policy_seed_preserves_existing_reviewers(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text("[reviewers]\ncodex = {}\n")
    seeded = config.apply_policy_seed(p)
    assert "[reviewers]" not in seeded
    assert config.load(p)["reviewers"] == {"codex": {}}


def test_apply_policy_seed_preserves_consumer_lint_section(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[lint]\nignore = ["tests/fixtures/**"]\n')
    seeded = config.apply_policy_seed(p)
    assert "[lint].ignore" not in seeded
    assert config.load_lint_ignore(config.load(p)) == ["tests/fixtures/**"]


def test_plan_policy_seed_fresh_seeds_lint_ignore(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.plan_policy_seed(p)
    assert "[lint].ignore" in seeded
    assert config.plan_policy_seed(p) == seeded
    assert not p.exists()


def test_apply_policy_seed_seeds_exact_lint_globs(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.apply_policy_seed(p)
    assert "[lint].ignore" in seeded
    assert config.load_lint_ignore(config.load(p)) == [
        "CHANGELOG.md",
        "CHANGELOG/**",
        "package-lock.json",
        "pnpm-lock.yaml",
    ]


def test_apply_policy_seed_lint_is_idempotent(tmp_path):
    p = tmp_path / ".shipit.toml"
    config.apply_policy_seed(p)
    before = p.read_text(encoding="utf-8")
    again = config.apply_policy_seed(p)
    assert "[lint].ignore" not in again
    assert p.read_text(encoding="utf-8") == before
    assert before.count("\n[lint]\n") == 1


def test_seeded_reviewers_resolve_to_required_set(tmp_path):
    from shipit.prstate import reviewers_config as rcfg

    p = tmp_path / ".shipit.toml"
    config.apply_policy_seed(p)
    roster = rcfg.load_roster(str(tmp_path))
    assert roster.required_names == tuple(rcfg.DEFAULT_REVIEWERS)
    assert roster.required_names == ("copilot",)


def test_plan_policy_seed_raises_on_malformed(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text("this is = not valid = toml\n")
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p)


def test_apply_policy_seed_merges_under_header_with_comment(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[ secrets ]  # my repo secrets\nMY = { env = "MY" }\n')
    config.apply_policy_seed(p)
    secrets = {s.name: s for s in config.load_secrets(config.load(p))}
    assert secrets["MY"].kind == "env"
    assert set(config.seeded_app_secrets()) <= set(secrets)


@pytest.mark.parametrize(
    "body",
    [
        'secrets = "disabled"\n',
        "reviewers = 42\n",
    ],
)
def test_seed_refuses_scalar_policy_value(tmp_path, body):
    p = tmp_path / ".shipit.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p)
    with pytest.raises(config.ConfigError):
        config.apply_policy_seed(p)
    assert p.read_text() == body


@pytest.mark.parametrize(
    "body",
    [
        'secrets = { CODEX_REVIEW_APP_ID = { doppler = "X" } }\n',
        'secrets.CODEX_REVIEW_APP_ID = { doppler = "X" }\n',
    ],
)
def test_seed_refuses_secrets_without_literal_header(tmp_path, body):
    p = tmp_path / ".shipit.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p)
    assert p.read_text() == body


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
    assert config.derive_toolchains(tmp_path) == ()


def test_derive_toolchains_first_signal_wins_for_the_root(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "package.json").write_text("{}\n")
    assert config.derive_toolchains(tmp_path) == ((".", "rust"),)


def test_signal_toolchains_are_registry_names():
    from shipit.tools import registry

    assert {tc for _, tc in config.SIGNAL_MANIFESTS} <= set(registry.names())


def test_plan_policy_seed_with_toolchains_lists_the_map(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.plan_policy_seed(p, toolchains=((".", "python"),))
    assert "[toolchains]" in seeded
    assert not p.exists()


def test_apply_policy_seed_seeds_a_parseable_toolchains_map(tmp_path):
    p = tmp_path / ".shipit.toml"
    seeded = config.apply_policy_seed(p, toolchains=((".", "python"),))
    assert "[toolchains]" in seeded
    entries = config.load_toolchains(config.load(p))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "python")]
    assert config.plan_policy_seed(p, toolchains=((".", "python"),)) == []


def test_seed_without_toolchain_entries_seeds_no_map(tmp_path):
    p = tmp_path / ".shipit.toml"
    config.apply_policy_seed(p)
    assert "toolchains" not in tomllib.loads(p.read_text())


def test_apply_policy_seed_never_clobbers_a_consumer_toolchains_map(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[toolchains]\n"." = "go"\n')
    seeded = config.apply_policy_seed(p, toolchains=((".", "rust"),))
    assert "[toolchains]" not in seeded
    entries = config.load_toolchains(config.load(p))
    assert [(e.path, e.toolchain) for e in entries] == [(".", "go")]


def test_apply_policy_seed_respects_an_empty_toolchains_table(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text("[toolchains]\n")
    config.apply_policy_seed(p, toolchains=((".", "rust"),))
    assert tomllib.loads(p.read_text())["toolchains"] == {}


def test_seed_refuses_scalar_toolchains(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('toolchains = "rust"\n')
    with pytest.raises(config.ConfigError):
        config.plan_policy_seed(p, toolchains=((".", "rust"),))
    with pytest.raises(config.ConfigError):
        config.apply_policy_seed(p, toolchains=((".", "rust"),))
    assert p.read_text() == 'toolchains = "rust"\n'
