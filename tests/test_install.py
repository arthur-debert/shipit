"""Unit tests for install — reconciliation decisions, block splicing, and the verb."""

import pytest

from shipit import config, gh
from shipit.verbs import install


# --------------------------------------------------------------------------
# Pure reconciliation
# --------------------------------------------------------------------------


def test_decide_covers_four_cases():
    # absent -> ADD
    assert install.decide(consumer_hash=None, pristine_hash=None, desired_hash="d") == install.ADD
    # already current -> NOOP
    assert install.decide(consumer_hash="d", pristine_hash="p", desired_hash="d") == install.NOOP
    # untouched since last install -> UPDATE
    assert install.decide(consumer_hash="p", pristine_hash="p", desired_hash="d") == install.UPDATE
    # consumer-edited -> OVERRIDE
    assert install.decide(consumer_hash="x", pristine_hash="p", desired_hash="d") == install.OVERRIDE
    # present but never installed by shipit (no pristine) and divergent -> OVERRIDE
    assert install.decide(consumer_hash="x", pristine_hash=None, desired_hash="d") == install.OVERRIDE


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

    def git_push(self, branch, *, cwd, remote="origin"):
        self.calls.append(("push", branch))

    def git_current_branch(self, *, cwd):
        return "main"

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


def test_push_flag_pushes_to_branch_without_pr(tmp_path, rec):
    (tmp_path / "AGENTS.md").write_text("# Acme\n")
    rc = install.run(str(tmp_path), push=True)
    assert rc == 0
    assert ("push", "main") in rec.calls
    assert "pr_create" not in rec.names()
