from __future__ import annotations

from shipit import cli
from shipit.agent import backend as agent_backend
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.review import ghauth
from shipit.verbs import verify_apps


def _granted(checks: str | None) -> dict:
    perms = {"pull_requests": "write"}
    if checks is not None:
        perms["checks"] = checks
    return {"token": "ghs_tok", "permissions": perms}


def _mint_live(backend, repo):
    return _granted("write")


def _mint_missing_checks(backend, repo):
    return _granted("read")


def _mint_not_installed(backend, repo):
    raise ghauth.ReviewAuthError(
        f"The {backend.funnel_agent!r} review app is not installed on {repo}'s owner.",
        kind=ghauth.NOT_INSTALLED,
    )


def _mint_unconfigured(backend, repo):
    raise ghauth.ReviewAuthError(
        "Could not source the private key from Doppler: doppler: command not found",
        kind=ghauth.UNCONFIGURED,
    )


def _mint_api_error(backend, repo):
    raise ghauth.ReviewAuthError(
        "GitHub API GET /repos/owner/repo/installation failed (HTTP 503): down",
        kind=ghauth.API_ERROR,
        status=503,
    )


def test_app_live_when_installed_with_checks_write():
    result = verify_apps.verify_app(agent_backend.CODEX, "owner/repo", mint=_mint_live)
    assert result.status == verify_apps.LIVE
    assert result.live is True
    assert result.reason == ""
    assert result.app == "adr-codex-review"
    assert result.agent == "codex"


def test_app_not_live_when_not_installed():
    result = verify_apps.verify_app(
        agent_backend.ANTIGRAVITY, "owner/repo", mint=_mint_not_installed
    )
    assert result.status == verify_apps.NOT_LIVE
    assert result.app == "adr-agy-review"
    assert "not installed" in result.reason
    assert verify_apps.PROVISIONING_DOC in result.reason


def test_app_not_live_when_missing_checks_write():
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_missing_checks
    )
    assert result.status == verify_apps.NOT_LIVE
    assert "checks: write" in result.reason
    assert "'read'" in result.reason
    assert verify_apps.PROVISIONING_DOC in result.reason


def test_app_not_live_when_checks_permission_absent():
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=lambda b, r: _granted(None)
    )
    assert result.status == verify_apps.NOT_LIVE
    assert "checks: write" in result.reason


def test_unconfigured_auth_is_not_a_verdict_about_the_repo():
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_unconfigured
    )
    assert result.status == verify_apps.UNCONFIGURED
    assert result.live is False
    assert "NOT checked" in result.reason
    assert "not installed" not in result.reason
    assert "unknown" in result.reason


def test_probe_failure_is_undetermined():
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_api_error
    )
    assert result.status == verify_apps.UNDETERMINED
    assert "not installed" not in result.reason
    assert "unknown" in result.reason


def test_unconfigured_probe_does_not_spray_a_traceback(caplog):
    with caplog.at_level("DEBUG", logger="shipit.verifyapps"):
        verify_apps.verify_app(
            agent_backend.CODEX, "owner/repo", mint=_mint_unconfigured
        )
    records = [r for r in caplog.records if r.name == "shipit.verifyapps"]
    assert records, "the probe must still leave a durable record"
    assert all(r.exc_info is None for r in records)
    assert all(r.levelname != "ERROR" for r in records)
    assert records[-1].status == verify_apps.UNCONFIGURED


def test_run_exits_zero_when_all_apps_live(capsys):
    rc = verify_apps.run("owner/repo", mint=_mint_live)
    assert rc == 0
    out = capsys.readouterr().out
    assert "LIVE" in out
    assert "adr-codex-review" in out
    assert "adr-agy-review" in out


def test_run_exits_nonzero_when_any_app_not_live(capsys):
    rc = verify_apps.run("owner/repo", mint=_mint_not_installed)
    assert rc == verify_apps.RC_NOT_LIVE
    out = capsys.readouterr().out
    assert "NOT LIVE" in out
    assert verify_apps.PROVISIONING_DOC in out


def test_run_exits_two_when_nothing_could_be_verified(capsys):
    rc = verify_apps.run("owner/repo", mint=_mint_unconfigured)
    assert rc == verify_apps.RC_UNVERIFIED
    out = capsys.readouterr().out
    assert "UNVERIFIED" in out
    assert "NOT LIVE" not in out


