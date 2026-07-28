"""Tests for `shipit.verbs.verify_apps` — per-consumer App-liveness verification.

The verify logic mints the reviewer App's installation token (the App-auth path,
`ghauth.installation_auth`) and asserts the granted `permissions` carry
`checks: write`. These tests fake that mint seam (no Doppler, no network, no PyJWT)
to cover the four situations the verb must keep apart (#969):

  * App installed + `checks: write` present               -> LIVE (pass);
  * App not installed (mint kind NOT_INSTALLED)           -> NOT LIVE (instruct);
  * installed but missing `checks: write`                 -> NOT LIVE (instruct);
  * no App credentials HERE (mint kind UNCONFIGURED)      -> UNVERIFIED, rc 2; and
  * the probe itself failed (mint kind API_ERROR)         -> UNVERIFIED, rc 2.

The last two are the regression this file exists to hold: a consumer repo whose
operator has no App credentials used to be told its Apps were NOT LIVE — a claim
about the repo drawn from a fact about the machine.
"""

from __future__ import annotations

from shipit import cli
from shipit.agent import backend as agent_backend
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.review import ghauth
from shipit.verbs import verify_apps


def _granted(checks: str | None) -> dict:
    """A fake installation-auth response whose token carries `checks=<checks>`."""
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
    """The #969 shape: this machine cannot produce App credentials at all."""
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


# --------------------------------------------------------------------------
# verify_app — the per-App probe
# --------------------------------------------------------------------------


def test_app_live_when_installed_with_checks_write():
    """Installed + token carries `checks: write` -> live, no instruct reason."""
    result = verify_apps.verify_app(agent_backend.CODEX, "owner/repo", mint=_mint_live)
    assert result.status == verify_apps.LIVE
    assert result.live is True
    assert result.reason == ""
    assert result.app == "adr-codex-review"
    assert result.agent == "codex"


def test_app_not_live_when_not_installed():
    """A NOT_INSTALLED mint failure -> not live, instruct to install."""
    result = verify_apps.verify_app(
        agent_backend.ANTIGRAVITY, "owner/repo", mint=_mint_not_installed
    )
    assert result.status == verify_apps.NOT_LIVE
    assert result.app == "adr-agy-review"
    assert "not installed" in result.reason
    assert verify_apps.PROVISIONING_DOC in result.reason


def test_app_not_live_when_missing_checks_write():
    """Installed but the token lacks `checks: write` -> not live, instruct to consent."""
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_missing_checks
    )
    assert result.status == verify_apps.NOT_LIVE
    assert "checks: write" in result.reason
    # Names the OBSERVED scope so a human sees what's actually granted.
    assert "'read'" in result.reason
    assert verify_apps.PROVISIONING_DOC in result.reason


def test_app_not_live_when_checks_permission_absent():
    """No `checks` key at all (only the original scopes) -> not live, instruct."""
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=lambda b, r: _granted(None)
    )
    assert result.status == verify_apps.NOT_LIVE
    assert "checks: write" in result.reason


def test_unconfigured_auth_is_not_a_verdict_about_the_repo():
    """#969: no App credentials HERE must never read as "not installed THERE".

    The reason must say the App was not checked, must not claim it is missing, and
    must point at the local fix — the operator's next move is to configure
    credentials, not to go install an App that may already be live.
    """
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_unconfigured
    )
    assert result.status == verify_apps.UNCONFIGURED
    assert result.live is False
    assert "NOT checked" in result.reason
    assert "not installed" not in result.reason
    assert "unknown" in result.reason


def test_probe_failure_is_undetermined():
    """A GitHub-side error establishes nothing — never a not-live verdict."""
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_api_error
    )
    assert result.status == verify_apps.UNDETERMINED
    assert "not installed" not in result.reason
    assert "unknown" in result.reason


def test_unconfigured_probe_does_not_spray_a_traceback(caplog):
    """An expected local-credential gap logs the FACT, not a stack dump.

    It is the same operator-actionable failure the reviewer adapter already
    refuses to spray; verify-apps used to `logger.error(..., exc_info=True)` on it,
    so a consumer running the verb without Doppler read two tracebacks before
    reaching the (wrong) verdict.
    """
    with caplog.at_level("DEBUG", logger="shipit.verifyapps"):
        verify_apps.verify_app(
            agent_backend.CODEX, "owner/repo", mint=_mint_unconfigured
        )
    records = [r for r in caplog.records if r.name == "shipit.verifyapps"]
    assert records, "the probe must still leave a durable record"
    assert all(r.exc_info is None for r in records)
    assert all(r.levelname != "ERROR" for r in records)
    assert records[-1].status == verify_apps.UNCONFIGURED


