"""gh-setup through the ADR-0030 seam (CLI02-WS04).

Domain tests drive :mod:`shipit.ghsetup` typed-in/typed-out — every pass
returns an outcome value, :func:`~shipit.ghsetup.setup` one frozen
:class:`~shipit.ghsetup.SetupReport`; no capsys, no prints in the domain. The
verb layer is covered by a thin wiring smoke layer (glue: ambient identity →
values → domain → render) plus pure-renderer assertions that freeze the text
surface.
"""

from typing import Any

import pytest

from shipit import ghsetup
from shipit.config import SecretSource
from shipit.identity import Revision, WorkingDir, repo_from_slug
from shipit.verbs import gh_setup as gh_setup_verb
from shipit.verbs._context import RootContext

REPO = repo_from_slug("o/r")


def get_rule(ruleset: dict[str, Any], rule_type: str) -> dict[str, Any]:
    """Return the single rule of the given type from a ruleset dict."""
    return next(r for r in ruleset["rules"] if r["type"] == rule_type)


# --------------------------------------------------------------------------
# Packaged data
# --------------------------------------------------------------------------


def test_template_is_cleaned():
    tmpl = ghsetup.load_template()
    # Per-repo capture fields are stripped from the shipped template.
    assert "id" not in tmpl
    assert "source" not in tmpl
    assert "source_type" not in tmpl
    assert tmpl["name"] == ghsetup.RULESET_NAME
    rule = get_rule(tmpl, "required_status_checks")
    assert rule["parameters"]["required_status_checks"] == []


def test_template_pins_automatic_copilot_review_off():
    """RVW01 sole-requester drift protection (ADR-0031): the pull_request rule
    explicitly pins automatic Copilot review off, so re-running gh-setup erases
    any hand-enabled auto-review."""
    tmpl = ghsetup.load_template()
    rule = get_rule(tmpl, "pull_request")
    assert rule["parameters"]["automatic_copilot_code_review_enabled"] is False


def test_load_labels_full_set_with_colors():
    labels = ghsetup.load_labels()
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
    tmpl = ghsetup.load_template()
    body = ghsetup.build_payload(tmpl, ["app-ui / check", "wire / check"])
    rule = get_rule(body, "required_status_checks")
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "app-ui / check"},
        {"context": "wire / check"},
    ]
    # The template is not mutated (deepcopy).
    src_rule = get_rule(tmpl, "required_status_checks")
    assert src_rule["parameters"]["required_status_checks"] == []


def test_build_payload_preserves_copilot_pin():
    """Injecting required checks must not disturb the pull_request rule — it
    flows into the built payload strictly equal to the template's rule,
    Copilot pin included."""
    tmpl = ghsetup.load_template()
    body = ghsetup.build_payload(tmpl, ["app-ui / check"])
    rule = get_rule(body, "pull_request")
    assert rule == get_rule(tmpl, "pull_request")
    assert rule["parameters"]["automatic_copilot_code_review_enabled"] is False


def test_existing_ruleset_id():
    rulesets = [
        {"name": "other", "id": 1},
        {"name": "main-branch-protection", "id": 42},
    ]
    assert ghsetup.existing_ruleset_id(rulesets, "main-branch-protection") == 42
    assert ghsetup.existing_ruleset_id(rulesets, "absent") is None
    assert ghsetup.existing_ruleset_id(None, "x") is None


# --------------------------------------------------------------------------
# Passes, with a recording fake gh boundary — typed outcomes out
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
    monkeypatch.setattr(ghsetup.gh, "rest", fake.rest)
    monkeypatch.setattr(ghsetup.gh, "label_create", fake.label_create)
    monkeypatch.setattr(ghsetup.gh, "secret_set", fake.secret_set)
    return fake


def test_apply_ruleset_creates_when_absent(fake_gh):
    outcome = ghsetup.apply_ruleset("o/r", ["c1"], dry_run=False)
    assert outcome.action == "created"
    assert outcome.existing_id is None
    assert outcome.checks == ("c1",)
    assert ("rest", "repos/o/r/rulesets", "POST") in fake_gh.calls


