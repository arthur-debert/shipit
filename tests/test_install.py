"""Unit tests for install — reconciliation decisions, block splicing, and the verb."""

import json

import pytest

from shipit import config, gh
from shipit.verbs import install


# --------------------------------------------------------------------------
# Pure reconciliation
# --------------------------------------------------------------------------


def test_decide_covers_four_cases():
    # absent -> ADD
    assert (
        install.decide(consumer_hash=None, pristine_hash=None, desired_hash="d")
        == install.ADD
    )
    # already current -> NOOP
    assert (
        install.decide(consumer_hash="d", pristine_hash="p", desired_hash="d")
        == install.NOOP
    )
    # untouched since last install -> UPDATE
    assert (
        install.decide(consumer_hash="p", pristine_hash="p", desired_hash="d")
        == install.UPDATE
    )
    # consumer-edited -> OVERRIDE
    assert (
        install.decide(consumer_hash="x", pristine_hash="p", desired_hash="d")
        == install.OVERRIDE
    )
    # present but never installed by shipit (no pristine) and divergent -> OVERRIDE
    assert (
        install.decide(consumer_hash="x", pristine_hash=None, desired_hash="d")
        == install.OVERRIDE
    )


def test_block_extract_and_splice_roundtrip():
    base = "# Consumer AGENTS\n\nSome consumer-owned text.\n"
    spliced = install.splice_block(base, "managed body")
    assert install.BLOCK_OPEN in spliced and install.BLOCK_CLOSE in spliced
    # The consumer's own text is preserved.
    assert "Some consumer-owned text." in spliced
    assert install.extract_block(spliced) == "managed body"
    # Re-splicing replaces only the block, leaving one block.
    again = install.splice_block(spliced, "new body")
    assert install.extract_block(again) == "new body"
    assert again.count(install.BLOCK_OPEN) == 1
    assert "Some consumer-owned text." in again


def test_extract_block_absent_is_none():
    assert install.extract_block("no markers here") is None


# --------------------------------------------------------------------------
# The lint-check units (Step 3) — lefthook caller + pixi [tasks] block
# --------------------------------------------------------------------------


def test_load_units_includes_lefthook_and_pixi_task_block():
    units = {u.key: u for u in install.load_units()}
    assert install.LEFTHOOK_FILE in units
    assert units[install.LEFTHOOK_FILE].kind == "file"

    pixi = units[install.PIXI_KEY]
    assert pixi.kind == "block"
    assert pixi.dest == "pixi.toml"
    assert pixi.anchor == "[tasks]"
    # The managed pixi block is the thin task lines ONLY — never a linter-dep
    # block (deps ride in as shipit's own package deps, architecture.lex §5).
    assert pixi.desired_inner() == 'lint = "shipit lint"\nlogs = "shipit logs"'


def test_pixi_block_inserts_under_existing_tasks_table():
    consumer = '[project]\nname = "acme"\n\n[tasks]\ntest = "pytest"\n'
    out = install.splice_block(
        consumer,
        'lint = "shipit lint"',
        install.PIXI_OPEN,
        install.PIXI_CLOSE,
        anchor="[tasks]",
    )
    # The managed line lands inside [tasks], not after some later table.
    tasks_idx = out.index("[tasks]")
    project_after = out.find("[project]", tasks_idx)
    lint_idx = out.index('lint = "shipit lint"')
    assert tasks_idx < lint_idx
    assert project_after == -1  # no table opens between [tasks] and the line
    assert 'test = "pytest"' in out
    # Round-trips through extract with the pixi markers.
    assert (
        install.extract_block(out, install.PIXI_OPEN, install.PIXI_CLOSE)
        == 'lint = "shipit lint"'
    )


def test_pixi_block_creates_tasks_table_when_absent():
    consumer = '[project]\nname = "acme"\n'
    out = install.splice_block(
        consumer,
        'lint = "shipit lint"',
        install.PIXI_OPEN,
        install.PIXI_CLOSE,
        anchor="[tasks]",
    )
    assert "[tasks]" in out
    # The block follows the freshly-added header.
    assert out.index("[tasks]") < out.index('lint = "shipit lint"')


