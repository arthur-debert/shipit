"""Unit tests for .shipit.toml parsing — the [secrets] map."""

import tomllib

import pytest

from shipit import config


def _secrets(toml: str) -> list[config.SecretSource]:
    return config.load_secrets(tomllib.loads(toml))


def test_load_secrets_each_source_kind():
    sources = _secrets(
        """
        [secrets]
        A = { doppler = "KEY_A" }
        B = { env = "VAR_B" }
        C = { prompt = true }
        D = { doppler = "KEY_D", optional = true }
        """
    )
    by_name = {s.name: s for s in sources}
    assert by_name["A"] == config.SecretSource("A", "doppler", "KEY_A", False)
    assert by_name["B"] == config.SecretSource("B", "env", "VAR_B", False)
    assert by_name["C"] == config.SecretSource("C", "prompt", None, False)
    assert by_name["D"].optional is True
    # Declaration order preserved.
    assert [s.name for s in sources] == ["A", "B", "C", "D"]


def test_missing_secrets_table_is_empty():
    assert config.load_secrets({}) == []


def test_two_sources_rejected():
    with pytest.raises(config.ConfigError, match="exactly one source"):
        _secrets('[secrets]\nA = { doppler = "K", env = "V" }\n')


def test_no_source_rejected():
    with pytest.raises(config.ConfigError, match="exactly one source"):
        _secrets("[secrets]\nA = { optional = true }\n")


def test_prompt_must_be_true():
    with pytest.raises(config.ConfigError, match="prompt must be"):
        _secrets("[secrets]\nA = { prompt = false }\n")


def test_non_table_entry_rejected():
    with pytest.raises(config.ConfigError, match="inline table"):
        _secrets('[secrets]\nA = "just-a-string"\n')


def test_empty_key_rejected():
    with pytest.raises(config.ConfigError, match="non-empty string"):
        _secrets('[secrets]\nA = { doppler = "" }\n')


def test_load_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="no .shipit.toml"):
        config.load(tmp_path / "nope.toml")