def test_apply_ruleset_updates_when_present(monkeypatch):
    fake = FakeGh(existing_rulesets=[{"name": "main-branch-protection", "id": 7}])
    monkeypatch.setattr(ghsetup.gh, "rest", fake.rest)
    outcome = ghsetup.apply_ruleset("o/r", ["c1"], dry_run=False)
    assert outcome.action == "updated"
    assert outcome.existing_id == 7
    assert ("rest", "repos/o/r/rulesets/7", "PUT") in fake.calls


def test_apply_ruleset_dry_run_sends_nothing_and_carries_the_payload(fake_gh):
    outcome = ghsetup.apply_ruleset("o/r", ["c1"], dry_run=True)
    assert outcome.action == "dry-run"
    assert not any(m in ("POST", "PUT") for (_, _, m) in fake_gh.calls)
    # The would-be payload rides the outcome, checks injected.
    rule = get_rule(outcome.payload, "required_status_checks")
    assert rule["parameters"]["required_status_checks"] == [{"context": "c1"}]


def test_ensure_labels_upserts_all(fake_gh):
    outcomes = ghsetup.ensure_labels("o/r", ghsetup.load_labels(), dry_run=False)
    assert len(outcomes) == 6
    assert all(o.action == "upserted" for o in outcomes)
    assert set(fake_gh.labels) == {
        "bug",
        "feature",
        "ready-for-agent",
        "small",
        "needs-decision",
        "duplicate-of",
    }


def test_ensure_labels_dry_run_touches_nothing(fake_gh):
    outcomes = ghsetup.ensure_labels("o/r", ghsetup.load_labels(), dry_run=True)
    assert all(o.action == "dry-run" for o in outcomes)
    assert fake_gh.labels == []


def test_push_secrets_sets_and_skips_optional(fake_gh, monkeypatch):
    monkeypatch.setenv("VAR_A", "secret-a")
    monkeypatch.delenv("VAR_B", raising=False)
    sources = [
        SecretSource("A", "env", "VAR_A", False),  # present → set
        SecretSource("B", "env", "VAR_B", True),  # optional, missing → skip
    ]
    outcomes = ghsetup.push_secrets("o/r", sources, dry_run=False)
    assert [(o.name, o.action) for o in outcomes] == [("A", "set"), ("B", "skipped")]
    assert outcomes[1].reason == "optional source absent"
    assert fake_gh.secrets == {"A": "secret-a"}


def test_push_secrets_required_failure_does_not_crash(fake_gh, monkeypatch):
    monkeypatch.delenv("VAR_MISSING", raising=False)
    sources = [SecretSource("X", "env", "VAR_MISSING", False)]  # required, absent
    outcomes = ghsetup.push_secrets("o/r", sources, dry_run=False)
    assert [(o.name, o.action) for o in outcomes] == [("X", "failed")]
    assert outcomes[0].reason  # the why rides the outcome
    assert fake_gh.secrets == {}  # nothing pushed, no exception escaped


def test_push_secrets_dry_run_never_resolves(fake_gh, monkeypatch):
    """A dry run records the intended source without resolving it — no doppler,
    no prompt, no value anywhere in the report."""
    monkeypatch.delenv("VAR_MISSING", raising=False)
    sources = [SecretSource("X", "env", "VAR_MISSING", False)]
    outcomes = ghsetup.push_secrets("o/r", sources, dry_run=True)
    assert [(o.name, o.action, o.source) for o in outcomes] == [("X", "dry-run", "env")]
    assert fake_gh.secrets == {}


# --------------------------------------------------------------------------
# Orchestrator — one typed SetupReport out
# --------------------------------------------------------------------------


