"""gh-setup through the ADR-0030 seam (CLI02-WS04).

Domain tests drive :mod:`shipit.ghsetup` typed-in/typed-out — every pass
returns an outcome value, :func:`~shipit.ghsetup.setup` one frozen
:class:`~shipit.ghsetup.SetupReport`; no capsys, no prints in the domain. The
verb layer is covered by a thin wiring smoke layer (glue: ambient identity →
values → domain → render) plus pure-renderer assertions that freeze the text
surface.
"""

import re
from typing import Any

import pytest

from shipit import config, execrun, ghsetup, redact
from shipit.config import SecretSource
from shipit.identity import Revision, WorkingDir, repo_from_slug
from shipit.verbs import gh_setup as gh_setup_verb
from shipit.verbs._context import RootContext

REPO = repo_from_slug("o/r")


@pytest.fixture(autouse=True)
def _clean_redactor_registry():
    """Secret resolution must not leak process-lifetime redactor state.

    Production deliberately retains every fetched value for the process lifetime,
    but this module resolves many synthetic values across otherwise independent
    tests.  Clear the test seam around each case so short fixtures such as an App
    id cannot redact unrelated later records (for example, timestamp digits).
    """
    redact.clear_registered_secrets()
    yield
    redact.clear_registered_secrets()


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


def test_template_omits_automatic_copilot_flag():
    """The rulesets REST endpoint rejects `automatic_copilot_code_review_enabled`
    (422 Unexpected parameter — #438), and ADR-0031/RVW01 makes the PR state
    engine the sole review requester anyway: the payload must not carry the
    GitHub-side auto-review flag at all."""
    tmpl = ghsetup.load_template()
    rule = get_rule(tmpl, "pull_request")
    assert "automatic_copilot_code_review_enabled" not in rule["parameters"]


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


def test_build_payload_zero_checks_omits_the_rule():
    """The live API rejects an empty required_status_checks array ("Expected
    at least 1 elements" — #441), so zero checks must OMIT the rule entirely,
    never send an empty set. The other rules stay untouched."""
    tmpl = ghsetup.load_template()
    body = ghsetup.build_payload(tmpl, [])
    types = [r["type"] for r in body["rules"]]
    assert "required_status_checks" not in types
    # Every other rule flows through untouched — compare against the template
    # itself so this stays green as the template's rule set evolves.
    expected = [r for r in tmpl["rules"] if r.get("type") != "required_status_checks"]
    assert body["rules"] == expected
    # The template is not mutated (deepcopy) — its rule survives for next time.
    assert get_rule(tmpl, "required_status_checks")


def test_build_payload_blank_only_checks_omit_the_rule():
    """Blank names are dropped by checks_json; all-blank input is the
    zero-checks case and must omit the rule, not emit an empty array."""
    tmpl = ghsetup.load_template()
    body = ghsetup.build_payload(tmpl, ["", ""])
    assert "required_status_checks" not in [r["type"] for r in body["rules"]]


#: Every rule type the template may carry → its documented parameter keys
#: (https://docs.github.com/rest/repos/rules#create-a-repository-ruleset).
#: This POST has 422'd twice on undocumented/invalid parameters (#438, #441);
#: this pin makes a third layer fail in unit tests, not on the live canary.
_DOCUMENTED_RULE_PARAMS = {
    "pull_request": {
        "allowed_merge_methods",
        "dismiss_stale_reviews_on_push",
        "require_code_owner_review",
        "require_last_push_approval",
        "required_approving_review_count",
        "required_review_thread_resolution",
        "required_reviewers",
    },
    "required_status_checks": {
        "do_not_enforce_on_create",
        "required_status_checks",
        "strict_required_status_checks_policy",
    },
    # Parameterless rules: only a `type` key.
    "required_linear_history": set(),
    "non_fast_forward": set(),
    "deletion": set(),
}


def test_template_rules_carry_only_documented_parameters():
    """Whole-payload audit (#441): every rule in the shipped template is a
    documented type and carries only documented parameter keys."""
    tmpl = ghsetup.load_template()
    for rule in tmpl["rules"]:
        assert rule["type"] in _DOCUMENTED_RULE_PARAMS, rule["type"]
        allowed = _DOCUMENTED_RULE_PARAMS[rule["type"]]
        params = set(rule.get("parameters", {}))
        assert params <= allowed, (
            f"{rule['type']} carries undocumented parameters: {params - allowed}"
        )
        if not allowed:
            assert set(rule) == {"type"}, f"{rule['type']} must be parameterless"


def test_built_payload_carries_only_documented_top_level_keys():
    """The POST body itself stays within the documented create-ruleset schema."""
    body = ghsetup.build_payload(ghsetup.load_template(), ["c1"])
    assert set(body) <= {
        "name",
        "target",
        "enforcement",
        "conditions",
        "rules",
        "bypass_actors",
    }
    for actor in body.get("bypass_actors", []):
        assert set(actor) <= {"actor_id", "actor_type", "bypass_mode"}
    conditions = body.get("conditions", {})
    assert set(conditions) <= {"ref_name"}
    assert set(conditions.get("ref_name", {})) <= {"include", "exclude"}