def test_load_malformed_toml_raises_config_error(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text("not = valid = toml\n")
    with pytest.raises(config.ConfigError, match="malformed"):
        config.load(p)


def test_load_non_utf8_raises_config_error(tmp_path):
    # tomllib decodes the file as UTF-8 before parsing, so a non-UTF-8 file
    # used to leak UnicodeDecodeError (a ValueError) past the documented
    # ConfigError contract — crashing callers that guard on ConfigError (#585).
    p = tmp_path / ".shipit.toml"
    p.write_bytes(b"\xff\xfe[secrets]\n")
    with pytest.raises(config.ConfigError, match="malformed"):
        config.load(p)


def test_load_roundtrip(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[secrets]\nA = { env = "X" }\n')
    cfg = config.load(p)
    assert config.load_secrets(cfg)[0].name == "A"


def test_unknown_top_level_table_rejected(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[secretz]\nA = { env = "X" }\n')
    with pytest.raises(config.ConfigError, match="unknown top-level table `secretz`"):
        config.load(p)


def test_known_tables_load(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[secrets]\nA = { env = "X" }\n'
        "[reviewers]\ncopilot = {}\n"
        '[shipit]\nversion = "abc"\n'
        '[managed]\n"path" = "sha256:deadbeef"\n'
    )
    cfg = config.load(p)
    assert config.load_secrets(cfg)[0].name == "A"


def test_project_freeform_subtree_not_validated(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        "[project.portfolio]\n"
        'exclude = ["a/b"]\n'
        "[[project.portfolio.repo]]\n"
        'name = "x/y"\n'
        'kind = "anything"\n'
        "[project.whatever.deeply.nested]\n"
        'made_up_key = "fine"\n'
    )
    cfg = config.load(p)
    assert cfg["project"]["portfolio"]["repo"][0]["name"] == "x/y"


def test_custom_escape_hatch_alias_allowed(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text('[custom.anything]\nmade_up = "fine"\n')
    cfg = config.load(p)
    assert cfg["custom"]["anything"]["made_up"] == "fine"


# --------------------------------------------------------------------------
# [lint] — the consumer-owned lint-ignore seam (#484)
# --------------------------------------------------------------------------


def test_lint_table_is_a_known_table(tmp_path):
    # [lint] is a first-class consumer table: it loads without tripping the
    # closed-registry validation.
    p = tmp_path / ".shipit.toml"
    p.write_text('[lint]\nignore = ["tests/fixtures/**"]\n')
    assert config.load_lint_ignore(config.load(p)) == ["tests/fixtures/**"]


def test_lint_ignore_absent_is_empty():
    assert config.load_lint_ignore({}) == []
    assert config.load_lint_ignore({"lint": {}}) == []


def test_lint_ignore_preserves_order():
    cfg = {"lint": {"ignore": ["b/**", "a.md", "CHANGELOG.md"]}}
    assert config.load_lint_ignore(cfg) == ["b/**", "a.md", "CHANGELOG.md"]


def test_lint_must_be_a_table():
    with pytest.raises(config.ConfigError, match=r"\[lint\] must be a table"):
        config.load_lint_ignore({"lint": "off"})


def test_lint_ignore_must_be_a_string_list():
    with pytest.raises(config.ConfigError, match="list of glob strings"):
        config.load_lint_ignore({"lint": {"ignore": "tests/**"}})
    with pytest.raises(config.ConfigError, match="list of glob strings"):
        config.load_lint_ignore({"lint": {"ignore": ["ok", 42]}})


# --------------------------------------------------------------------------
# [lanes] — declared CI test units (TOL01, PRD story 14; WS06 declares the
# fragment-sync check as the first one)
# --------------------------------------------------------------------------


def test_lanes_table_is_a_known_table(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        "[lanes.changelog-sync]\n"
        'run = "changelog check"\n'
        "required = true\n"
        'trigger = "pr"\n'
    )
    lanes = config.load_lanes(config.load(p))
    # The declared fragment-sync lane parses to the SAME typed Lane as the
    # shipped scaffold constant: one definition, laptop and CI identical.
    assert lanes == [config.CHANGELOG_SYNC_LANE]


def test_lanes_absent_is_empty():
    assert config.load_lanes({}) == []


def test_lanes_full_field_set_and_order():
    cfg = {
        "lanes": {
            "tests-npm": {
                "run": "test npm",
                "required": True,
                "local": True,
                "trigger": "push",
                "runner": "ubuntu-latest",
                "scope": "packages/npm",
            },
            "nightly-e2e": {"run": "e2e", "trigger": "nightly"},
        }
    }
    lanes = config.load_lanes(cfg)
    assert [lane.name for lane in lanes] == ["tests-npm", "nightly-e2e"]
    first = lanes[0]
    assert first == config.Lane(
        name="tests-npm",
        run="test npm",
        required=True,
        local=True,
        trigger="push",
        runner="ubuntu-latest",
        scope="packages/npm",
    )
    # Defaults: advisory, not local, planner-default routing (trigger given).
    assert lanes[1] == config.Lane(name="nightly-e2e", run="e2e", trigger="nightly")
    assert lanes[1].required is False and lanes[1].local is False
    # And an entry with only `run` is PR-triggered by default.
    assert config.load_lanes({"lanes": {"y": {"run": "lint"}}})[0].trigger == "pr"


def test_lanes_run_is_required():
    with pytest.raises(config.ConfigError, match=r"\[lanes\].x: `run` must be"):
        config.load_lanes({"lanes": {"x": {"required": True}}})
    with pytest.raises(config.ConfigError, match=r"\[lanes\].x: `run` must be"):
        config.load_lanes({"lanes": {"x": {"run": "  "}}})


@pytest.mark.parametrize("key", ["runner", "scope"])
def test_lanes_blank_runner_or_scope_dies_at_parse(key):
    # A present-but-blank routing hint is a footgun, not a default: a blank
    # runner is an invalid `runs-on`, a blank scope drops the lane every PR.
    with pytest.raises(config.ConfigError, match=rf"`{key}` must be a non-empty"):
        config.load_lanes({"lanes": {"x": {"run": "test", key: "   "}}})
    # Absent stays the planner default (None), never rejected.
    lane = config.load_lanes({"lanes": {"x": {"run": "test"}}})[0]
    assert getattr(lane, key) is None
    # A real value is stripped, mirroring `run`.
    stripped = config.load_lanes({"lanes": {"x": {"run": "test", key: " a "}}})[0]
    assert getattr(stripped, key) == "a"


def test_lanes_unknown_key_dies_fast():
    with pytest.raises(config.ConfigError, match=r"unknown key\(s\) runs_on"):
        config.load_lanes({"lanes": {"x": {"run": "lint", "runs_on": "mac"}}})


def test_lanes_trigger_vocabulary_is_closed():
    with pytest.raises(config.ConfigError, match="`trigger` must be one of"):
        config.load_lanes({"lanes": {"x": {"run": "lint", "trigger": "PR"}}})


def test_lanes_trigger_non_string_is_a_configerror_not_a_typeerror():
    # TOML parses `trigger = ["pr"]` into a list; the unhashable value must be
    # rejected as ConfigError (not crash the membership test with a TypeError).
    with pytest.raises(config.ConfigError, match="`trigger` must be one of"):
        config.load_lanes({"lanes": {"x": {"run": "lint", "trigger": ["pr"]}}})


def test_lanes_entry_must_be_a_table():
    with pytest.raises(config.ConfigError, match=r"\[lanes\].x must be a table"):
        config.load_lanes({"lanes": {"x": "changelog check"}})
    with pytest.raises(config.ConfigError, match=r"\[lanes\] must be a table"):
        config.load_lanes({"lanes": "off"})


# --------------------------------------------------------------------------
# [lanes].<lane>.secrets — the declared-secrets allowlist (#778)
# --------------------------------------------------------------------------


def test_lane_secrets_absent_defaults_to_empty_tuple():
    # No allowlist = the lane is handed no secret (the default, least privilege).
    lane = config.load_lanes({"lanes": {"x": {"run": "test"}}})[0]
    assert lane.secrets == ()


def test_lane_secrets_allowlist_parses_to_an_ordered_tuple_of_names():
    lanes = config.load_lanes(
        {"lanes": {"wasm": {"run": "test wasm", "secrets": ["lane_token"]}}}
    )
    assert lanes[0].secrets == ("lane_token",)


def test_lane_secrets_must_be_a_list_not_a_bare_string():
    # `secrets = "lane_token"` is the `secrets: inherit` shape this seam refuses
    # — a scalar is rejected so the allowlist is always an explicit named set.
    with pytest.raises(config.ConfigError, match=r"`secrets` must be a list"):
        config.load_lanes({"lanes": {"x": {"run": "test", "secrets": "lane_token"}}})


@pytest.mark.parametrize(
    "bad",
    ["9lives", "has-dash", "has space", "GITHUB_TOKEN", "", 42],
)
def test_lane_secrets_rejects_names_github_forbids(bad):
    # Leading digit, dash/space, the reserved `GITHUB_` prefix, empty, and a
    # non-string all die at parse — an unroutable name must never reach CI as a
    # silently-dropped credential.
    with pytest.raises(config.ConfigError, match=r"not a valid GitHub secret name"):
        config.load_lanes({"lanes": {"x": {"run": "test", "secrets": [bad]}}})


def test_lane_secrets_is_a_known_key_full_field_set(tmp_path):
    p = tmp_path / ".shipit.toml"
    p.write_text(
        '[lanes.wasm]\nrun = "test wasm"\nrequired = true\nsecrets = ["lane_token"]\n'
    )
    lane = config.load_lanes(config.load(p))[0]
    assert lane == config.Lane(
        name="wasm", run="test wasm", required=True, secrets=("lane_token",)
    )


def test_lanes_bool_and_string_field_types():
    with pytest.raises(config.ConfigError, match="must be booleans"):
        config.load_lanes({"lanes": {"x": {"run": "lint", "required": "yes"}}})
    with pytest.raises(config.ConfigError, match="`runner` must be a non-empty string"):
        config.load_lanes({"lanes": {"x": {"run": "lint", "runner": 3}}})


# --------------------------------------------------------------------------
# The fleet manifest (#449 item 3, ADR-0033): [project.portfolio] carries the
# adoption targets of the ADP fleet sweep (#426) — including shipit-canary,
# the standing test bed — and none of the sweep's non-targets.
# --------------------------------------------------------------------------


def _portfolio_repos() -> set[str]:
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / ".shipit.toml").read_text())
    portfolio = cfg["project"]["portfolio"]
    return {entry["repo"] for stack in portfolio.values() for entry in stack}


def test_portfolio_contains_the_adoption_targets():
    repos = _portfolio_repos()
    # The ADP00 canary and the tool itself are fleet rows (#426).
    assert "arthur-debert/shipit-canary" in repos
    assert "arthur-debert/shipit" in repos
    # The WS07 sweep's other previously-missing targets.
    for slug in (
        "arthur-debert/supage",
        "lex-fmt/mkdocs-lex",
        "phos-editor/phos.photo",
    ):
        assert slug in repos, slug


def test_portfolio_excludes_the_sweeps_non_targets():
    repos = _portfolio_repos()
    # n/a rows in the sweep: docs, editor config, grammar packaging.
    for slug in ("lex-fmt/comms", "lex-fmt/nvim", "lex-fmt/zed-lex"):
        assert slug not in repos, slug


def test_portfolio_excludes_the_dropped_non_targets():
    # Deliberately dropped from the rollout manifest as non-adoption targets
    # (fix(portfolio): drop 7 non-target repos). This guard is the regression
    # cover codex asked for: it documents the intentional removal AND fails loud
    # if any of these silently reappears in the portfolio.
    repos = _portfolio_repos()
    for slug in (
        "arthur-debert/dotcat",
        "arthur-debert/falala",
        "arthur-debert/electron-splashguard",
        "arthur-debert/nanodoc",
        "arthur-debert/shellai",
        "arthur-debert/sprinkles",
        "arthur-debert/visual-explore",
    ):
        assert slug not in repos, slug


def test_portfolio_entries_carry_repo_and_path():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / ".shipit.toml").read_text())
    for stack, entries in cfg["project"]["portfolio"].items():
        assert isinstance(entries, list) and entries, stack
        for entry in entries:
            assert "repo" in entry and "/" in entry["repo"], (stack, entry)
            assert "path" in entry and entry["path"], (stack, entry)