# --------------------------------------------------------------------------
# run — the multi-App check + exit code
# --------------------------------------------------------------------------


def test_run_exits_zero_when_all_apps_live(capsys):
    """All probed Apps live -> exit 0, and the report reads LIVE."""
    rc = verify_apps.run("owner/repo", mint=_mint_live)
    assert rc == 0
    out = capsys.readouterr().out
    assert "LIVE" in out
    # Every known App reviewer is probed by default.
    assert "adr-codex-review" in out
    assert "adr-agy-review" in out


def test_run_exits_nonzero_when_any_app_not_live(capsys):
    """One not-live App fails the whole check -> exit 1 with an instruct line."""
    rc = verify_apps.run("owner/repo", mint=_mint_not_installed)
    assert rc == verify_apps.RC_NOT_LIVE
    out = capsys.readouterr().out
    assert "NOT LIVE" in out
    assert verify_apps.PROVISIONING_DOC in out


def test_run_exits_two_when_nothing_could_be_verified(capsys):
    """#969's headline: unverified is its OWN exit code, distinct from a gap.

    A rollout branching on `verify-apps` must be able to tell "go install the App"
    (1) from "this machine cannot check" (2) without parsing prose, and the report
    must not print NOT LIVE for it.
    """
    rc = verify_apps.run("owner/repo", mint=_mint_unconfigured)
    assert rc == verify_apps.RC_UNVERIFIED
    out = capsys.readouterr().out
    assert "UNVERIFIED" in out
    assert "NOT LIVE" not in out


def test_run_prefers_a_real_gap_over_an_unverified_sibling(capsys):
    """A definite NOT LIVE outranks an UNVERIFIED sibling — it is the actionable
    finding — but the unverified line still reads as itself."""

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


def test_run_exits_nonzero_when_probe_set_is_empty(capsys, monkeypatch):
    """An empty probe set must FAIL the check, not pass via `all([])` being True.

    The exit code and the printed verdict must agree: with nothing verified,
    `format_report` renders NOT LIVE, so `run` must also return non-zero.
    """
    monkeypatch.setattr(verify_apps, "known_agents", list)
    rc = verify_apps.run("owner/repo", mint=_mint_live)
    assert rc == verify_apps.RC_NOT_LIVE
    assert "NOT LIVE" in capsys.readouterr().out


def test_exit_code_is_derived_from_the_printed_verdict():
    """The code and the word come from ONE decision, so they cannot drift."""
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
    """`agents=[...]` probes only the named App(s)."""
    rc = verify_apps.run("owner/repo", agents=["codex"], mint=_mint_live)
    assert rc == 0
    out = capsys.readouterr().out
    assert "adr-codex-review" in out
    assert "adr-agy-review" not in out


def test_run_errors_without_a_repo_or_checkout(capsys, monkeypatch):
    """No repo arg and not in a checkout -> exit 1 with a clear message."""

    def no_repo():

        raise ExecError(["gh"], rc=1, stderr="not a repo")

    monkeypatch.setattr(verify_apps.gh, "current_repo", no_repo)
    rc = verify_apps.run(None, mint=_mint_live)
    assert rc == 1
    assert "no repo given" in capsys.readouterr().err


def test_run_defaults_repo_to_current_checkout(capsys, monkeypatch):
    """Omitted repo resolves from the current checkout (like gh-setup / logs)."""
    monkeypatch.setattr(
        verify_apps.gh, "current_repo", lambda: repo_from_slug("owner/here")
    )
    rc = verify_apps.run(None, mint=_mint_live)
    assert rc == 0
    assert "owner/here" in capsys.readouterr().out


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_known_agents_are_the_funnel_backends():
    """The probed set is exactly the registry's funnel App reviewers (ADR-0025)."""
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
    """The command threads `run`'s exit code through SystemExit."""
    monkeypatch.setattr(verify_apps, "run", lambda repo, **kw: 1)
    assert cli.main(["verify-apps", "owner/repo"]) == 1
    monkeypatch.setattr(verify_apps, "run", lambda repo, **kw: 0)
    assert cli.main(["verify-apps", "owner/repo"]) == 0
