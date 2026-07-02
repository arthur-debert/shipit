"""Tests for `shipit.verbs.verify_apps` — per-consumer App-liveness verification.

The verify logic mints the reviewer App's installation token (the App-auth path,
`ghauth.installation_auth`) and asserts the granted `permissions` carry
`checks: write`. These tests fake that mint seam (no Doppler, no network, no PyJWT)
to cover the three liveness shapes the rollout blocks on:

  * App installed + `checks: write` present              -> LIVE (pass);
  * App not installed (the mint `ReviewAuthError`)        -> NOT LIVE (instruct);
  * installed but missing `checks: write`                 -> NOT LIVE (instruct).
"""

from __future__ import annotations

from shipit import cli
from shipit.agent import backend as agent_backend
from shipit.review import ghauth
from shipit.verbs import verify_apps
from shipit.execrun import ExecError


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
        f"The {backend.funnel_agent!r} review app is not installed on {repo}'s owner."
    )


# --------------------------------------------------------------------------
# verify_app — the per-App probe
# --------------------------------------------------------------------------


def test_app_live_when_installed_with_checks_write():
    """Installed + token carries `checks: write` -> live, no instruct reason."""
    result = verify_apps.verify_app(agent_backend.CODEX, "owner/repo", mint=_mint_live)
    assert result.live is True
    assert result.reason == ""
    assert result.app == "adr-codex-review"
    assert result.agent == "codex"


def test_app_not_live_when_not_installed():
    """A mint ReviewAuthError (App not installed) -> not live, instruct to install."""
    result = verify_apps.verify_app(
        agent_backend.ANTIGRAVITY, "owner/repo", mint=_mint_not_installed
    )
    assert result.live is False
    assert result.app == "adr-agy-review"
    assert "not installed" in result.reason
    assert verify_apps.PROVISIONING_DOC in result.reason


def test_app_not_live_when_missing_checks_write():
    """Installed but the token lacks `checks: write` -> not live, instruct to consent."""
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=_mint_missing_checks
    )
    assert result.live is False
    assert "checks: write" in result.reason
    # Names the OBSERVED scope so a human sees what's actually granted.
    assert "'read'" in result.reason
    assert verify_apps.PROVISIONING_DOC in result.reason


def test_app_not_live_when_checks_permission_absent():
    """No `checks` key at all (only the original scopes) -> not live, instruct."""
    result = verify_apps.verify_app(
        agent_backend.CODEX, "owner/repo", mint=lambda b, r: _granted(None)
    )
    assert result.live is False
    assert "checks: write" in result.reason


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
    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT LIVE" in out
    assert verify_apps.PROVISIONING_DOC in out


def test_run_exits_nonzero_when_probe_set_is_empty(capsys, monkeypatch):
    """An empty probe set must FAIL the check, not pass via `all([])` being True.

    The exit code and the printed verdict must agree: with nothing verified,
    `format_report` renders NOT LIVE, so `run` must also return non-zero.
    """
    monkeypatch.setattr(verify_apps, "known_agents", list)
    rc = verify_apps.run("owner/repo", mint=_mint_live)
    assert rc == 1
    assert "NOT LIVE" in capsys.readouterr().out


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
    monkeypatch.setattr(verify_apps.gh, "current_repo", lambda: "owner/here")
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
