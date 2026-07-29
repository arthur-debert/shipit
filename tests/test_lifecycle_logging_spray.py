from __future__ import annotations

import logging

import pytest

from shipit import execrun, gh, ghsetup, git
from shipit.agent import backend as agent_backend
from shipit.config import SecretSource
from shipit.install import apply as install_apply
from shipit.review import ghauth
from shipit.verbs import install, lint, verify_apps


def _with_fields(records, level, *fields):
    return [
        r for r in records if r.levelno == level and all(hasattr(r, f) for f in fields)
    ]


class _GhRecorder:
    def __init__(self):
        self.fail_switch = False

    def default_branch(self, *, cwd, remote="origin"):
        return "main"

    def fetch(self, *, cwd, remote="origin"):
        pass

    def switch_create(self, branch, *, cwd):
        if self.fail_switch:
            raise execrun.ExecError(["git", "switch"], rc=1, stderr="boom")

    def reset_soft(self, ref, *, cwd):
        pass

    def read_tree(self, ref, *, cwd, index_file):
        pass

    def add(self, paths, *, cwd, index_file=None):
        pass

    def rm_cached(self, paths, *, cwd, index_file=None):
        pass

    def staged_paths(self, paths, *, cwd, index_file=None):
        return sorted(paths)

    def reset_index(self, *, cwd):
        pass

    def commit(self, message, paths, *, cwd, no_verify=False):
        pass

    def commit_all(self, message, *, cwd, no_verify=False, index_file=None):
        pass

    def push(self, branch, *, cwd, remote="origin", force=False, no_verify=False):
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
        "read_tree",
        "add",
        "rm_cached",
        "staged_paths",
        "reset_index",
        "commit",
        "commit_all",
        "push",
        "current_branch",
        "default_branch",
        "fetch",
        "reset_soft",
    ):
        monkeypatch.setattr(git, name, getattr(r, name))
    for name in ("pr_url_for_head", "pr_create"):
        monkeypatch.setattr(gh, name, getattr(r, name))
    monkeypatch.setattr(install_apply, "_shipit_version", lambda: "testhash")
    from shipit.install import selfcert as _selfcert

    monkeypatch.setattr(
        _selfcert,
        "certify",
        lambda plan, root, **kw: _selfcert.CertReport(
            checks=(_selfcert.CertCheck(name="stub", ok=True),)
        ),
    )
    monkeypatch.setattr(_selfcert, "consumer_debt", lambda root, **kw: None)
    monkeypatch.setattr(
        install_apply,
        "_activate_hooks",
        lambda root: execrun.ExecResult(
            argv=("lefthook", "install"), rc=0, stdout="", stderr="", duration_ms=1
        ),
    )
    return r


def test_install_logs_the_write_and_pr_milestones(tmp_path, rec, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.install"):
        assert install.run(str(tmp_path), pr=True) == 0
    written = _with_fields(
        caplog.records, logging.INFO, "root", "adds", "updates", "overrides", "seeds"
    )
    assert written and written[0].adds > 0
    pr = _with_fields(caplog.records, logging.INFO, "branch", "url", "duration_ms")
    assert pr and pr[0].branch == install_apply.INSTALL_BRANCH


def test_noop_reinstall_emits_no_mutation_milestone(tmp_path, rec, caplog):
    assert install.run(str(tmp_path)) == 0
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="shipit.install"):
        assert install.run(str(tmp_path)) == 0
    from shipit.events import EXTRA_KEY

    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.INFO
        and r.name == "shipit.install"
        and getattr(r, EXTRA_KEY, None) is None
    ]


def test_install_boundary_failure_is_an_error_with_the_exception(tmp_path, rec, caplog):
    rec.fail_switch = True
    with caplog.at_level(logging.DEBUG, logger="shipit.install"):
        assert install.run(str(tmp_path), pr=True) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and any(r.exc_info for r in errors)


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
    monkeypatch.setattr(ghsetup.gh, "rest", fake.rest)
    monkeypatch.setattr(ghsetup.gh, "label_create", fake.label_create)
    monkeypatch.setattr(ghsetup.gh, "secret_set", fake.secret_set)
    return fake