def test_build_payload_preserves_pull_request_rule():
    """Injecting required checks must not disturb the pull_request rule — it
    flows into the built payload strictly equal to the template's rule, and
    never grows the API-rejected copilot flag (#438)."""
    tmpl = ghsetup.load_template()
    body = ghsetup.build_payload(tmpl, ["app-ui / check"])
    rule = get_rule(body, "pull_request")
    assert rule == get_rule(tmpl, "pull_request")
    assert "automatic_copilot_code_review_enabled" not in rule["parameters"]


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
    """Records calls and serves canned ruleset-list / repo-info responses."""

    def __init__(self, existing_rulesets=None):
        self.calls = []
        self._rulesets = existing_rulesets or []
        self.secrets = {}
        self.labels = []
        # Pass (d) inputs: a public user-owned repo by default, so the access
        # verify is typed not-applicable and never reaches the access endpoint.
        self.repo_info = {"private": False, "owner": {"type": "User"}}
        self.access_level = "none"

    def rest(self, path, *, method=None, body=None, paginate=False):
        self.calls.append(("rest", path, method))
        if path.endswith("/rulesets") and method is None:
            return self._rulesets
        if path.endswith("/actions/permissions/access"):
            return {"access_level": self.access_level}
        if re.fullmatch(r"repos/[^/]+/[^/]+", path):
            return self.repo_info
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


def test_apply_ruleset_refusal_short_circuits_without_writing(fake_gh):
    # A refusal (#1056): auto-discovery could not name a PR workflow's checks —
    # the pass writes NOTHING (real or dry) and rides the message on the outcome.
    outcome = ghsetup.apply_ruleset(
        "o/r", ["c1"], dry_run=False, refusal="brick guard: pass --checks"
    )
    assert outcome.action == "refused"
    assert outcome.checks == ()
    assert outcome.payload == {}
    assert outcome.refusal == "brick guard: pass --checks"
    assert not any(m in ("POST", "PUT") for (_, _, m) in fake_gh.calls)
    assert outcome.to_dict()["refusal"] == "brick guard: pass --checks"


def test_setup_refusal_from_discovery_is_rc1_and_writes_no_ruleset(
    fake_gh, monkeypatch
):
    # discover refuses (a PR workflow it could not name) → the ruleset pass
    # refuses, no ruleset is written, and the run exits rc 1 (#1056).
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(
            checks=(), refusal="required-check auto-discovery could not name ..."
        ),
    )
    report = ghsetup.setup(REPO, config_path="/dev/null", dry_run=False)
    assert report.ruleset_refused
    assert report.ruleset.action == "refused"
    assert report.ruleset.refusal.startswith("required-check auto-discovery")
    # No ruleset mutation went out.
    assert not any(
        p.endswith("/rulesets") and m == "POST" for (_, p, m) in fake_gh.calls
    )
    # The refusal renders and drives a non-empty exit path.
    text = gh_setup_verb.format_setup(report)
    assert "REFUSED" in text


def test_apply_ruleset_unlistable_is_a_report_fact(monkeypatch):
    """A failed listing degrades to "assume none" — and says so on the outcome,
    so a --json consumer can tell "verified absent" from "could not list"."""
    fake = FakeGh()

    def rest(path, *, method=None, body=None, paginate=False):
        if method is None:
            raise execrun.ExecError(["gh", "api"], rc=1, stderr="HTTP 403")
        return fake.rest(path, method=method, body=body)

    monkeypatch.setattr(ghsetup.gh, "rest", rest)
    outcome = ghsetup.apply_ruleset("o/r", ["c1"], dry_run=False)
    assert outcome.action == "created"  # fell through to POST
    assert outcome.existing_id is None
    assert outcome.list_error is not None and "HTTP 403" in outcome.list_error
    assert ("rest", "repos/o/r/rulesets", "POST") in fake.calls
    assert outcome.to_dict()["list_error"] == outcome.list_error


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
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )

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


def test_setup_pushes_the_derived_requirement_set(fake_gh, monkeypatch, tmp_path):
    """The sync consumes the derivation (TOL02-WS02, story 44): required and
    sourced → pushed; declared-but-unrequired → orphan, NOT pushed."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )
    monkeypatch.setenv("VAR_A", "secret-a")
    monkeypatch.setenv("VAR_B", "secret-b")
    cfg = tmp_path / ".shipit.toml"
    cfg.write_text(
        '[artifacts.dist]\nbuild = ["python"]\nendpoints = ["gh-release", "pypi"]\n'
        "[secrets]\n"
        'RELEASE_TOKEN = { env = "VAR_A" }\n'
        'PYPI_TOKEN = { env = "VAR_B" }\n'
        'NPM_TOKEN = { env = "VAR_B" }\n',  # no npm endpoint → orphan
        encoding="utf-8",
    )

    report = ghsetup.setup(REPO, config_path=str(cfg), dry_run=False)
    assert report.secrets_error is None
    assert [(s.name, s.action) for s in report.secrets] == [
        ("RELEASE_TOKEN", "set"),
        ("PYPI_TOKEN", "set"),
        ("NPM_TOKEN", "orphan"),
    ]
    assert (report.secrets_set, report.secrets_failed, report.secrets_orphaned) == (
        2,
        0,
        1,
    )
    # Never over-provisions: the orphan is flagged, not pushed.
    assert fake_gh.secrets == {"RELEASE_TOKEN": "secret-a", "PYPI_TOKEN": "secret-b"}
    assert report.ruleset.action == "created"
    assert ("rest", "repos/o/r/rulesets", "POST") in fake_gh.calls


def test_setup_missing_source_fails_naming_the_requiring_entry(
    fake_gh, monkeypatch, tmp_path
):
    """Story 45: a derived requirement with no [secrets] source is a
    SYNC-TIME error naming the requiring registry entry — drift is caught at
    gh-setup, not at release."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )
    monkeypatch.setenv("VAR_A", "secret-a")
    cfg = tmp_path / ".shipit.toml"
    cfg.write_text(
        '[artifacts.dist]\nbuild = ["python"]\nendpoints = ["gh-release", "pypi"]\n'
        '[secrets]\nRELEASE_TOKEN = { env = "VAR_A" }\n',
        encoding="utf-8",
    )

    report = ghsetup.setup(REPO, config_path=str(cfg), dry_run=False)
    failed = [s for s in report.secrets if s.action == "failed"]
    assert [(s.name, s.source) for s in failed] == [("PYPI_TOKEN", "none")]
    assert "required by endpoint pypi (artifact dist)" in failed[0].reason
    assert report.secrets_failed == 1  # → the verb's rc 1