def test_setup_dry_run_reports_and_mutates_nothing(fake_gh, monkeypatch):
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(ghsetup.checks_mod, "discover", lambda *a, **k: ["c / check"])

    report = ghsetup.setup(REPO, config_path="/dev/null", dry_run=True)
    assert report.repo == "o/r"
    assert report.dry_run is True
    assert report.ruleset.action == "dry-run"
    assert report.ruleset.checks == ("c / check",)
    assert all(label.action == "dry-run" for label in report.labels)
    # No config at /dev/null → the degraded secrets outcome, recorded not raised.
    assert report.secrets == ()
    assert report.secrets_error is not None
    assert report.secrets_failed == 0
    # Dry-run sends no writes.
    assert not any(m in ("POST", "PUT") for (_, _, m) in fake_gh.calls)


def test_setup_pushes_configured_secrets(fake_gh, monkeypatch, tmp_path):
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(ghsetup.checks_mod, "discover", lambda *a, **k: ["c / check"])
    monkeypatch.setenv("VAR_A", "secret-a")
    cfg = tmp_path / ".shipit.toml"
    cfg.write_text('[secrets]\nA = { env = "VAR_A" }\n', encoding="utf-8")

    report = ghsetup.setup(REPO, config_path=str(cfg), dry_run=False)
    assert report.secrets_error is None
    assert [(s.name, s.action) for s in report.secrets] == [("A", "set")]
    assert (report.secrets_set, report.secrets_skipped, report.secrets_failed) == (
        1,
        0,
        0,
    )
    assert fake_gh.secrets == {"A": "secret-a"}
    assert report.ruleset.action == "created"
    assert ("rest", "repos/o/r/rulesets", "POST") in fake_gh.calls


def test_setup_discovery_respects_local_checkout(fake_gh, monkeypatch):
    """``local_checkout`` flows straight through to checks discovery — ``None``
    (a remote target) disables reading local workflow files."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    seen = {}

    def fake_discover(target, branch, *, toplevel):
        seen["toplevel"] = toplevel
        return []

    monkeypatch.setattr(ghsetup.checks_mod, "discover", fake_discover)
    ghsetup.setup(REPO, local_checkout=None, config_path="/dev/null", dry_run=True)
    assert seen["toplevel"] is None


def test_setup_checks_override_skips_discovery(fake_gh, monkeypatch):
    def boom(*a, **k):  # neither default_branch nor discover may be called
        raise AssertionError("discovery must be skipped under --checks")

    monkeypatch.setattr(ghsetup.gh, "default_branch", boom)
    monkeypatch.setattr(ghsetup.checks_mod, "discover", boom)
    report = ghsetup.setup(
        REPO, checks_override=["a", "", "b"], config_path="/dev/null", dry_run=True
    )
    assert report.ruleset.checks == ("a", "b")


def test_report_json_field_set():
    """The exact --json surface: SetupReport.to_dict()'s declared field set."""
    report = ghsetup.SetupReport(
        repo="o/r",
        dry_run=False,
        ruleset=ghsetup.RulesetOutcome(
            name=ghsetup.RULESET_NAME,
            existing_id=7,
            checks=("c1",),
            action="updated",
            payload={"name": ghsetup.RULESET_NAME},
        ),
        labels=(ghsetup.LabelOutcome(name="bug", action="upserted"),),
        secrets=(
            ghsetup.SecretOutcome(name="A", source="env", action="set"),
            ghsetup.SecretOutcome(
                name="X", source="env", action="failed", reason="no VAR"
            ),
        ),
    )
    payload = report.to_dict()
    assert set(payload) == {
        "repo",
        "dry_run",
        "ruleset",
        "labels",
        "secrets",
        "secrets_error",
    }
    assert set(payload["ruleset"]) == {
        "name",
        "existing_id",
        "checks",
        "action",
        "payload",
    }
    assert payload["labels"] == [{"name": "bug", "action": "upserted"}]
    assert payload["secrets"][1] == {
        "name": "X",
        "source": "env",
        "action": "failed",
        "reason": "no VAR",
    }
    # The exit contract derives from the report.
    assert report.secrets_failed == 1