def test_ruleset_mutation_is_logged_with_repo_bound(fake_gh, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        ghsetup.apply_ruleset("o/r", ["c1"], dry_run=False)
    recs = _with_fields(caplog.records, logging.INFO, "repo", "ruleset", "checks")
    assert recs and recs[0].repo == "o/r" and recs[0].checks == 1


def test_labels_pass_logs_its_milestone(fake_gh, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        ghsetup.ensure_labels("o/r", ghsetup.load_labels(), dry_run=False)
    recs = _with_fields(caplog.records, logging.INFO, "repo", "labels")
    assert recs and recs[0].labels > 0


def test_secret_set_is_logged_by_name_and_the_value_never_appears(
    fake_gh, monkeypatch, caplog
):
    secret_value = "shipit-test-secret-value-9f8e7d"
    monkeypatch.setenv("VAR_A", secret_value)
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        ghsetup.push_secrets(
            "o/r", [SecretSource("A", "env", "VAR_A", False)], dry_run=False
        )
    recs = _with_fields(caplog.records, logging.INFO, "repo", "secret")
    assert recs and recs[0].secret == "A"
    for r in caplog.records:
        assert secret_value not in r.getMessage()
        assert all(secret_value not in str(v) for v in r.__dict__.values())


def test_unresolvable_secret_degrades_to_warning_with_the_exception(
    fake_gh, monkeypatch, caplog
):
    monkeypatch.delenv("VAR_MISSING", raising=False)
    with caplog.at_level(logging.DEBUG, logger="shipit.ghsetup"):
        ghsetup.push_secrets(
            "o/r", [SecretSource("X", "env", "VAR_MISSING", False)], dry_run=False
        )
    warnings = _with_fields(caplog.records, logging.WARNING, "repo", "secret")
    assert warnings and any(r.exc_info for r in warnings)


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
    raise ghauth.ReviewAuthError("not installed", kind=ghauth.NOT_INSTALLED)


def _mint_unconfigured(backend, repo):
    raise ghauth.ReviewAuthError("no doppler here", kind=ghauth.UNCONFIGURED)


def test_verify_apps_verdict_carries_the_run_fields(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", mint=_mint_live)
    assert rc == 0
    verdicts = _with_fields(
        caplog.records,
        logging.INFO,
        "repo",
        "apps",
        "live",
        "verdict",
        "rc",
        "duration_ms",
    )
    assert verdicts and verdicts[0].repo == "o/r" and verdicts[0].rc == 0
    assert verdicts[0].apps > 0 and verdicts[0].live == verdicts[0].apps
    assert verdicts[0].verdict == verify_apps.VERDICT_LIVE
    assert not hasattr(verdicts[0], "not_live_apps")
    assert not hasattr(verdicts[0], "unverified_apps")
    probes = _with_fields(
        caplog.records, logging.DEBUG, "repo", "agent", "app", "status", "duration_ms"
    )
    assert len(probes) == verdicts[0].apps
    assert all(p.status == verify_apps.LIVE for p in probes)


def test_verify_apps_failing_verdict_names_the_not_live_apps(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", mint=_mint_not_installed)
    assert rc == verify_apps.RC_NOT_LIVE
    verdicts = _with_fields(caplog.records, logging.INFO, "rc", "not_live_apps")
    assert verdicts and verdicts[0].rc == 1 and verdicts[0].live == 0
    assert verdicts[0].verdict == verify_apps.VERDICT_NOT_LIVE
    errors = _with_fields(
        caplog.records, logging.ERROR, "repo", "agent", "app", "status", "duration_ms"
    )
    assert errors and all(r.exc_info for r in errors)
    assert all(r.status == verify_apps.NOT_LIVE for r in errors)


def test_verify_apps_unverified_run_is_recorded_apart_from_a_gap(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", mint=_mint_unconfigured)
    assert rc == verify_apps.RC_UNVERIFIED
    verdicts = _with_fields(caplog.records, logging.INFO, "rc", "unverified_apps")
    assert verdicts and verdicts[0].verdict == verify_apps.VERDICT_UNVERIFIED
    assert verdicts[0].live == 0
    assert not hasattr(verdicts[0], "not_live_apps")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    warnings = _with_fields(
        caplog.records, logging.WARNING, "repo", "agent", "app", "status"
    )
    assert warnings and all(
        r.status == verify_apps.UNCONFIGURED and r.exc_info is None for r in warnings
    )


def test_verify_apps_degraded_permission_is_a_warning(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        rc = verify_apps.run("o/r", agents=["codex"], mint=_mint_degraded)
    assert rc == verify_apps.RC_NOT_LIVE
    warnings = _with_fields(
        caplog.records, logging.WARNING, "repo", "agent", "app", "status", "duration_ms"
    )
    assert warnings and warnings[0].status == verify_apps.NOT_LIVE
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
    with caplog.at_level(logging.DEBUG, logger="shipit.verifyapps"):
        verify_apps.run("o/r", mint=_mint_live)
    results = [
        verify_apps.verify_app(agent_backend.by_funnel_agent(a), "o/r", mint=_mint_live)
        for a in verify_apps.known_agents()
    ]
    assert capsys.readouterr().out == verify_apps.format_report("o/r", results) + "\n"