def test_setup_reads_reviewers_and_provisions_their_credentials(
    fake_gh, monkeypatch, tmp_path
):
    """#740 end-to-end: the [reviewers] table is the third derivation input —
    a declared funnel reviewer's sourced credential pair is pushed."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )
    monkeypatch.setenv("VAR_PEM", "pem")
    monkeypatch.setenv("VAR_ID", "42")
    # A direct caller may supply any filename; all policy tables must come from
    # that exact file rather than re-discovering a sibling `.shipit.toml`.
    cfg = tmp_path / "custom-policy.toml"
    cfg.write_text(
        "[reviewers]\ncodex = {}\n"
        "[secrets]\n"
        'CODEX_REVIEW_APP_PRIVATE_KEY = { env = "VAR_PEM" }\n'
        'CODEX_REVIEW_APP_ID = { env = "VAR_ID" }\n',
        encoding="utf-8",
    )

    report = ghsetup.setup(REPO, config_path=str(cfg), dry_run=False)
    assert report.secrets_error is None
    assert [(s.name, s.action) for s in report.secrets] == [
        ("CODEX_REVIEW_APP_PRIVATE_KEY", "set"),
        ("CODEX_REVIEW_APP_ID", "set"),
    ]
    assert fake_gh.secrets == {
        "CODEX_REVIEW_APP_PRIVATE_KEY": "pem",
        "CODEX_REVIEW_APP_ID": "42",
    }


def test_setup_declared_reviewer_with_pruned_secrets_fails_the_sync(
    fake_gh, monkeypatch, tmp_path
):
    """#740's deliberate behavior change, end-to-end: reviewers declared +
    broken/pruned [secrets] → failed outcomes → rc 1, loud at sync time."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )
    cfg = tmp_path / ".shipit.toml"
    cfg.write_text("[reviewers]\ncodex = {}\n[secrets]\n", encoding="utf-8")

    report = ghsetup.setup(REPO, config_path=str(cfg), dry_run=False)
    failed = [s for s in report.secrets if s.action == "failed"]
    assert [s.name for s in failed] == [
        "CODEX_REVIEW_APP_PRIVATE_KEY",
        "CODEX_REVIEW_APP_ID",
    ]
    assert "reviewer codex ([reviewers] declaration)" in failed[0].reason
    assert report.secrets_failed == 2  # → the verb's rc 1