def test_pixi_block_reinstall_replaces_in_place():
    consumer = '[tasks]\ntest = "pytest"\n'
    once = install.splice_block(
        consumer,
        'lint = "shipit lint"',
        install.PIXI_OPEN,
        install.PIXI_CLOSE,
        "[tasks]",
    )
    twice = install.splice_block(
        once, 'lint = "shipit lint"', install.PIXI_OPEN, install.PIXI_CLOSE, "[tasks]"
    )
    # Idempotent: exactly one managed block after a second install.
    assert twice.count(install.PIXI_OPEN) == 1
    assert twice == once


def test_load_units_has_skills_agents_and_bootstrap():
    units = install.load_units()
    keys = {u.key for u in units}
    assert "AGENTS.md#shipit-block" in keys
    assert "bin/shipit" in keys
    assert any(k.startswith("skills/") for k in keys)
    agents = next(u for u in units if u.key == "AGENTS.md#shipit-block")
    assert agents.kind == "block"
    boot = next(u for u in units if u.key == "bin/shipit")
    assert boot.executable is True


# --------------------------------------------------------------------------
# The HAR01 harness units — generated agent-defs + the settings.json hook line
# (docs/prd/har01-coordinator-guard-and-role-prompts.md, user stories 17 & 21)
# --------------------------------------------------------------------------


def test_load_units_includes_the_three_agent_defs():
    units = {u.key: u for u in install.load_units()}
    for role in ("implementer", "shepherd", "explorer"):
        key = f"{install.AGENTS_DEF_DIR}/{role}.md"
        assert key in units, f"{key} not registered"
        unit = units[key]
        assert unit.kind == "file"
        assert unit.dest == key
        # The bundled content is the generated agent-def (frontmatter names the role).
        assert f"name: {role}".encode() in unit.content


def test_load_units_includes_the_settings_hook_block():
    units = {u.key: u for u in install.load_units()}
    assert install.SETTINGS_KEY in units
    unit = units[install.SETTINGS_KEY]
    assert unit.kind == "block"
    assert unit.fmt == install.FMT_JSON_HOOK
    assert unit.dest == install.SETTINGS_FILE
    # The managed region is shipit's PreToolUse entry (canonical JSON), nothing else.
    entry = json.loads(unit.desired_inner())
    assert entry["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert install.SETTINGS_HOOK_MARKER in entry["hooks"][0]["command"]


def test_settings_hook_splice_preserves_other_settings():
    consumer = json.dumps(
        {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]
            },
        }
    )
    inner = json.dumps(
        {
            "matcher": "Edit|Write",
            "hooks": [
                {"type": "command", "command": "pixi run shipit hook pretooluse"}
            ],
        }
    )
    out = install.splice_settings_hook(consumer, inner)
    data = json.loads(out)
    # The consumer's unrelated settings survive untouched.
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    # shipit's entry is now present in PreToolUse.
    assert install.extract_settings_hook(out) == install._canonical_hook_entry(
        json.loads(inner)
    )


def _unit(key):
    return next(u for u in install.load_units() if u.key == key)


def test_settings_hook_splice_is_idempotent_and_replaces_in_place():
    inner = _unit(install.SETTINGS_KEY).desired_inner()
    once = install.splice_settings_hook("", inner)
    twice = install.splice_settings_hook(once, inner)
    assert twice == once
    # Exactly one shipit PreToolUse entry, even after a second splice.
    pre = json.loads(twice)["hooks"]["PreToolUse"]
    assert sum(install._is_shipit_hook(e) for e in pre) == 1


def test_settings_hook_extract_is_none_when_absent_or_unparseable():
    assert install.extract_settings_hook("") is None
    assert install.extract_settings_hook("{}") is None
    assert install.extract_settings_hook("not json") is None
    # A settings.json with only the consumer's own hooks → no shipit entry.
    other = json.dumps(
        {"hooks": {"PreToolUse": [{"hooks": [{"command": "echo other"}]}]}}
    )
    assert install.extract_settings_hook(other) is None