# --------------------------------------------------------------------------
# [toolchains] — the path→toolchain map (TOL01-WS01, ADR-0007/0039)
# --------------------------------------------------------------------------


def _toolchains(toml: str) -> tuple[config.ToolchainEntry, ...]:
    return config.load_toolchains(tomllib.loads(toml))


def test_load_toolchains_bare_names_in_declaration_order():
    entries = _toolchains(
        """
        [toolchains]
        "web" = "npm"
        "."   = "rust"
        """
    )
    # Declaration order IS the fan-out order (ADR-0039) — never re-sorted.
    assert [(e.path, e.toolchain) for e in entries] == [("web", "npm"), (".", "rust")]
    assert all(e.commands == {} for e in entries)


def test_load_toolchains_table_entry_with_per_path_test_override():
    entries = _toolchains(
        """
        [toolchains]
        "crates/cli" = { toolchain = "rust", test = ["cargo", "test", "--workspace"] }
        """
    )
    assert entries[0].toolchain == "rust"
    assert entries[0].commands == {"test": ("cargo", "test", "--workspace")}


def test_toolchain_entry_commands_are_read_only():
    # The "typed frozen values" contract (ADR-0030): a parsed entry's override
    # map cannot be mutated after the fact — frozen=True freezes the binding,
    # and the map itself is wrapped read-only.
    entry = config.ToolchainEntry(path=".", toolchain="python", commands={})
    with pytest.raises(TypeError):
        entry.commands["test"] = ("pytest",)  # type: ignore[index]


