"""Unit tests for install — reconciliation decisions, block splicing, and the verb."""

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
# The gate units (Step 3) — lefthook caller + pixi [tasks] block
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
    assert (tmp_path / "skills" / "shipt-to-prd" / "SKILL.md").is_file()
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
    skill = tmp_path / "skills" / "shipt-to-prd" / "SKILL.md"
    skill.write_text("CONSUMER EDIT\n")

    rc = install.run(str(tmp_path))
    assert rc == 0
    assert ("pr_create", True) in rec.calls
    assert "### Overrides" in rec.pr_body
    assert "skills/shipt-to-prd/SKILL.md" in rec.pr_body
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
    # ...but it does NOT claim to (re)activate the gate — no managed unit was written.
    assert "### Gate activated locally" not in rec.pr_body
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
# Gate activation — the lefthook.yml caller is turned LIVE, not just written
# --------------------------------------------------------------------------


def test_activates_hooks_is_true_iff_lefthook_is_managed():
    units = install.load_units()
    decisions = install.plan(units, {}, {})
    assert install.activates_hooks(decisions) is True

    # A set with no lefthook unit does not activate.
    others = [d for d in decisions if d.unit.key != install.LEFTHOOK_FILE]
    assert install.activates_hooks(others) is False


def test_fresh_install_activates_the_gate_hooks(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0
    # The lefthook boundary was invoked exactly once, on the consumer root.
    assert len(rec.hook_activations) == 1
    assert rec.hook_activations[0] == tmp_path.resolve()
    # The PR body announces the gate is live.
    assert "### Gate activated" in rec.pr_body
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
    # PR rather than aborting — activation is opportunistic, not a hard gate.
    rec.hook_activations.clear()
    monkeypatch.setattr(
        install, "_activate_hooks", lambda root: (127, "lefthook: not found on PATH")
    )
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path))
    assert rc == 0
    assert ("pr_create", True) in rec.calls
    # The PR body must NOT claim the gate went live on this failure path; it
    # records that local activation was deferred so a merger knows to act.
    assert "### Gate activated locally" not in rec.pr_body
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