# --------------------------------------------------------------------------
# The pure renderer — the frozen text surface, off the typed report
# --------------------------------------------------------------------------


def _report(**overrides) -> ghsetup.SetupReport:
    base = dict(
        repo="o/r",
        dry_run=False,
        ruleset=ghsetup.RulesetOutcome(
            name=ghsetup.RULESET_NAME,
            existing_id=None,
            checks=("c / check",),
            action="created",
            payload={"name": ghsetup.RULESET_NAME},
        ),
        labels=(ghsetup.LabelOutcome(name="bug", action="upserted"),),
        secrets=(),
        secrets_error=None,
    )
    base.update(overrides)
    return ghsetup.SetupReport(**base)


def test_format_setup_real_run():
    report = _report(
        secrets=(
            ghsetup.SecretOutcome(name="A", source="env", action="set"),
            ghsetup.SecretOutcome(
                name="B",
                source="env",
                action="skipped",
                reason="optional source absent",
            ),
            ghsetup.SecretOutcome(
                name="X", source="env", action="failed", reason="no VAR_MISSING"
            ),
        ),
    )
    assert gh_setup_verb.format_setup(report) == (
        "gh-setup: o/r\n"
        "ruleset:\n"
        "  ruleset: main-branch-protection (existing id: none)\n"
        "  checks:  c / check\n"
        "  ruleset created\n"
        "labels:\n"
        "  label bug\n"
        "secrets:\n"
        "  secret A\n"
        "  skip B (optional source absent)\n"
        "  FAIL X: no VAR_MISSING\n"
        "  1 secret(s) set, 1 skipped, 1 failed\n"
        "done."
    )


def test_format_setup_dry_run_renders_off_the_same_shape():
    report = _report(
        dry_run=True,
        ruleset=ghsetup.RulesetOutcome(
            name=ghsetup.RULESET_NAME,
            existing_id=7,
            checks=(),
            action="dry-run",
            payload={"name": ghsetup.RULESET_NAME},
        ),
        labels=(ghsetup.LabelOutcome(name="bug", action="dry-run"),),
        secrets=(ghsetup.SecretOutcome(name="A", source="env", action="dry-run"),),
    )
    assert gh_setup_verb.format_setup(report) == (
        "gh-setup: o/r (dry-run)\n"
        "ruleset:\n"
        "  ruleset: main-branch-protection (existing id: 7)\n"
        "  checks:  (none)\n"
        "  --- payload (dry-run, not sent) ---\n"
        '{\n  "name": "main-branch-protection"\n}\n'
        "labels:\n"
        "  [dry] label bug\n"
        "secrets:\n"
        "  [dry] secret A (from env)\n"
        "  1 secret(s) set, 0 skipped, 0 failed\n"
        "done."
    )


def test_format_setup_config_error_line():
    report = _report(secrets_error="no .shipit.toml at /x")
    out = gh_setup_verb.format_setup(report)
    assert "secrets:\n  no secrets applied: no .shipit.toml at /x\ndone." in out


# --------------------------------------------------------------------------
# Verb wiring smoke layer — glue only; the domain is stubbed
# --------------------------------------------------------------------------


def _ambient(monkeypatch, slug="me/shipit", path="/somewhere/shipit"):
    ctx = RootContext(
        working_dir=WorkingDir(
            path=path, repo=repo_from_slug(slug), revision=Revision()
        )
    )
    monkeypatch.setattr(gh_setup_verb, "current_root_context", lambda: ctx)
    return ctx


@pytest.fixture
def stub_setup(monkeypatch):
    """Capture the values the glue threads into the domain; return a canned report."""
    seen = {}

    def fake_setup(repo, **kwargs):
        seen["repo"] = repo
        seen.update(kwargs)
        return _report(repo=repo.slug)

    monkeypatch.setattr(gh_setup_verb, "setup", fake_setup)
    return seen