def test_settings_hook_reconciles_through_the_four_cases():
    """The settings hook unit gives the standard ADD/NOOP/UPDATE/OVERRIDE decisions."""
    unit = _unit(install.SETTINGS_KEY)
    desired = unit.desired_hash()
    extract = install.extract_settings_hook
    h = lambda inner: config.content_hash(inner.encode("utf-8"))  # noqa: E731

    # absent → ADD
    assert (
        install.decide(consumer_hash=None, pristine_hash=None, desired_hash=desired)
        == install.ADD
    )
    # unchanged (consumer carries shipit's exact entry) → NOOP
    on_disk = install.splice_settings_hook("", unit.desired_inner())
    cur = h(extract(on_disk))
    assert cur == desired
    assert (
        install.decide(consumer_hash=cur, pristine_hash=desired, desired_hash=desired)
        == install.NOOP
    )
    # consumer edited shipit's own entry → OVERRIDE (not clobbered, surfaced in PR)
    edited = on_disk.replace("Edit|Write|MultiEdit|NotebookEdit", "Edit")
    cedit = h(extract(edited))
    assert cedit != desired
    assert (
        install.decide(consumer_hash=cedit, pristine_hash=desired, desired_hash=desired)
        == install.OVERRIDE
    )


# --------------------------------------------------------------------------
# The verb — gh boundary patched
# --------------------------------------------------------------------------


class _GhRecorder:
    """Records the git/PR boundary calls install makes, doing nothing real."""

    def __init__(self):
        self.calls = []
        self.pr_body = None
        self.hook_activations = []

    def activate_hooks(self, root):
        # Stand in for `lefthook install`: record the call, mutate nothing.
        self.hook_activations.append(root)
        return (0, "")

    def git_switch_create(self, branch, *, cwd):
        self.calls.append(("switch", branch))

    def git_add(self, paths, *, cwd):
        self.calls.append(("add", tuple(paths)))

    def git_commit(self, message, paths, *, cwd):
        self.calls.append(("commit", message))

    def git_push(self, branch, *, cwd, remote="origin", force=False):
        self.calls.append(("push", branch))

    def git_current_branch(self, *, cwd):
        return "main"

    def pr_url_for_head(self, branch, *, cwd=None):
        return None  # no existing PR by default

    def pr_create(self, *, head, title, body, draft, cwd, **kw):
        self.calls.append(("pr_create", draft))
        self.pr_body = body
        return "https://github.com/acme/repo/pull/1"

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rec(monkeypatch):
    r = _GhRecorder()
    for name in (
        "git_switch_create",
        "git_add",
        "git_commit",
        "git_push",
        "git_current_branch",
        "pr_url_for_head",
        "pr_create",
    ):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(install, "_shipit_version", lambda: "testhash")
    # Inject the lefthook boundary so no test spawns a real `lefthook install`
    # (mirrors how lint tests inject run_tool). Real activation is covered
    # directly against subprocess in test_activate_hooks_* below.
    monkeypatch.setattr(install, "_activate_hooks", r.activate_hooks)
    return r


def test_dry_run_has_no_side_effects(tmp_path, rec):
    rc = install.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert not (tmp_path / ".shipit.toml").exists()
    assert not (tmp_path / "skills").exists()
    assert rec.calls == []  # no git, no PR


def test_fresh_install_writes_set_and_opens_draft_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n\nConsumer text.\n")
    rc = install.run(str(tmp_path))
    assert rc == 0

    # Managed files landed.
    assert (tmp_path / "skills" / "shipit-to-prd" / "SKILL.md").is_file()
    assert (tmp_path / "bin" / "shipit").is_file()
    # The AGENTS block was spliced in without losing the consumer's text.
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "Consumer text." in agents
    assert install.BLOCK_OPEN in agents

    # Manifest written with version + a pristine for every unit.
    cfg = config.load(tmp_path / ".shipit.toml")
    assert config.shipit_version(cfg) == "testhash"
    managed = config.load_managed(cfg)
    assert "bin/shipit" in managed and "AGENTS.md#shipit-block" in managed

    # A DRAFT PR was opened; the body lists the additions.
    assert ("pr_create", True) in rec.calls
    assert "### Added" in rec.pr_body
    # Order: branch -> add -> commit -> push -> pr.
    assert rec.names() == ["switch", "add", "commit", "push", "pr_create"]


