"""verify-apps — probe each local-agent reviewer App for ``checks: write`` on a repo."""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass

from .. import execrun, gh
from ..agent import backend as _agent_backend
from ..agent.backend import Backend
from ..review import ghauth

logger = logging.getLogger("shipit.verifyapps")

PROVISIONING_DOC = "docs/dev/review-app-provisioning.md"

LIVE = "live"
NOT_LIVE = "not-live"
UNCONFIGURED = "unconfigured"
UNDETERMINED = "undetermined"

_KIND_STATUS = {
    ghauth.UNCONFIGURED: UNCONFIGURED,
    ghauth.NOT_INSTALLED: NOT_LIVE,
    ghauth.API_ERROR: UNDETERMINED,
}

_MARKS = {
    LIVE: "live",
    NOT_LIVE: "NOT LIVE",
    UNCONFIGURED: "UNVERIFIED",
    UNDETERMINED: "UNVERIFIED",
}

VERDICT_LIVE = "LIVE"
VERDICT_NOT_LIVE = "NOT LIVE"
VERDICT_UNVERIFIED = "UNVERIFIED"

RC_LIVE = 0
RC_NOT_LIVE = 1
RC_UNVERIFIED = 2

_VERDICT_RC = {
    VERDICT_LIVE: RC_LIVE,
    VERDICT_NOT_LIVE: RC_NOT_LIVE,
    VERDICT_UNVERIFIED: RC_UNVERIFIED,
}


def known_agents() -> list[str]:
    """The local-agent reviewer Apps this verb can probe — the funnel backends."""
    return sorted(
        b.funnel_agent for b in _agent_backend.funnel_backends() if b.funnel_agent
    )


@dataclass(frozen=True)
class AppLiveness:
    """What one probe of one reviewer App on a target repo established; ``reason`` is empty on a pass."""

    agent: str
    app: str
    status: str
    reason: str = ""

    @property
    def live(self) -> bool:
        """Whether this App is usable — ``status == LIVE`` and nothing else; read :attr:`status` to tell a verdict from an admission."""
        return self.status == LIVE


def _auth_failure(
    exc: ghauth.ReviewAuthError, agent: str, slug: str, repo: str
) -> AppLiveness:
    """Read a mint failure as the situation its ``kind`` names, never as a verdict about the repo."""
    status = _KIND_STATUS[exc.kind]
    if status == NOT_LIVE:
        reason = (
            f"App {slug!r} is not installed on {repo}'s owner. Install the App and "
            f"re-consent per {PROVISIONING_DOC}."
        )
    elif status == UNCONFIGURED:
        reason = (
            f"App {slug!r} was NOT checked: this environment cannot mint App "
            f"credentials, so nothing was asked of GitHub and the App's state on "
            f"{repo} is unknown. Configure the App credentials here (see "
            f"{PROVISIONING_DOC}) and re-run."
        )
    else:
        reason = (
            f"App {slug!r} was NOT checked: the probe itself failed, so the App's "
            f"state on {repo} is unknown. Re-run."
        )
    return AppLiveness(agent, slug, status, f"{reason} ({exc})")


def verify_app(backend: Backend, repo: str, *, mint=None) -> AppLiveness:
    """Probe ``backend``'s review App on ``repo`` — one of the four situations; creates no check run."""
    minter = mint if mint is not None else ghauth.installation_auth
    agent = backend.funnel_agent or backend.name
    slug = backend.app_slug
    started = time.monotonic()
    try:
        auth = minter(backend, repo)
    except ghauth.ReviewAuthError as exc:
        result = _auth_failure(exc, agent, slug, repo)
        record = {
            "repo": repo,
            "agent": agent,
            "app": slug,
            "status": result.status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if result.status == UNCONFIGURED:
            logger.warning(
                "app credentials unavailable — app not checked", extra=record
            )
        else:
            logger.error(
                "app installation token mint failed", exc_info=True, extra=record
            )
        return result
    perms = auth.get("permissions", {}) if isinstance(auth, dict) else {}
    granted = perms.get("checks")
    if granted != "write":
        record = {
            "repo": repo,
            "agent": agent,
            "app": slug,
            "status": NOT_LIVE,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if granted is not None:
            record["checks"] = granted
        logger.warning("app token lacks checks-write — app not live", extra=record)
        return AppLiveness(
            agent,
            slug,
            NOT_LIVE,
            f"App {slug!r} is installed on {repo}'s owner but its token lacks the "
            f"'checks: write' permission (checks={granted!r}). Accept the updated "
            f"permissions for this owner's installation per {PROVISIONING_DOC}.",
        )
    logger.debug(
        "app live",
        extra={
            "repo": repo,
            "agent": agent,
            "app": slug,
            "status": LIVE,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return AppLiveness(agent, slug, LIVE)


def verdict(results: list[AppLiveness]) -> str:
    """The one word for a whole run — the weakest claim its probes support; an empty run is NOT LIVE, not a vacuous pass."""
    if not results or any(r.status == NOT_LIVE for r in results):
        return VERDICT_NOT_LIVE
    return VERDICT_LIVE if all(r.live for r in results) else VERDICT_UNVERIFIED


def exit_code(results: list[AppLiveness]) -> int:
    """The run's exit code — 0 live / 1 a remote gap / 2 unverified here."""
    return _VERDICT_RC[verdict(results)]


def format_report(repo: str, results: list[AppLiveness]) -> str:
    """A line-per-App block for the console, headed by the run's verdict."""
    lines = [f"verify-apps: {repo} — {verdict(results)}"]
    for result in results:
        line = f"  [{_MARKS[result.status]}] {result.app} ({result.agent})"
        if result.reason:
            line += f"\n         {result.reason}"
        lines.append(line)
    return "\n".join(lines)


def run(repo: str | None, *, agents: list[str] | None = None, mint=None) -> int:
    """Verify each local-agent reviewer App on ``repo`` (default: the current checkout's); returns the exit code."""
    started = time.monotonic()
    target = repo
    if not target:
        try:
            target = gh.current_repo().slug
        except (execrun.ExecError, ValueError):
            logger.error(
                "no repo given and not inside a GitHub checkout", exc_info=True
            )
            target = None
    if not target:
        print(
            "verify-apps: no repo given and not inside a GitHub checkout",
            file=sys.stderr,
        )
        return 1

    selected = agents or known_agents()
    results = [
        verify_app(_agent_backend.by_funnel_agent(agent), target, mint=mint)
        for agent in selected
    ]
    print(format_report(target, results))
    rc = exit_code(results)
    not_live = sorted(r.app for r in results if r.status == NOT_LIVE)
    unverified = sorted(
        r.app for r in results if r.status in (UNCONFIGURED, UNDETERMINED)
    )
    summary = {
        "repo": target,
        "apps": len(results),
        "live": sum(1 for r in results if r.live),
        "verdict": verdict(results),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if not_live:
        summary["not_live_apps"] = ", ".join(not_live)
    if unverified:
        summary["unverified_apps"] = ", ".join(unverified)
    logger.info("app liveness verified", extra=summary)
    return rc