def test_setup_malformed_reviewers_degrades_like_a_config_error(
    fake_gh, monkeypatch, tmp_path
):
    """A bad [reviewers] table is the same degraded-but-continuing posture as a
    malformed [secrets]: ruleset/labels applied, the failure a report fact."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )
    cfg = tmp_path / "custom-policy.toml"
    cfg.write_text("[reviewers]\nnotareviewer = {}\n", encoding="utf-8")

    report = ghsetup.setup(REPO, config_path=str(cfg), dry_run=False)
    assert report.secrets == ()
    assert report.secrets_error is not None
    assert "notareviewer" in report.secrets_error
    assert str(cfg) in report.secrets_error
    assert ".shipit.toml" not in report.secrets_error
    assert report.ruleset.action == "created"  # the earlier passes still ran


def test_sync_secrets_dry_run_reports_the_same_derivation(fake_gh, monkeypatch):
    """Dry-run previews the derived sync — would-push, missing, orphan — with
    zero resolution side effects (no doppler, no prompt, no env read)."""
    artifacts = config.load_artifacts(
        {"artifacts": {"dist": {"build": ["python"], "endpoints": ["pypi"]}}}
    )
    sources = [
        SecretSource("RELEASE_TOKEN", "env", "VAR_A", False),
        SecretSource("NPM_TOKEN", "env", "VAR_B", False),  # orphan
    ]
    outcomes = ghsetup.sync_secrets(
        "o/r", artifacts, sources, reviewers=(), dry_run=True, prompt=None
    )
    assert [(o.name, o.action) for o in outcomes] == [
        ("RELEASE_TOKEN", "dry-run"),
        ("PYPI_TOKEN", "failed"),  # missing source is a fact, dry or real
        ("NPM_TOKEN", "orphan"),
    ]
    assert fake_gh.secrets == {}


def test_sync_secrets_declared_reviewer_app_secrets_are_required_and_pushed(
    fake_gh, monkeypatch
):
    """#740 (option C): a declared funnel reviewer's credential pair rides the
    derived required set — sourced → pushed, exactly like an endpoint token."""
    monkeypatch.setenv("VAR_PEM", "pem")
    monkeypatch.setenv("VAR_ID", "42")
    sources = [
        SecretSource("CODEX_REVIEW_APP_PRIVATE_KEY", "env", "VAR_PEM", False),
        SecretSource("CODEX_REVIEW_APP_ID", "env", "VAR_ID", False),
    ]
    outcomes = ghsetup.sync_secrets(
        "o/r", (), sources, reviewers=("codex",), dry_run=False, prompt=None
    )
    assert [(o.name, o.action) for o in outcomes] == [
        ("CODEX_REVIEW_APP_PRIVATE_KEY", "set"),
        ("CODEX_REVIEW_APP_ID", "set"),
    ]
    assert fake_gh.secrets == {
        "CODEX_REVIEW_APP_PRIVATE_KEY": "pem",
        "CODEX_REVIEW_APP_ID": "42",
    }


def test_sync_secrets_undeclared_seeded_app_secrets_are_orphans(fake_gh, monkeypatch):
    """#740: the extra_required orphan exemption is gone — a seeded App pair
    whose reviewer is NOT in [reviewers] is a normal orphan: flagged, never
    resolved, never pushed."""
    monkeypatch.setenv("VAR_APP", "pem")
    (app_name, *_rest) = config.seeded_app_secrets()
    sources = [SecretSource(app_name, "env", "VAR_APP", False)]
    outcomes = ghsetup.sync_secrets(
        "o/r", (), sources, reviewers=(), dry_run=False, prompt=None
    )
    assert [(o.name, o.action) for o in outcomes] == [(app_name, "orphan")]
    assert fake_gh.secrets == {}


def test_sync_secrets_declared_reviewer_without_sources_fails_loud(fake_gh):
    """#740's deliberate behavior change: reviewers declared + pruned [secrets]
    source → the sync FAILS naming the declaring reviewer (rc 1 at gh-setup,
    not a delayed break at review-posting time)."""
    outcomes = ghsetup.sync_secrets(
        "o/r", (), [], reviewers=("agy",), dry_run=False, prompt=None
    )
    assert [(o.name, o.action) for o in outcomes] == [
        ("AGY_REVIEW_APP_PRIVATE_KEY", "failed"),
        ("AGY_REVIEW_APP_ID", "failed"),
    ]
    assert all(
        "reviewer agy ([reviewers] declaration)" in (o.reason or "") for o in outcomes
    )
    assert fake_gh.secrets == {}


def test_sync_secrets_reviewer_credential_cannot_be_optional_skipped(
    fake_gh, monkeypatch
):
    """A hand-edited `optional = true` on a declared reviewer's credential can
    no longer sync "clean" while the value is absent (#740's failure mode):
    the derivation wins over the flag — failed, not skipped."""
    monkeypatch.delenv("VAR_MISSING", raising=False)
    sources = [
        SecretSource("CODEX_REVIEW_APP_PRIVATE_KEY", "env", "VAR_MISSING", True),
        SecretSource("CODEX_REVIEW_APP_ID", "env", "VAR_MISSING", True),
    ]
    outcomes = ghsetup.sync_secrets(
        "o/r", (), sources, reviewers=("codex",), dry_run=False, prompt=None
    )
    assert [(o.name, o.action) for o in outcomes] == [
        ("CODEX_REVIEW_APP_PRIVATE_KEY", "failed"),
        ("CODEX_REVIEW_APP_ID", "failed"),
    ]
    assert fake_gh.secrets == {}


def test_sync_secrets_required_source_cannot_be_optional_skipped(fake_gh, monkeypatch):
    """A derived-REQUIRED secret marked `optional = true` cannot silently skip
    when absent (story 44 — the sync never under-provisions): the derivation
    wins over the flag, so a missing value is `failed`, not `skipped`."""
    monkeypatch.delenv("VAR_MISSING", raising=False)
    # A gh-release endpoint makes RELEASE_TOKEN required (prepare's push).
    artifacts = config.load_artifacts(
        {"artifacts": {"dist": {"build": ["python"], "endpoints": ["gh-release"]}}}
    )
    sources = [SecretSource("RELEASE_TOKEN", "env", "VAR_MISSING", True)]  # optional
    outcomes = ghsetup.sync_secrets(
        "o/r", artifacts, sources, reviewers=(), dry_run=False, prompt=None
    )
    assert [(o.name, o.action) for o in outcomes] == [("RELEASE_TOKEN", "failed")]
    assert fake_gh.secrets == {}  # not skipped, not pushed — the sync fails loud


def _signing_artifacts():
    """A minimal signing artifact map (sign = true on a darwin lane, over a
    signable archive composition)."""
    return config.load_artifacts(
        {
            "artifacts": {
                "app": {
                    "build": [{"toolchain": "rust", "package": "app-cli"}],
                    "platforms": ["darwin-arm64"],
                    "bundle": {"composition": "archive"},
                    "endpoints": ["gh-release"],
                    "sign": True,
                }
            }
        }
    )


def _env_source(name):
    return SecretSource(name, "env", f"VAR_{name}", False)


#: The names a signing repo must source besides a notary trio.
_SIGN_BASE_NAMES = ("RELEASE_TOKEN", "APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD")

_APPLE_ID_TRIO = ("APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID")
_ASC_TRIO = ("ASC_API_KEY_BASE64", "ASC_API_KEY_ID", "ASC_API_ISSUER_ID")


def test_sync_secrets_apple_id_only_trio_is_pushed_without_demanding_asc(
    fake_gh, monkeypatch
):
    """#746: the Apple-ID trio is a first-class provisioning path — a repo
    sourcing it (and no ASC key) syncs clean: pushed, the ASC trio neither
    demanded name-by-name nor flagged."""
    names = (*_SIGN_BASE_NAMES, *_APPLE_ID_TRIO)
    for name in names:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    outcomes = ghsetup.sync_secrets(
        "o/r",
        _signing_artifacts(),
        [_env_source(n) for n in names],
        reviewers=(),
        dry_run=False,
        prompt=None,
    )
    assert [(o.name, o.action) for o in outcomes] == [(n, "set") for n in names]
    assert set(fake_gh.secrets) == set(names)


def test_sync_secrets_partial_asc_beside_complete_apple_id_is_accepted(
    fake_gh, monkeypatch
):
    """#746: a partial ASC trio never poisons a complete Apple-ID trio — the
    declared partial names are still accepted (pushed), not orphaned, and no
    diagnostic fires."""
    names = (*_SIGN_BASE_NAMES, "ASC_API_KEY_ID", *_APPLE_ID_TRIO)
    for name in names:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    outcomes = ghsetup.sync_secrets(
        "o/r",
        _signing_artifacts(),
        [_env_source(n) for n in names],
        reviewers=(),
        dry_run=False,
        prompt=None,
    )
    assert [(o.name, o.action) for o in outcomes] == [(n, "set") for n in names]


def test_sync_secrets_optional_absent_trio_does_not_satisfy_notary_requirement(
    fake_gh, monkeypatch
):
    """#746: merely declaring a complete trio cannot make sync clean when
    every optional source resolves absent — satisfaction follows effective
    provisioning, not config keys."""
    for name in _SIGN_BASE_NAMES:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    for name in _APPLE_ID_TRIO:
        monkeypatch.delenv(f"VAR_{name}", raising=False)
    sources = [
        *[_env_source(name) for name in _SIGN_BASE_NAMES],
        *[SecretSource(name, "env", f"VAR_{name}", True) for name in _APPLE_ID_TRIO],
    ]

    outcomes = ghsetup.sync_secrets(
        "o/r",
        _signing_artifacts(),
        sources,
        reviewers=(),
        dry_run=False,
        prompt=None,
    )

    assert [(o.name, o.action) for o in outcomes] == [
        *((name, "set") for name in _SIGN_BASE_NAMES),
        *((name, "skipped") for name in _APPLE_ID_TRIO),
        ("notary credentials", "failed"),
    ]
    assert "Apple-ID trio (missing: APPLE_ID, APPLE_PASSWORD, APPLE_TEAM_ID)" in (
        outcomes[-1].reason or ""
    )
    assert set(fake_gh.secrets) == set(_SIGN_BASE_NAMES)


def test_sync_secrets_passwordless_p12_with_no_password_source_syncs_clean(
    fake_gh, monkeypatch
):
    """#892 on the PROVISIONING side: APPLE_CERTIFICATE_PASSWORD is empty-valid,
    so a passwordless-.p12 signing repo that declares NO source for it syncs
    clean — no `failed` missing-source outcome. Preflight already accepts the
    empty value; gh-setup now agrees, so the two authorities cannot drift."""
    names = ("RELEASE_TOKEN", "APPLE_CERTIFICATE", *_APPLE_ID_TRIO)
    for name in names:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    monkeypatch.delenv("VAR_APPLE_CERTIFICATE_PASSWORD", raising=False)
    outcomes = ghsetup.sync_secrets(
        "o/r",
        _signing_artifacts(),
        [_env_source(n) for n in names],
        reviewers=(),
        dry_run=False,
        prompt=None,
    )
    assert [(o.name, o.action) for o in outcomes] == [(n, "set") for n in names]
    assert not any(o.action == "failed" for o in outcomes)
    assert "APPLE_CERTIFICATE_PASSWORD" not in fake_gh.secrets


def test_sync_secrets_passwordless_p12_optional_absent_password_skips_not_fails(
    fake_gh, monkeypatch
):
    """#892: an OPTIONAL-and-absent APPLE_CERTIFICATE_PASSWORD source resolves
    to `skipped`, not `failed` — the empty-valid name is left optional, never
    forced non-optional like an ordinary required source, so a passwordless
    repo need not invent a non-empty dummy password to sync clean."""
    present = ("RELEASE_TOKEN", "APPLE_CERTIFICATE", *_APPLE_ID_TRIO)
    for name in present:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    monkeypatch.delenv("VAR_APPLE_CERTIFICATE_PASSWORD", raising=False)
    sources = [
        *[_env_source(n) for n in present],
        SecretSource(
            "APPLE_CERTIFICATE_PASSWORD",
            "env",
            "VAR_APPLE_CERTIFICATE_PASSWORD",
            True,  # optional
        ),
    ]
    outcomes = ghsetup.sync_secrets(
        "o/r", _signing_artifacts(), sources, reviewers=(), dry_run=False, prompt=None
    )
    by_name = {o.name: o.action for o in outcomes}
    assert by_name["APPLE_CERTIFICATE_PASSWORD"] == "skipped"
    assert not any(o.action == "failed" for o in outcomes)
    assert "APPLE_CERTIFICATE_PASSWORD" not in fake_gh.secrets


def test_sync_secrets_no_complete_notary_trio_fails_with_one_diagnostic(
    fake_gh, monkeypatch
):
    """#746: neither trio complete → ONE failed outcome for the requirement,
    its reason naming what is missing from EVERY alternative — never six
    name-by-name failures."""
    names = (*_SIGN_BASE_NAMES, "ASC_API_KEY_ID")  # one ASC name, no Apple-ID
    for name in names:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    outcomes = ghsetup.sync_secrets(
        "o/r",
        _signing_artifacts(),
        [_env_source(n) for n in names],
        reviewers=(),
        dry_run=False,
        prompt=None,
    )
    failed = [o for o in outcomes if o.action == "failed"]
    assert [o.name for o in failed] == ["notary credentials"]
    reason = failed[0].reason or ""
    assert "required by sign-mac stage (artifact app)" in reason
    assert "ASC API-key trio (missing: " in reason
    assert "Apple-ID trio (missing: " in reason
    # The sourced names still pushed — one gap never strands the others.
    assert set(fake_gh.secrets) == set(names)


def test_sync_secrets_both_trios_sourced_orphans_neither(fake_gh, monkeypatch):
    """#746: declaring both trios is legal — both pushed, no orphan flag."""
    names = (*_SIGN_BASE_NAMES, *_ASC_TRIO, *_APPLE_ID_TRIO)
    for name in names:
        monkeypatch.setenv(f"VAR_{name}", f"value-{name}")
    outcomes = ghsetup.sync_secrets(
        "o/r",
        _signing_artifacts(),
        [_env_source(n) for n in names],
        reviewers=(),
        dry_run=False,
        prompt=None,
    )
    assert all(o.action == "set" for o in outcomes)
    assert set(fake_gh.secrets) == set(names)


def test_setup_discovery_respects_local_checkout(fake_gh, monkeypatch):
    """``local_checkout`` flows straight through to checks discovery — ``None``
    (a remote target) disables reading local workflow files."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    seen = {}

    def fake_discover(target, branch, *, toplevel):
        seen["toplevel"] = toplevel
        return ghsetup.checks_mod.Discovery(checks=())

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


# --------------------------------------------------------------------------
# Pass (d) — the Actions access verify-and-warn (#739). Reads only, no PUT.
# --------------------------------------------------------------------------

_PUBLISHER_YAML = "on:\n  workflow_call:\n    inputs: {}\njobs:\n  build: {}\n"
_PR_ONLY_YAML = "on:\n  pull_request:\njobs:\n  ci: {}\n"


def _checkout(tmp_path, *files):
    """A fake local checkout carrying the given (name, text) workflow files."""
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True, exist_ok=True)
    for name, text in files:
        (wfdir / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


def _rest_fake(monkeypatch, responses):
    """Patch gh.rest with a canned path→response map; returns the call log.

    A response that is an Exception is raised; an unexpected path fails the
    test (KeyError) — the seam asserts WHICH endpoints the pass touches.
    """
    calls = []

    def rest(path, *, method=None, body=None, paginate=False):
        assert method is None, "the verify pass must never mutate"
        calls.append(path)
        result = responses[path]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ghsetup.gh, "rest", rest)
    return calls


def test_workflow_access_public_is_not_applicable_without_access_call(monkeypatch):
    """Public repo → typed not-applicable, and the access endpoint (which 422s
    on public repos) is NEVER called — nor is any workflow inspected."""
    calls = _rest_fake(
        monkeypatch, {"repos/o/r": {"private": False, "owner": {"type": "User"}}}
    )
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=None)
    assert outcome.status == "not-applicable"
    assert "public" in outcome.reason
    assert calls == ["repos/o/r"]


def test_workflow_access_private_non_publisher_is_not_applicable(monkeypatch, tmp_path):
    """A private repo with no workflow_call workflow has nothing callable —
    not-applicable, access endpoint untouched."""
    calls = _rest_fake(
        monkeypatch, {"repos/o/r": {"private": True, "owner": {"type": "User"}}}
    )
    checkout = _checkout(tmp_path, ("ci.yml", _PR_ONLY_YAML))
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=checkout)
    assert outcome.status == "not-applicable"
    assert "not a reusable-workflow publisher" in outcome.reason
    assert calls == ["repos/o/r"]


def test_workflow_access_private_publisher_none_warns_naming_user_fix(
    monkeypatch, tmp_path
):
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/actions/permissions/access": {"access_level": "none"},
        },
    )
    checkout = _checkout(tmp_path, ("wf-build.yml", _PUBLISHER_YAML))
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=checkout)
    assert outcome.status == "warn"
    assert outcome.access_level == "none"
    assert outcome.recommended_level == "user"
    assert "access_level=user" in outcome.reason
    assert "never sets" in outcome.reason


def test_workflow_access_org_owner_names_organization(monkeypatch, tmp_path):
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "Organization"}},
            "repos/o/r/actions/permissions/access": {"access_level": "none"},
        },
    )
    checkout = _checkout(tmp_path, ("wf-build.yml", _PUBLISHER_YAML))
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=checkout)
    assert outcome.status == "warn"
    assert outcome.recommended_level == "organization"
    assert "access_level=organization" in outcome.reason


def test_workflow_access_acceptable_level_is_no_warn(monkeypatch, tmp_path):
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/actions/permissions/access": {"access_level": "user"},
        },
    )
    checkout = _checkout(tmp_path, ("wf-build.yml", _PUBLISHER_YAML))
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=checkout)
    assert outcome.status == "acceptable"
    assert outcome.access_level == "user"
    assert outcome.recommended_level is None


def test_workflow_access_repo_read_failure_is_unknown_not_warn(monkeypatch):
    """An unreadable repo is `unknown` — never a verified `none` (or a clean
    not-applicable)."""
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": execrun.ExecError(
                ["gh", "api"], rc=1, stderr="HTTP 403 forbidden"
            )
        },
    )
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=None)
    assert outcome.status == "unknown"
    assert outcome.status != "warn"
    assert "could not verify Actions access" in outcome.reason
    assert "403" in outcome.reason


def test_workflow_access_access_read_failure_is_unknown(monkeypatch, tmp_path):
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/actions/permissions/access": execrun.ExecError(
                ["gh", "api"], rc=1, stderr="HTTP 401"
            ),
        },
    )
    checkout = _checkout(tmp_path, ("wf-build.yml", _PUBLISHER_YAML))
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=checkout)
    assert outcome.status == "unknown"
    assert outcome.access_level is None


def test_workflow_access_malformed_access_payload_is_unknown(monkeypatch, tmp_path):
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/actions/permissions/access": {"nope": True},
        },
    )
    checkout = _checkout(tmp_path, ("wf-build.yml", _PUBLISHER_YAML))
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=checkout)
    assert outcome.status == "unknown"
    assert "access_level" in outcome.reason


def test_workflow_access_remote_repo_inspects_via_contents_api(monkeypatch):
    """An explicitly named remote target (no local checkout) detects the
    publisher through the contents API and still warns on `none`."""
    import base64

    encoded = base64.b64encode(_PUBLISHER_YAML.encode()).decode()
    calls = _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/contents/.github/workflows": [
                {"name": "wf-build.yml"},
                {"name": "README.md"},
            ],
            "repos/o/r/contents/.github/workflows/wf-build.yml": {"content": encoded},
            "repos/o/r/actions/permissions/access": {"access_level": "none"},
        },
    )
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=None)
    assert outcome.status == "warn"
    assert outcome.recommended_level == "user"
    # The non-workflow file is never fetched.
    assert "repos/o/r/contents/.github/workflows/README.md" not in calls


def test_workflow_access_remote_missing_workflows_dir_is_not_applicable(monkeypatch):
    """HTTP 404 on the contents listing means no workflows directory — a
    verified non-publisher, not an inspection failure."""
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/contents/.github/workflows": execrun.ExecError(
                ["gh", "api"], rc=1, stderr="gh: Not Found (HTTP 404)"
            ),
        },
    )
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=None)
    assert outcome.status == "not-applicable"
    assert "not a reusable-workflow publisher" in outcome.reason


@pytest.mark.parametrize("listing", [None, {"name": "wf-build.yml"}])
def test_workflow_access_remote_malformed_listing_is_unknown(monkeypatch, listing):
    """A malformed contents response is an inspection failure, never proof
    that the target has no reusable workflows."""
    _rest_fake(
        monkeypatch,
        {
            "repos/o/r": {"private": True, "owner": {"type": "User"}},
            "repos/o/r/contents/.github/workflows": listing,
        },
    )
    outcome = ghsetup.verify_workflow_access("o/r", local_checkout=None)
    assert outcome.status == "unknown"
    assert "expected a list" in outcome.reason


def test_setup_report_carries_the_workflow_access_outcome(fake_gh, monkeypatch):
    """setup() threads pass (d) into the report; the fake's default public
    repo is typed not-applicable — on the dry run too (the pass reads only)."""
    monkeypatch.setattr(ghsetup.gh, "default_branch", lambda repo: "main")
    monkeypatch.setattr(
        ghsetup.checks_mod,
        "discover",
        lambda *a, **k: ghsetup.checks_mod.Discovery(checks=("c / check",)),
    )
    report = ghsetup.setup(REPO, config_path="/dev/null", dry_run=True)
    assert report.workflow_access.status == "not-applicable"
    assert "public" in report.workflow_access.reason


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
        workflow_access=ghsetup.WorkflowAccessOutcome(
            status="warn",
            reason="access level none",
            access_level="none",
            recommended_level="user",
        ),
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
        "workflow_access",
        "secrets",
        "secrets_error",
    }
    assert payload["workflow_access"] == {
        "status": "warn",
        "reason": "access level none",
        "access_level": "none",
        "recommended_level": "user",
    }
    assert set(payload["ruleset"]) == {
        "name",
        "existing_id",
        "checks",
        "action",
        "payload",
        "list_error",
        "refusal",
    }
    # A clean listing is the explicit null, not an absent key.
    assert payload["ruleset"]["list_error"] is None
    # No refusal on a normal run (#1056) — the explicit null, not an absent key.
    assert payload["ruleset"]["refusal"] is None
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
        workflow_access=ghsetup.WorkflowAccessOutcome(
            status="not-applicable",
            reason="repository is public — its reusable workflows are "
            "callable by any repo (ADR-0053)",
        ),
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
        "workflow access:\n"
        "  not applicable: repository is public — its reusable workflows are "
        "callable by any repo (ADR-0053)\n"
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
        "workflow access:\n"
        "  not applicable: repository is public — its reusable workflows are "
        "callable by any repo (ADR-0053)\n"
        "secrets:\n"
        "  [dry] secret A (from env)\n"
        "  1 secret(s) set, 0 skipped, 0 failed\n"
        "done."
    )


def test_format_setup_list_error_warning_line():
    """The degraded listing renders a warning ahead of the ruleset line —
    absent entirely on a clean run (the frozen text surface is unchanged)."""
    report = _report(
        ruleset=ghsetup.RulesetOutcome(
            name=ghsetup.RULESET_NAME,
            existing_id=None,
            checks=("c / check",),
            action="created",
            payload={"name": ghsetup.RULESET_NAME},
            list_error="gh api failed (exit, rc=1, 5ms): HTTP 403",
        ),
    )
    out = gh_setup_verb.format_setup(report)
    assert (
        "ruleset:\n"
        "  warning: could not list rulesets — assumed none exists"
        " (gh api failed (exit, rc=1, 5ms): HTTP 403)\n"
        "  ruleset: main-branch-protection (existing id: none)\n"
    ) in out


def test_format_setup_config_error_line():
    report = _report(secrets_error="no .shipit.toml at /x")
    out = gh_setup_verb.format_setup(report)
    assert "secrets:\n  no secrets applied: no .shipit.toml at /x\ndone." in out


def test_format_setup_workflow_access_warn_line():
    report = _report(
        workflow_access=ghsetup.WorkflowAccessOutcome(
            status="warn",
            reason="private reusable-workflow publisher with Actions access "
            "level 'none' — fix: gh api …",
            access_level="none",
            recommended_level="user",
        ),
    )
    out = gh_setup_verb.format_setup(report)
    assert (
        "workflow access:\n"
        "  WARN private reusable-workflow publisher with Actions access "
        "level 'none' — fix: gh api …\n"
    ) in out


def test_format_setup_workflow_access_acceptable_line():
    report = _report(
        workflow_access=ghsetup.WorkflowAccessOutcome(
            status="acceptable",
            reason="Actions access level is 'user'",
            access_level="user",
        ),
    )
    out = gh_setup_verb.format_setup(report)
    assert "workflow access:\n  access level: user (acceptable)\n" in out


def test_format_setup_workflow_access_unknown_line():
    """The inspection failure renders as a warning, NOT as the WARN verdict —
    "could not look" stays distinct from "looked and it's none"."""
    report = _report(
        workflow_access=ghsetup.WorkflowAccessOutcome(
            status="unknown",
            reason="could not verify Actions access: HTTP 403",
        ),
    )
    out = gh_setup_verb.format_setup(report)
    assert (
        "workflow access:\n  warning: could not verify Actions access: HTTP 403\n"
    ) in out
    assert "WARN " not in out


def test_format_setup_rejects_unknown_workflow_access_status():
    report = _report(
        workflow_access=ghsetup.WorkflowAccessOutcome(
            status="typo",
            reason="must not be rendered as not applicable",
        ),
    )
    with pytest.raises(ValueError, match="unknown workflow access status: 'typo'"):
        gh_setup_verb.format_setup(report)


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


def test_run_ruleset_refusal_derives_exit_1_and_errors_on_stderr(
    stub_setup, monkeypatch, capsys
):
    # A #1056 refusal: the run exits rc 1 and prints the actionable error on
    # stderr (not the plain no-checks nudge).
    _ambient(monkeypatch)
    monkeypatch.setattr(
        gh_setup_verb,
        "setup",
        lambda repo, **kw: _report(
            ruleset=ghsetup.RulesetOutcome(
                name=ghsetup.RULESET_NAME,
                existing_id=None,
                checks=(),
                action="refused",
                payload={},
                refusal="required-check auto-discovery could not name every PR "
                "workflow's checks; re-run with explicit --checks",
            )
        ),
    )
    assert gh_setup_verb.run(None) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "--checks" in err


def test_format_setup_refused_renders_breakdown_and_continues(monkeypatch):
    # The renderer shows the REFUSED block plus the remaining passes.
    report = _report(
        ruleset=ghsetup.RulesetOutcome(
            name=ghsetup.RULESET_NAME,
            existing_id=None,
            checks=(),
            action="refused",
            payload={},
            refusal="could not name checks\n  ci.yml: certain [(none)]",
        )
    )
    out = gh_setup_verb.format_setup(report)
    assert "REFUSED — ruleset NOT written" in out
    assert "ci.yml: certain [(none)]" in out
    # The later passes still render.
    assert "labels:" in out
    assert out.rstrip().endswith("done.")


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
    assert "no required checks found" in captured.err
    assert "no required checks found" not in captured.out


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
        "workflow_access",
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


# --------------------------------------------------------------------------
# Flow events (#434) — ghsetup.started/completed/failed
# --------------------------------------------------------------------------


def _event_names(caplog):
    from shipit import events as ev

    return [getattr(r, ev.EXTRA_KEY, None) for r in caplog.records]


def test_run_emits_started_and_completed_events(
    stub_setup, monkeypatch, capsys, caplog
):
    import logging as _logging

    _ambient(monkeypatch)
    with caplog.at_level(_logging.INFO, logger="shipit.ghsetup"):
        rc = gh_setup_verb.run(None, dry_run=True)
    assert rc == 0
    names = _event_names(caplog)
    assert "ghsetup.started" in names
    assert "ghsetup.completed" in names
    assert "ghsetup.failed" not in names


def test_failed_run_emits_the_failed_event(monkeypatch, capsys, caplog):
    import logging as _logging

    from shipit.execrun import ExecError

    _ambient(monkeypatch)

    def boom(repo, **kw):
        raise ExecError(["gh", "api"], rc=1, stderr="broken gh")

    monkeypatch.setattr(gh_setup_verb, "setup", boom)
    with caplog.at_level(_logging.INFO, logger="shipit.ghsetup"):
        rc = gh_setup_verb.run(None)
    assert rc == 1  # the error shell still renders `error: …` + exit 1
    from shipit import events as ev

    failed = [
        r for r in caplog.records if getattr(r, ev.EXTRA_KEY, None) == "ghsetup.failed"
    ]
    assert len(failed) == 1
    assert failed[0].step == "setup (ruleset/labels/access/secrets)"
    names = _event_names(caplog)
    assert "ghsetup.completed" not in names


def test_secrets_failure_is_completed_not_failed(
    stub_setup, monkeypatch, capsys, caplog
):
    # A run that FINISHED with failed secrets is a completed run (rc 1 via the
    # report); `ghsetup.failed` is reserved for a run that could not finish.
    import logging as _logging

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
    with caplog.at_level(_logging.INFO, logger="shipit.ghsetup"):
        rc = gh_setup_verb.run(None)
    assert rc == 1
    names = _event_names(caplog)
    assert "ghsetup.completed" in names
    assert "ghsetup.failed" not in names


# --------------------------------------------------------------------------
# The truly stock consumer (#449 item 8): no .shipit.toml at all — gh-setup
# still runs its passes; the missing config is a report fact, never a crash.
# --------------------------------------------------------------------------


def test_setup_on_a_stock_checkout_without_config(fake_gh, tmp_path):
    stock = tmp_path / "stock"
    stock.mkdir()
    report = ghsetup.setup(
        repo_from_slug("acme/stock"),
        checks_override=["c / check"],
        local_checkout=str(stock),
        config_path=str(stock / ".shipit.toml"),
        dry_run=False,
    )
    # Ruleset + labels applied; secrets degraded to the report fact.
    assert report.ruleset.action in ("created", "updated")
    assert report.labels
    assert report.secrets == ()
    assert report.secrets_error is not None
    assert ".shipit.toml" in report.secrets_error
    assert report.secrets_failed == 0  # degraded config is not a failed secret
