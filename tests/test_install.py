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