def test_run_prefers_a_real_gap_over_an_unverified_sibling(capsys):

    def mixed(backend, repo):
        if backend.funnel_agent == "codex":
            return _granted("write")
        return _mint_not_installed(backend, repo)

    def gap_and_gapless(backend, repo):
        if backend.funnel_agent == "codex":
            return _mint_unconfigured(backend, repo)
        return _mint_not_installed(backend, repo)

    assert verify_apps.run("owner/repo", mint=mixed) == verify_apps.RC_NOT_LIVE
    capsys.readouterr()
    assert (
        verify_apps.run("owner/repo", mint=gap_and_gapless) == verify_apps.RC_NOT_LIVE
    )
    out = capsys.readouterr().out
    assert "verify-apps: owner/repo — NOT LIVE" in out
    assert "[UNVERIFIED] adr-codex-review" in out
    assert "[NOT LIVE] adr-agy-review" in out


def test_run_reports_unverified_when_github_answers_garbage(capsys, monkeypatch):
    monkeypatch.setattr(ghauth, "make_app_jwt", lambda backend: "signed.jwt.token")

    class _Html:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<html>we are having trouble</html>"

    monkeypatch.setattr(ghauth.urllib.request, "urlopen", lambda req, timeout: _Html())

    rc = verify_apps.run("owner/repo")
    assert rc == verify_apps.RC_UNVERIFIED
    out = capsys.readouterr().out
    assert "UNVERIFIED" in out
    assert "NOT LIVE" not in out
    assert "not installed" not in out


def test_run_exits_nonzero_when_probe_set_is_empty(capsys, monkeypatch):
    monkeypatch.setattr(verify_apps, "known_agents", list)
    rc = verify_apps.run("owner/repo", mint=_mint_live)
    assert rc == verify_apps.RC_NOT_LIVE
    assert "NOT LIVE" in capsys.readouterr().out


def test_exit_code_is_derived_from_the_printed_verdict():
    for results, expected in (
        ([verify_apps.AppLiveness("c", "a", verify_apps.LIVE)], verify_apps.RC_LIVE),
        (
            [verify_apps.AppLiveness("c", "a", verify_apps.NOT_LIVE)],
            verify_apps.RC_NOT_LIVE,
        ),
        (
            [verify_apps.AppLiveness("c", "a", verify_apps.UNCONFIGURED)],
            verify_apps.RC_UNVERIFIED,
        ),
        (
            [verify_apps.AppLiveness("c", "a", verify_apps.UNDETERMINED)],
            verify_apps.RC_UNVERIFIED,
        ),
        ([], verify_apps.RC_NOT_LIVE),
    ):
        assert verify_apps.exit_code(results) == expected
        rendered = verify_apps.format_report("owner/repo", results)
        assert verify_apps.verdict(results) in rendered


def test_run_can_narrow_to_one_agent(capsys):
    rc = verify_apps.run("owner/repo", agents=["codex"], mint=_mint_live)
    assert rc == 0
    out = capsys.readouterr().out
    assert "adr-codex-review" in out
    assert "adr-agy-review" not in out


def test_run_errors_without_a_repo_or_checkout(capsys, monkeypatch):

    def no_repo():

        raise ExecError(["gh"], rc=1, stderr="not a repo")

    monkeypatch.setattr(verify_apps.gh, "current_repo", no_repo)
    rc = verify_apps.run(None, mint=_mint_live)
    assert rc == 1
    assert "no repo given" in capsys.readouterr().err


def test_run_defaults_repo_to_current_checkout(capsys, monkeypatch):
    monkeypatch.setattr(
        verify_apps.gh, "current_repo", lambda: repo_from_slug("owner/here")
    )
    rc = verify_apps.run(None, mint=_mint_live)
    assert rc == 0
    assert "owner/here" in capsys.readouterr().out


def test_known_agents_are_the_funnel_backends():
    assert verify_apps.known_agents() == sorted(
        b.funnel_agent for b in agent_backend.funnel_backends()
    )


def test_cli_help_lists_verify_apps(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "verify-apps" in capsys.readouterr().out


def test_cli_verify_apps_help(capsys):
    rc = cli.main(["verify-apps", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "checks:write" in out.replace(" ", "")
    assert "--agent" in out


def test_cli_verify_apps_exits_with_run_code(monkeypatch):
    monkeypatch.setattr(verify_apps, "run", lambda repo, **kw: 1)
    assert cli.main(["verify-apps", "owner/repo"]) == 1
    monkeypatch.setattr(verify_apps, "run", lambda repo, **kw: 0)
    assert cli.main(["verify-apps", "owner/repo"]) == 0