def test_load_toolchains_absent_table_is_empty():
    assert config.load_toolchains({}) == ()


def test_load_toolchains_unknown_toolchain_names_the_registry():
    with pytest.raises(config.ConfigError, match="known toolchains: rust, go"):
        _toolchains('[toolchains]\n"." = "tauri"\n')


def test_load_toolchains_unknown_tool_slot_rejected():
    # The override keys are the CLOSED tool-slot vocabulary (test; WS02 adds
    # build) — a typo dies fast, mirroring the known-tables validation.
    with pytest.raises(config.ConfigError, match="unknown tool slot `tets`"):
        _toolchains(
            '[toolchains]\n"." = { toolchain = "rust", tets = ["cargo", "test"] }\n'
        )


def test_load_toolchains_override_must_be_a_non_empty_argv_list():
    # An argv list, never a shell string (ADR-0028: no shell=True anywhere).
    with pytest.raises(config.ConfigError, match="argv list"):
        _toolchains('[toolchains]\n"." = { toolchain = "rust", test = "cargo test" }\n')
    with pytest.raises(config.ConfigError, match="argv list"):
        _toolchains('[toolchains]\n"." = { toolchain = "rust", test = [] }\n')


def test_load_toolchains_entry_must_name_its_toolchain():
    with pytest.raises(config.ConfigError, match="must name its toolchain"):
        _toolchains('[toolchains]\n"." = { test = ["cargo", "test"] }\n')


def test_load_toolchains_rejects_absolute_paths():
    with pytest.raises(config.ConfigError, match="repo-relative"):
        _toolchains('[toolchains]\n"/abs" = "rust"\n')


def test_load_toolchains_rejects_paths_escaping_the_checkout():
    # A leg path is an adapter's cwd; a `..` segment would run the bump outside
    # the tree, so it is refused alongside absolute paths.
    with pytest.raises(config.ConfigError, match="repo-relative"):
        _toolchains('[toolchains]\n"../evil" = "rust"\n')


def test_load_toolchains_normalizes_paths_to_canonical_form():
    # `./web` and `web/` must be stored as `web` so the leg's pathspecs match
    # `git status --porcelain` output and never trip a false no-op bump.
    (entry,) = _toolchains('[toolchains]\n"./web" = "npm"\n')
    assert entry.path == "web"
    (root_entry,) = _toolchains('[toolchains]\n"." = "python"\n')
    assert root_entry.path == "."


def test_load_toolchains_non_table_section_rejected():
    with pytest.raises(config.ConfigError, match=r"\[toolchains\] must be a table"):
        config.load_toolchains({"toolchains": "rust"})


def test_toolchains_is_a_known_top_level_table():
    # `load` validates top-level tables against the closed registry; the map
    # must parse, not die as a typo.
    cfg = tomllib.loads('[toolchains]\n"." = "python"\n')
    config._validate_known_tables(cfg)  # does not raise