def test_fresh_install_provisions_agent_defs_and_settings_hook(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0

    # The three generated agent-defs land under .claude/agents/.
    for role in ("implementer", "shepherd", "explorer"):
        dest = tmp_path / ".claude" / "agents" / f"{role}.md"
        assert dest.is_file()
        assert f"name: {role}" in dest.read_text()

    # The PreToolUse hook line lands in .claude/settings.json.
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"]
    assert any(install._is_shipit_hook(e) for e in pre)

    # Both kinds recorded a pristine hash in the manifest.
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert ".claude/agents/implementer.md" in managed
    assert install.SETTINGS_KEY in managed


def test_install_merges_settings_hook_without_clobbering_consumer_settings(
    tmp_path, rec
):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    # A consumer who already has settings.json with their own permissions + hook.
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo hi"}]}
                    ]
                },
            },
            indent=2,
        )
    )
    rc = install.run(str(tmp_path))
    assert rc == 0

    merged = json.loads(settings_path.read_text())
    # The consumer's settings are intact, and shipit's hook was merged alongside.
    assert merged["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert any(install._is_shipit_hook(e) for e in merged["hooks"]["PreToolUse"])


def test_consumer_edit_to_settings_hook_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    rec.calls.clear()

    # The consumer narrows shipit's managed PreToolUse matcher.
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    for entry in data["hooks"]["PreToolUse"]:
        if install._is_shipit_hook(entry):
            entry["matcher"] = "Edit"
    settings_path.write_text(json.dumps(data, indent=2))

    rc = install.run(str(tmp_path))
    assert rc == 0
    assert ("pr_create", True) in rec.calls
    # The edited unit is surfaced as an override with its diff, never clobbered blind.
    assert "### Overrides" in rec.pr_body
    assert install.SETTINGS_FILE in rec.pr_body


def test_consumer_edit_to_agent_def_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    rec.calls.clear()

    (tmp_path / ".claude" / "agents" / "implementer.md").write_text("HAND EDIT\n")
    rc = install.run(str(tmp_path))
    assert rc == 0
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert ".claude/agents/implementer.md" in rec.pr_body
    assert "HAND EDIT" in rec.pr_body


def test_reinstall_with_no_changes_is_a_clean_noop(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    rec.calls.clear()
    rc = install.run(str(tmp_path))
    assert rc == 0
    # Nothing committed, no PR opened the second time.
    assert rec.calls == []


def test_consumer_edit_surfaces_as_override(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    rec.calls.clear()

    # The consumer edits a managed skill file.
    skill = tmp_path / "skills" / "shipit-to-prd" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")

    rc = install.run(str(tmp_path))
    assert rc == 0
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert "skills/shipit-to-prd/SKILL.md" in rec.pr_body
    # The diff is captured BEFORE the overwrite, so it shows the consumer's edit
    # (a non-empty diff), not an empty diff against what shipit just wrote.
    assert "CONSUMER EDIT" in rec.pr_body
    assert "```diff" in rec.pr_body


def test_open_install_pr_is_updated_not_recreated(tmp_path, rec, monkeypatch):
    # An install PR already exists for the branch (a prior unmerged install).
    monkeypatch.setattr(
        gh, "pr_url_for_head", lambda branch, cwd=None: "https://x/pull/7"
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0
    # The branch was force-pushed, but no second PR was created.
    assert "push" in rec.names()
    assert "pr_create" not in rec.names()


def test_push_flag_pushes_to_branch_without_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path), push=True)
    assert rc == 0
    assert ("push", "main") in rec.calls
    assert "pr_create" not in rec.names()


def test_stale_manifest_keys_are_dropped(tmp_path, rec):
    # A prior manifest claims a unit shipit no longer manages.
    config.write_manifest(
        tmp_path / ".shipit.toml",
        version="old",
        managed={"skills/retired/SKILL.md": "sha256:dead", "bin/shipit": "sha256:old"},
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    # The retired key is gone; the manifest reflects only the current set.
    assert "skills/retired/SKILL.md" not in managed
    assert set(managed) == {u.key for u in install.load_units()}


def test_gh_failure_is_a_clean_nonzero_exit(tmp_path, monkeypatch, rec):
    def boom(*a, **k):
        raise gh.GhError("no remote configured")

    monkeypatch.setattr(gh, "git_switch_create", boom)
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 1  # clean exit, not a raised traceback


# --------------------------------------------------------------------------
# Seed-if-absent consumer policy — App [secrets] mappings + [reviewers] set
# --------------------------------------------------------------------------


def _secrets_by_name(root):
    cfg = config.load(root / ".shipit.toml")
    return {s.name: s for s in config.load_secrets(cfg)}


def test_fresh_install_seeds_app_secret_mappings(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0

    secrets = _secrets_by_name(tmp_path)
    for name in (
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
        "AGY_REVIEW_APP_PRIVATE_KEY",
        "AGY_REVIEW_APP_ID",
    ):
        assert name in secrets
        # Each maps to its like-named Doppler key (matches shipit's own .shipit.toml).
        assert secrets[name].kind == "doppler"
        assert secrets[name].key == name
    # The PR body announces the seed under its own section.
    assert "### Policy seeded" in rec.pr_body
    assert "[secrets].CODEX_REVIEW_APP_PRIVATE_KEY" in rec.pr_body


def test_fresh_install_seeds_required_reviewer_set(tmp_path, rec):
    from shipit.prstate import reviewers_config as rcfg

    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))

    # The seeded [reviewers] table requires all three — copilot + the codex/agy
    # local-agent backends — matching shipit's own .shipit.toml.
    override = rcfg.load_override(str(tmp_path))
    assert rcfg.resolve_required_names(override) == ("copilot", "codex", "agy")


def test_install_preserves_existing_secrets_and_reviewers(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    (tmp_path / ".shipit.toml").write_text(
        "[secrets]\n"
        'MY_TOKEN = { env = "MY_TOKEN" }\n'
        # A consumer who deliberately points one App secret at a custom key must
        # NOT be clobbered by the seed.
        'CODEX_REVIEW_APP_ID = { doppler = "CUSTOM_KEY" }\n'
        "\n[reviewers]\n"
        "copilot = { rerun = true }\n"
    )
    rc = install.run(str(tmp_path))
    assert rc == 0

    secrets = _secrets_by_name(tmp_path)
    # Consumer entries are left exactly as written.
    assert secrets["MY_TOKEN"].kind == "env"
    assert secrets["CODEX_REVIEW_APP_ID"].key == "CUSTOM_KEY"
    # The absent App mappings are merged in alongside them.
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "AGY_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "AGY_REVIEW_APP_ID" in secrets
    # The pre-existing [reviewers] table is untouched — not overwritten by the scaffold.
    cfg = config.load(tmp_path / ".shipit.toml")
    assert cfg["reviewers"] == {"copilot": {"rerun": True}}


def test_reinstall_does_not_reseed_policy(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    before = (tmp_path / ".shipit.toml").read_text()

    rec.calls.clear()
    rc = install.run(str(tmp_path))
    assert rc == 0
    # Clean no-op: no PR, and the policy text is byte-identical (not re-appended).
    assert rec.calls == []
    assert (tmp_path / ".shipit.toml").read_text() == before


def test_install_reseeds_policy_when_missing_even_if_managed_current(tmp_path, rec):
    # Simulate an older install (or a consumer who dropped the policy tables): the
    # managed set is fully current but `[secrets]`/`[reviewers]` are absent.
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    cfg_path = tmp_path / ".shipit.toml"
    managed = config.load_managed(config.load(cfg_path))
    cfg_path.write_text(config.dump_manifest("testhash", managed))  # policy stripped

    rec.calls.clear()
    rc = install.run(str(tmp_path))
    assert rc == 0
    # A seed-only change still opens a DRAFT PR (managed set NOOP, policy seeded)...
    assert ("pr_create", True) in rec.calls
    assert "### Policy seeded" in rec.pr_body
    # ...but it does NOT claim to (re)activate the checks — no managed unit was written.
    assert "### Checks activated locally" not in rec.pr_body
    # ...and the policy is back in place.
    secrets = _secrets_by_name(tmp_path)
    assert "CODEX_REVIEW_APP_PRIVATE_KEY" in secrets
    assert "reviewers" in config.load(cfg_path)


def test_dry_run_does_not_seed_policy(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path), dry_run=True)
    assert rc == 0
    # No file written on a dry-run, so nothing is seeded.
    assert not (tmp_path / ".shipit.toml").exists()


# --------------------------------------------------------------------------
# Checks activation — the lefthook.yml caller is turned LIVE, not just written
# --------------------------------------------------------------------------


def test_activates_hooks_is_true_iff_lefthook_is_managed():
    units = install.load_units()
    decisions = install.plan(units, {}, {})
    assert install.activates_hooks(decisions) is True

    # A set with no lefthook unit does not activate.
    others = [d for d in decisions if d.unit.key != install.LEFTHOOK_FILE]
    assert install.activates_hooks(others) is False


def test_fresh_install_activates_the_check_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0
    # The lefthook boundary was invoked exactly once, on the consumer root.
    assert len(rec.hook_activations) == 1
    assert rec.hook_activations[0] == tmp_path.resolve()
    # The PR body announces the checks are live.
    assert "### Checks activated" in rec.pr_body
    assert "lefthook install" in rec.pr_body


def test_break_glass_push_also_activates_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path), push=True)
    assert rc == 0
    assert len(rec.hook_activations) == 1


def test_dry_run_does_not_activate_hooks(tmp_path, rec):
    rc = install.run(str(tmp_path), dry_run=True)
    assert rc == 0
    assert rec.hook_activations == []  # no side effect on dry-run


def test_reinstall_with_writes_reactivates_idempotently(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    install.run(str(tmp_path))
    assert len(rec.hook_activations) == 1
    # A consumer edit forces a writing re-install; activation re-runs (safe
    # because `lefthook install` is idempotent — we never hand-roll a hook).
    (tmp_path / "lefthook.yml").write_text("CONSUMER EDIT\n")
    rec.calls.clear()
    install.run(str(tmp_path))
    assert len(rec.hook_activations) == 2


def test_install_warns_but_succeeds_when_lefthook_missing(tmp_path, monkeypatch, rec):
    # The boundary reports a missing binary (127); install must still finish its
    # PR rather than aborting — activation is opportunistic, not a hard-fail check.
    rec.hook_activations.clear()
    monkeypatch.setattr(
        install, "_activate_hooks", lambda root: (127, "lefthook: not found on PATH")
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0
    assert ("pr_create", True) in rec.calls
    # The PR body must NOT claim the checks went live on this failure path; it
    # records that local activation was deferred so a merger knows to act.
    assert "### Checks activated locally" not in rec.pr_body
    assert "local activation skipped" in rec.pr_body
    assert "lefthook install" in rec.pr_body


def test_activate_hooks_boundary_runs_lefthook_install(tmp_path, monkeypatch):
    # The real boundary shells out to `lefthook install` (the install-hooks task
    # invocation), in the consumer root — never a re-implemented hook writer.
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "sync hooks: ✔️ pre-commit, ✔️ pre-push\n"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return _Proc()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    rc, out = install._activate_hooks(tmp_path)
    assert rc == 0
    assert captured["argv"] == ["lefthook", "install"]
    assert captured["cwd"] == str(tmp_path)
    assert "pre-commit" in out


def test_activate_hooks_boundary_reports_missing_binary(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("lefthook")

    monkeypatch.setattr(install.subprocess, "run", boom)
    rc, out = install._activate_hooks(tmp_path)
    assert rc == 127
    # Points at the canonical recovery, which works in a consumer repo too.
    assert "lefthook install" in out


def test_activate_hooks_boundary_reports_unexecutable_binary(tmp_path, monkeypatch):
    # A present-but-not-executable lefthook raises PermissionError (an OSError);
    # install must warn, not crash, exactly as for a missing binary.
    def boom(*a, **k):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(install.subprocess, "run", boom)
    rc, out = install._activate_hooks(tmp_path)
    assert rc == 127
    assert "lefthook install" in out
