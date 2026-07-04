"""Unit tests for the gh-setup passes — ruleset payload, labels, secrets, idempotence."""

import pytest

from shipit.config import SecretSource
from shipit.identity import repo_from_slug
from shipit.verbs import gh_setup


# --------------------------------------------------------------------------
# Packaged data
# --------------------------------------------------------------------------


def test_template_is_cleaned():
    tmpl = gh_setup.load_template()
    # Per-repo capture fields are stripped from the shipped template.
    assert "id" not in tmpl
    assert "source" not in tmpl
    assert "source_type" not in tmpl
    assert tmpl["name"] == gh_setup.RULESET_NAME
    rule = next(r for r in tmpl["rules"] if r["type"] == "required_status_checks")
    assert rule["parameters"]["required_status_checks"] == []


def test_template_pins_automatic_copilot_review_off():
    """RVW01 sole-requester drift protection (ADR-0031): the pull_request rule
    explicitly pins automatic Copilot review off, so re-running gh-setup erases
    any hand-enabled auto-review."""
    tmpl = gh_setup.load_template()
    rule = next(r for r in tmpl["rules"] if r["type"] == "pull_request")
    assert rule["parameters"]["automatic_copilot_code_review_enabled"] is False


def test_load_labels_full_set_with_colors():
    labels = gh_setup.load_labels()
    names = {label.name for label in labels}
    assert names == {
        "bug",
        "feature",
        "ready-for-agent",
        "small",
        "needs-decision",
        "duplicate-of",
    }
    for label in labels:
        assert label.description, f"{label.name} missing description"
        assert len(label.color) == 6, f"{label.name} color not 6-hex: {label.color!r}"


# --------------------------------------------------------------------------
# Pure ruleset logic
# --------------------------------------------------------------------------


def test_build_payload_injects_checks_only():
    tmpl = gh_setup.load_template()
    body = gh_setup.build_payload(tmpl, ["app-ui / check", "wire / check"])
    rule = next(r for r in body["rules"] if r["type"] == "required_status_checks")
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "app-ui / check"},
        {"context": "wire / check"},
    ]
    # The template is not mutated (deepcopy).
    src_rule = next(r for r in tmpl["rules"] if r["type"] == "required_status_checks")
    assert src_rule["parameters"]["required_status_checks"] == []


def test_build_payload_preserves_copilot_pin():
    """Injecting required checks must not disturb the pull_request rule — the
    automatic-Copilot-review pin survives into the built payload."""
    tmpl = gh_setup.load_template()
    body = gh_setup.build_payload(tmpl, ["app-ui / check"])
    rule = next(r for r in body["rules"] if r["type"] == "pull_request")
    assert rule["parameters"]["automatic_copilot_code_review_enabled"] is False


def test_existing_ruleset_id():
    rulesets = [
        {"name": "other", "id": 1},
        {"name": "main-branch-protection", "id": 42},
    ]
    assert gh_setup.existing_ruleset_id(rulesets, "main-branch-protection") == 42
    assert gh_setup.existing_ruleset_id(rulesets, "absent") is None
    assert gh_setup.existing_ruleset_id(None, "x") is None


# --------------------------------------------------------------------------
# Passes, with a recording fake gh boundary
# --------------------------------------------------------------------------


class FakeGh:
    """Records calls and serves canned ruleset-list responses."""

    def __init__(self, existing_rulesets=None):
        self.calls = []
        self._rulesets = existing_rulesets or []
        self.secrets = {}
        self.labels = []

    def rest(self, path, *, method=None, body=None, paginate=False):
        self.calls.append(("rest", path, method))
        if path.endswith("/rulesets") and method is None:
            return self._rulesets
        return None

    def label_create(self, repo, name, *, description, color):
        self.labels.append(name)

    def secret_set(self, name, value, *, repo):
        self.secrets[name] = value


