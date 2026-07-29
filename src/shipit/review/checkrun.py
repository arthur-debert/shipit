"""The local-review funnel breadcrumb, as an App-authored check run.

See docs/adr/0005-local-review-funnel-via-check-runs.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import quote

from .. import gh
from ..agent.backend import Backend
from . import ghauth

#: The minted installation token is NEVER passed to a record.
logger = logging.getLogger("shipit.review")

#: The sole terminal check-run status; every other status the Checks API can
#: surface means the run is still in flight.
_TERMINAL_STATUS = "completed"


def _run_name(backend: Backend) -> str:
    """The full funnel check-run name — ``review: <agent>-local``."""
    return f"review: {backend.check_run_name}"


def create(backend: Backend, repo: str, head_sha: str) -> int | None:
    """Open the in-progress funnel check run for ``backend`` on ``repo``@``head_sha``.

    Every failure propagates; the best-effort swallowing lives in the caller.
    """
    name = _run_name(backend)
    token = ghauth.installation_token(backend, repo)
    body = {
        "name": name,
        "head_sha": head_sha,
        "status": "in_progress",
        "started_at": datetime.now(UTC).isoformat(),
    }
    logger.debug(
        "check run %r opening on %s @ %s (as the %r app)",
        name,
        repo,
        head_sha,
        backend.funnel_agent,
    )
    response = gh.rest(
        f"/repos/{repo}/check-runs", method="POST", body=body, token=token
    )
    run_id = response.get("id") if isinstance(response, dict) else None
    logger.info("check run %r opened on %s (run id=%s)", name, repo, run_id)
    return int(run_id) if run_id is not None else None


def transition(
    backend: Backend,
    repo: str,
    run_id: int,
    *,
    conclusion: str,
    title: str,
    summary: str,
) -> None:
    """Close the funnel check run ``run_id`` to its terminal ``conclusion``.

    Every failure propagates; the best-effort swallowing lives in the caller.
    """
    name = _run_name(backend)
    token = ghauth.installation_token(backend, repo)
    body = {
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": datetime.now(UTC).isoformat(),
        "output": {"title": title, "summary": summary},
    }
    logger.debug(
        "check run %r closing on %s (run id=%s) -> completed/%s (as the %r app)",
        name,
        repo,
        run_id,
        conclusion,
        backend.funnel_agent,
    )
    gh.rest(
        f"/repos/{repo}/check-runs/{run_id}", method="PATCH", body=body, token=token
    )
    logger.info(
        "check run %r closed on %s (run id=%s) -> completed/%s",
        name,
        repo,
        run_id,
        conclusion,
    )


def find_nonterminal(backend: Backend, repo: str, head_sha: str) -> int | None:
    """The id of an in-flight funnel run for ``backend`` on ``repo``@``head_sha``, else ``None``.

    Every failure propagates; the best-effort swallowing lives in the caller.
    """
    name = _run_name(backend)
    token = ghauth.installation_token(backend, repo)
    # The `check_name` value carries a space + colon — url-encode it so the query
    # string is well formed (`gh api` passes the path through verbatim).
    path = f"/repos/{repo}/commits/{head_sha}/check-runs?check_name={quote(name)}"
    logger.debug(
        "check run %r read on %s @ %s (as the %r app)",
        name,
        repo,
        head_sha,
        backend.funnel_agent,
    )
    response = gh.rest(path, token=token)
    runs = response.get("check_runs") if isinstance(response, dict) else None
    for run in runs or []:
        if isinstance(run, dict) and run.get("status") != _TERMINAL_STATUS:
            run_id = run.get("id")
            if run_id is not None:
                logger.info(
                    "check run %r on %s has an in-flight run (id=%s, status=%s)",
                    name,
                    repo,
                    run_id,
                    run.get("status"),
                )
                return int(run_id)
    return None
