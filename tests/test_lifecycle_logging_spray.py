"""LOG02-WS04: verbs + remaining subsystems narrate their lifecycle (ADR-0029).

Convention-level assertions ONLY (PRD glassbox, Testing Decisions): the key
lifecycle events exist at the conventional level and carry their required flat
fields — matched by FIELD PRESENCE, never per-message string assertions, so
wording can evolve without breaking the pin. Covered surfaces: install/reconcile
actions, gh-setup mutations, the lint orchestration summary, the session
liveness probes + pidfile lifecycle, and the verify-apps liveness verdict
(LOG03-WS03).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shipit import execrun, gh, git
from shipit.agent import backend as agent_backend
from shipit.config import SecretSource
from shipit.review import ghauth
from shipit.session import liveness
from shipit.verbs import gh_setup, install, lint, verify_apps


def _with_fields(records, level, *fields):
    """Records at ``level`` that carry ALL of ``fields`` as flat attributes."""
    return [
        r for r in records if r.levelno == level and all(hasattr(r, f) for f in fields)
    ]


# --------------------------------------------------------------------------
# install — reconcile writes, the PR milestone, and the failing boundary
# --------------------------------------------------------------------------


class _GhRecorder:
    """A do-nothing git/PR boundary so install runs against a tmp consumer."""

    def __init__(self):
        self.fail_switch = False

    def switch_create(self, branch, *, cwd):
        if self.fail_switch:
            raise execrun.ExecError(["git", "switch"], rc=1, stderr="boom")

    def add(self, paths, *, cwd):
        pass

    def commit(self, message, paths, *, cwd):
        pass

    def push(self, branch, *, cwd, remote="origin", force=False):
        pass

    def current_branch(self, *, cwd):
        return "main"

    def pr_url_for_head(self, branch, *, cwd=None):
        return None

    def pr_create(self, *, head, title, body, draft, cwd, **kw):
        return "https://github.com/acme/repo/pull/1"


@pytest.fixture
def rec(monkeypatch):
    r = _GhRecorder()
    for name in (
        "switch_create",
        "add",
        "commit",
        "push",
        "current_branch",
    ):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(install, "_shipit_version", lambda: "testhash")
    monkeypatch.setattr(
        install,
        "_activate_hooks",
        lambda root: execrun.ExecResult(
            argv=("lefthook", "install"), rc=0, stdout="", stderr="", duration_ms=1
        ),
    )
    return r


def test_install_logs_the_write_and_pr_milestones(tmp_path, rec, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.install"):
        assert install.run(str(tmp_path)) == 0
    # The reconcile milestone: the managed set landed, with the decided counts.
    written = _with_fields(
        caplog.records, logging.INFO, "root", "adds", "updates", "overrides", "seeds"
    )
    assert written and written[0].adds > 0
    # The PR milestone: the branch and its draft-PR URL, timed.
    pr = _with_fields(caplog.records, logging.INFO, "branch", "url", "duration_ms")
    assert pr and pr[0].branch == install.INSTALL_BRANCH


def test_noop_reinstall_emits_no_mutation_milestone(tmp_path, rec, caplog):
    assert install.run(str(tmp_path)) == 0
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="shipit.install"):
        assert install.run(str(tmp_path)) == 0
    # Nothing mutated, so nothing narrates at INFO — mechanics stay at DEBUG.
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_install_boundary_failure_is_an_error_with_the_exception(tmp_path, rec, caplog):
    rec.fail_switch = True
    with caplog.at_level(logging.DEBUG, logger="shipit.install"):
        assert install.run(str(tmp_path)) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and any(r.exc_info for r in errors)


# --------------------------------------------------------------------------
# gh-setup — ruleset/label/secret mutations, and the never-log-a-secret pin
# --------------------------------------------------------------------------


class _FakeGh:
    def __init__(self, existing_rulesets=None):
        self._rulesets = existing_rulesets or []

    def rest(self, path, *, method=None, body=None, paginate=False):
        if path.endswith("/rulesets") and method is None:
            return self._rulesets
        return None

    def label_create(self, repo, name, *, description, color):
        pass

    def secret_set(self, name, value, *, repo):
        pass


@pytest.fixture
def fake_gh(monkeypatch):
    fake = _FakeGh()
    monkeypatch.setattr(gh_setup.gh, "rest", fake.rest)
    monkeypatch.setattr(gh_setup.gh, "label_create", fake.label_create)
    monkeypatch.setattr(gh_setup.gh, "secret_set", fake.secret_set)
    return fake


def test_ruleset_mutation_is_logged_with_repo_bound(fake_gh, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        gh_setup.apply_ruleset("o/r", ["c1"], dry_run=False)
    recs = _with_fields(caplog.records, logging.INFO, "repo", "ruleset", "checks")
    assert recs and recs[0].repo == "o/r" and recs[0].checks == 1


def test_labels_pass_logs_its_milestone(fake_gh, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        gh_setup.ensure_labels("o/r", gh_setup.load_labels(), dry_run=False)
    recs = _with_fields(caplog.records, logging.INFO, "repo", "labels")
    assert recs and recs[0].labels > 0


def test_secret_set_is_logged_by_name_and_the_value_never_appears(
    fake_gh, monkeypatch, caplog
):
    secret_value = "shipit-test-secret-value-9f8e7d"
    monkeypatch.setenv("VAR_A", secret_value)
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        gh_setup.push_secrets(
            "o/r", [SecretSource("A", "env", "VAR_A", False)], dry_run=False
        )
    recs = _with_fields(caplog.records, logging.INFO, "repo", "secret")
    assert recs and recs[0].secret == "A"
    # The value must never reach a record — message OR any flat field.
    for r in caplog.records:
        assert secret_value not in r.getMessage()
        assert all(secret_value not in str(v) for v in r.__dict__.values())


def test_unresolvable_secret_degrades_to_warning_with_the_exception(
    fake_gh, monkeypatch, caplog
):
    monkeypatch.delenv("VAR_MISSING", raising=False)
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        gh_setup.push_secrets(
            "o/r", [SecretSource("X", "env", "VAR_MISSING", False)], dry_run=False
        )
    warnings = _with_fields(caplog.records, logging.WARNING, "repo", "secret")
    assert warnings and any(r.exc_info for r in warnings)


# --------------------------------------------------------------------------
# lint — the orchestration summary, and a launch failure at ERROR
# --------------------------------------------------------------------------


def _discover(files):
    return lambda root: list(files)


class _Tool:
    def __init__(self, codes=None):
        self.codes = codes or {}

    def __call__(self, binary, args, cwd):
        rc = self.codes.get(binary, 0)
        if isinstance(rc, execrun.ExecError):
            raise rc
        return execrun.ExecResult(
            argv=(binary, *args), rc=rc, stdout="", stderr="", duration_ms=1
        )


def test_lint_summary_carries_the_run_fields(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.lint"):
        rc = lint.run(str(tmp_path), discover=_discover(["a.py"]), run_tool=_Tool())
    assert rc == 0
    summaries = _with_fields(
        caplog.records,
        logging.INFO,
        "root",
        "mode",
        "checks",
        "failed",
        "rc",
        "duration_ms",
    )
    assert summaries and summaries[0].rc == 0 and summaries[0].checks > 0
    # A clean run carries NO failed_checks field — absent, not null-stuffed.
    assert not hasattr(summaries[0], "failed_checks")


def test_failing_lint_summary_names_the_failed_checks(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.lint"):
        rc = lint.run(
            str(tmp_path),
            discover=_discover(["a.py"]),
            run_tool=_Tool(codes={"ruff": 1}),
        )
    assert rc == 1
    summaries = _with_fields(
        caplog.records, logging.INFO, "rc", "failed", "failed_checks"
    )
    assert summaries and summaries[0].failed > 0


def test_lint_launch_failure_is_an_error_with_the_exception(tmp_path, caplog):
    boom = execrun.ExecError(
        ["markdownlint"], rc=None, cause=execrun.CAUSE_MISSING_BINARY
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.lint"):
        rc = lint.run(
            str(tmp_path),
            discover=_discover(["b.md"]),
            run_tool=_Tool(codes={"markdownlint": boom}),
        )
    assert rc == 1
    errors = _with_fields(caplog.records, logging.ERROR, "lang", "tool", "rc")
    assert errors and any(r.exc_info for r in errors)


# --------------------------------------------------------------------------
# session liveness — pidfile lifecycle at INFO, probe verdicts at DEBUG
# --------------------------------------------------------------------------


def _tree(tmp_path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_pidfile_write_and_remove_are_info_milestones(tmp_path, caplog):
    tree = _tree(tmp_path)
    record = liveness.LivenessRecord(pid=41, session_id="s-1", create_time=123.0)
    with caplog.at_level(logging.DEBUG, logger="shipit.session"):
        liveness.write_pidfile(tree, record)
    written = _with_fields(caplog.records, logging.INFO, "tree", "session", "pid")
    assert written and written[0].pid == 41

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="shipit.session"):
        liveness.remove_pidfile(tree)
    removed = _with_fields(caplog.records, logging.INFO, "tree")
    assert removed

    # Removing an already-absent pidfile mutates nothing — and records nothing.
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="shipit.session"):
        liveness.remove_pidfile(tree)
    assert not caplog.records


# --------------------------------------------------------------------------
# verify-apps — the App-liveness verdict (the report a rollout reads)
# --------------------------------------------------------------------------


def _minted(checks: str | None) -> dict:
    perms = {"pull_requests": "write"}
    if checks is not None:
        perms["checks"] = checks
    return {"token": "ghs_tok", "permissions": perms}


def _mint_live(backend, repo):
    return _minted("write")


def _mint_degraded(backend, repo):
    return _minted("read")


def _mint_not_installed(backend, repo):
    raise ghauth.ReviewAuthError("not installed")


def test_verify_apps_verdict_carries_the_run_fields(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", mint=_mint_live)
    assert rc == 0
    verdicts = _with_fields(
        caplog.records, logging.INFO, "repo", "apps", "live", "rc", "duration_ms"
    )
    assert verdicts and verdicts[0].repo == "o/r" and verdicts[0].rc == 0
    assert verdicts[0].live == verdicts[0].apps > 0
    # An all-live run carries NO not_live_apps field — absent, not null-stuffed.
    assert not hasattr(verdicts[0], "not_live_apps")
    # Per-App passes are mechanics: each probe's outcome lands at DEBUG.
    probes = _with_fields(
        caplog.records, logging.DEBUG, "repo", "agent", "app", "live", "duration_ms"
    )
    assert len(probes) == verdicts[0].apps and all(p.live for p in probes)


def test_verify_apps_failing_verdict_names_the_not_live_apps(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", mint=_mint_not_installed)
    assert rc == 1
    verdicts = _with_fields(caplog.records, logging.INFO, "rc", "not_live_apps")
    assert verdicts and verdicts[0].rc == 1 and verdicts[0].live == 0
    # The probe raising (App not installed) is the failure path: ERROR + exception.
    errors = _with_fields(caplog.records, logging.ERROR, "repo", "agent", "app")
    assert errors and all(r.exc_info for r in errors)


def test_verify_apps_degraded_permission_is_a_warning(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", agents=["codex"], mint=_mint_degraded)
    assert rc == 1
    # Reachable but missing checks:write — degraded, so WARNING, not ERROR.
    warnings = _with_fields(
        caplog.records, logging.WARNING, "repo", "agent", "app", "live"
    )
    assert warnings and warnings[0].live is False
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_verify_apps_no_repo_dead_end_is_an_error_with_the_exception(
    capsys, monkeypatch, caplog
):
    def no_repo():
        raise execrun.ExecError(["gh"], rc=1, stderr="not a repo")

    monkeypatch.setattr(verify_apps.gh, "current_repo", no_repo)
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run(None, mint=_mint_live)
    assert rc == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and any(r.exc_info for r in errors)


def test_verify_apps_printed_report_is_unchanged_by_the_spray(capsys, caplog):
    """The log records are additive: the printed report stays the verb's product."""
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        verify_apps.run("o/r", mint=_mint_live)
    results = [
        verify_apps.verify_app(agent_backend.by_funnel_agent(a), "o/r", mint=_mint_live)
        for a in verify_apps.known_agents()
    ]
    assert capsys.readouterr().out == verify_apps.format_report("o/r", results) + "\n"


def test_liveness_probe_verdict_is_recorded_at_debug(caplog):
    record = liveness.LivenessRecord(pid=41, session_id="s-1", create_time=123.0)
    with caplog.at_level(logging.DEBUG, logger="shipit.session"):
        assert liveness.is_live(record, lambda pid: None) is False
        alive = liveness.ProcessInfo(pid=41, ppid=1, create_time=123.0, argv="claude")
        assert liveness.is_live(record, lambda pid: alive) is True
    verdicts = _with_fields(
        caplog.records, logging.DEBUG, "pid", "session", "live", "rung"
    )
    assert [v.live for v in verdicts] == [False, True]
    assert verdicts[0].rung == "pid not alive"
    assert verdicts[1].rung == "pid and create-time match"