@pytest.fixture
def fake_gh(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(gh_setup.gh, "rest", fake.rest)
    monkeypatch.setattr(gh_setup.gh, "label_create", fake.label_create)
    monkeypatch.setattr(gh_setup.gh, "secret_set", fake.secret_set)
    return fake


def test_apply_ruleset_creates_when_absent(fake_gh):
    action = gh_setup.apply_ruleset("o/r", ["c1"], dry_run=False)
    assert action == "created"
    assert ("rest", "repos/o/r/rulesets", "POST") in fake_gh.calls


def test_apply_ruleset_updates_when_present(monkeypatch):
    fake = FakeGh(existing_rulesets=[{"name": "main-branch-protection", "id": 7}])
    monkeypatch.setattr(gh_setup.gh, "rest", fake.rest)
    action = gh_setup.apply_ruleset("o/r", ["c1"], dry_run=False)
    assert action == "updated"
    assert ("rest", "repos/o/r/rulesets/7", "PUT") in fake.calls


def test_apply_ruleset_dry_run_sends_nothing(fake_gh):
    action = gh_setup.apply_ruleset("o/r", ["c1"], dry_run=True)
    assert action == "dry-run"
    assert not any(m in ("POST", "PUT") for (_, _, m) in fake_gh.calls)


def test_ensure_labels_upserts_all(fake_gh):
    n = gh_setup.ensure_labels("o/r", gh_setup.load_labels(), dry_run=False)
    assert n == 6
    assert set(fake_gh.labels) == {
        "bug",
        "feature",
        "ready-for-agent",
        "small",
        "needs-decision",
        "duplicate-of",
    }


def test_push_secrets_sets_and_skips_optional(fake_gh, monkeypatch):
    monkeypatch.setenv("VAR_A", "secret-a")
    monkeypatch.delenv("VAR_B", raising=False)
    sources = [
        SecretSource("A", "env", "VAR_A", False),  # present → set
        SecretSource("B", "env", "VAR_B", True),  # optional, missing → skip
    ]
    set_count, skipped, failed = gh_setup.push_secrets("o/r", sources, dry_run=False)
    assert set_count == 1
    assert skipped == 1
    assert failed == 0
    assert fake_gh.secrets == {"A": "secret-a"}


def test_push_secrets_required_failure_does_not_crash(fake_gh, monkeypatch):
    monkeypatch.delenv("VAR_MISSING", raising=False)
    sources = [SecretSource("X", "env", "VAR_MISSING", False)]  # required, absent
    set_count, skipped, failed = gh_setup.push_secrets("o/r", sources, dry_run=False)
    assert (set_count, skipped, failed) == (0, 0, 1)
    assert fake_gh.secrets == {}  # nothing pushed, no exception escaped


def test_run_dry_run_end_to_end(monkeypatch, capsys):
    fake = FakeGh()
    monkeypatch.setattr(gh_setup.gh, "rest", fake.rest)
    monkeypatch.setattr(gh_setup.gh, "label_create", fake.label_create)
    monkeypatch.setattr(gh_setup.gh, "secret_set", fake.secret_set)
    monkeypatch.setattr(gh_setup.git, "repo_root", lambda: "/somewhere/o-r")
    monkeypatch.setattr(gh_setup.gh, "current_repo", lambda: repo_from_slug("o/r"))
    monkeypatch.setattr(gh_setup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(gh_setup.checks_mod, "discover", lambda *a, **k: ["c / check"])

    rc = gh_setup.run(None, config_path=None, checks_override=None, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "gh-setup: o/r (dry-run)" in out
    assert "c / check" in out
    # Dry-run sends no writes.
    assert not any(m in ("POST", "PUT") for (_, _, m) in fake.calls)


def test_remote_target_does_not_read_local_workflows(monkeypatch):
    """A target that isn't the current checkout must NOT pass local toplevel."""
    fake = FakeGh()
    monkeypatch.setattr(gh_setup.gh, "rest", fake.rest)
    monkeypatch.setattr(gh_setup.gh, "label_create", fake.label_create)
    monkeypatch.setattr(gh_setup.gh, "secret_set", fake.secret_set)
    monkeypatch.setattr(gh_setup.git, "repo_root", lambda: "/somewhere/shipit")
    monkeypatch.setattr(
        gh_setup.gh, "current_repo", lambda: repo_from_slug("me/shipit")
    )
    monkeypatch.setattr(gh_setup.gh, "default_branch", lambda repo: "main")

    seen = {}

    def fake_discover(target, branch, *, toplevel):
        seen["toplevel"] = toplevel
        return []

    monkeypatch.setattr(gh_setup.checks_mod, "discover", fake_discover)
    gh_setup.run(
        "other/repo", config_path="/dev/null", checks_override=None, dry_run=True
    )
    # Target is not the checkout's repo → local discovery is disabled.
    assert seen["toplevel"] is None


def test_no_repo_and_no_checkout_errors(monkeypatch):
    monkeypatch.setattr(gh_setup.git, "repo_root", lambda: None)
    rc = gh_setup.run(None, config_path=None, checks_override=None, dry_run=True)
    assert rc == 1