def test_run_defaults_to_ambient_repo_and_local_discovery(
    stub_setup, monkeypatch, capsys
):
    _ambient(monkeypatch)
    rc = gh_setup_verb.run(None, dry_run=True)
    assert rc == 0
    assert stub_setup["repo"].slug == "me/shipit"
    # Target IS the checkout → local workflow discovery enabled.
    assert stub_setup["local_checkout"] == "/somewhere/shipit"
    assert stub_setup["config_path"] == "/somewhere/shipit/.shipit.toml"
    assert stub_setup["dry_run"] is True
    assert capsys.readouterr().out.startswith("gh-setup: me/shipit")


def test_run_remote_target_disables_local_discovery(stub_setup, monkeypatch, capsys):
    """A target that isn't the current checkout must NOT pass the local
    toplevel to discovery — but the config default stays the ambient checkout's."""
    _ambient(monkeypatch)
    rc = gh_setup_verb.run(repo_from_slug("other/repo"), dry_run=True)
    assert rc == 0
    assert stub_setup["repo"].slug == "other/repo"
    assert stub_setup["local_checkout"] is None
    assert stub_setup["config_path"] == "/somewhere/shipit/.shipit.toml"


def test_run_failed_secret_derives_exit_1(stub_setup, monkeypatch, capsys):
    _ambient(monkeypatch)
    monkeypatch.setattr(
        gh_setup_verb,
        "setup",
        lambda repo, **kw: _report(
            secrets=(
                ghsetup.SecretOutcome(
                    name="X", source="env", action="failed", reason="no VAR"
                ),
            )
        ),
    )
    assert gh_setup_verb.run(None) == 1


def test_no_repo_and_no_checkout_is_the_uniform_refusal(monkeypatch, capsys):
    """Outside a checkout with no explicit REPO: the ONE uniform refusal through
    the error shell — `error: …` on stderr, exit 1 (the stated exit-contract
    move off the old bespoke stderr line)."""
    monkeypatch.setattr(
        gh_setup_verb, "current_root_context", lambda: RootContext(working_dir=None)
    )
    rc = gh_setup_verb.run(None, dry_run=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "not inside a repository checkout" in err


def test_empty_checks_warning_goes_to_stderr(stub_setup, monkeypatch, capsys):
    _ambient(monkeypatch)
    monkeypatch.setattr(
        gh_setup_verb,
        "setup",
        lambda repo, **kw: _report(
            ruleset=ghsetup.RulesetOutcome(
                name=ghsetup.RULESET_NAME,
                existing_id=None,
                checks=(),
                action="created",
                payload={},
            )
        ),
    )
    assert gh_setup_verb.run(None) == 0
    captured = capsys.readouterr()
    assert "no required checks discovered" in captured.err
    assert "no required checks discovered" not in captured.out


def test_cli_json_emits_the_report_dict(stub_setup, monkeypatch, capsys):
    import json as jsonlib

    from shipit import cli

    _ambient(monkeypatch)
    rc = cli.main(["gh-setup", "me/shipit", "--json", "--dry-run"])
    assert rc == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert set(payload) == {
        "repo",
        "dry_run",
        "ruleset",
        "labels",
        "secrets",
        "secrets_error",
    }
    assert payload["repo"] == "me/shipit"


def test_cli_checks_flag_parses_to_the_override_list(stub_setup, monkeypatch, capsys):
    from shipit import cli

    _ambient(monkeypatch)
    rc = cli.main(["gh-setup", "me/shipit", "--checks", "a , b,,c"])
    assert rc == 0
    assert stub_setup["checks_override"] == ["a", "b", "c"]


def test_cli_malformed_slug_is_usage_tier_exit_2(capsys):
    """The USAGE tier (ADR-0030): a REPO that is not owner/name dies at click's
    parse with a usage message and exit 2 — it never reaches the verb body."""
    from shipit import cli

    rc = cli.main(["gh-setup", "not-a-slug", "--dry-run"])
    assert rc == 2
    assert "Usage:" in capsys.readouterr().err
