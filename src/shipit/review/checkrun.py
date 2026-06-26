"""checkrun — the local-review funnel breadcrumb, as an App-authored check run.

GitHub denies a custom App bot the native ``review_requested`` edge (ADR-0005),
so a local reviewer (``codex-local`` / ``agy-local``) has no *requested /
in-flight* signal on the PR until it actually posts. This module supplies the
missing breadcrumb: a **GitHub Check Run authored by the reviewer's own App**,
the native, timestamped stand-in for that edge.

:func:`create` (OBS02-WS01) opens the run ``status=in_progress`` with
``started_at=now``; :func:`transition` (OBS02-WS02) closes that SAME run to its
terminal ``conclusion`` (``success`` / ``failure`` / ``timed_out`` / ``neutral``)
at completion. The two share this module's App-token boundary — one create, one
PATCH to the run create returned — so the breadcrumb carries one run through its
whole life and shipit never opens a second run.

The auth is the same installation-token path :mod:`shipit.review.post` already
uses to post AS the bot (Doppler-sourced PEM → in-memory RS256 JWT → installation
token, via :mod:`shipit.review.ghauth`): the PEM never lands on disk, and the
minted token is threaded onto the ``gh`` boundary but NEVER reaches a log record.

The run is **non-required** — a check run only blocks merge if branch protection
names it, and shipit never registers it there, so it is *visible but never gates*
(the Ready pillar is *settled*, not *succeeded*). And ``started_at`` is the
load-bearing output: OBS02 only WRITES an honest timestamp; reading/aging it
against a wait window is OBS04.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import gh
from . import ghauth

#: The funnel writes through the shared review logger (OBS01 sink). The minted
#: installation token is NEVER passed to a record — only the run's identity facts.
logger = logging.getLogger("shipit.review")


def reviewer_name(agent: str) -> str:
    """The funnel reviewer name for ``agent`` (``codex`` → ``codex-local``).

    The local backends surface as the ``<agent>-local`` reviewer; the check run
    is named ``review: <reviewer>`` so the funnel reads one run per reviewer.
    """
    return f"{agent}-local"


def create(agent: str, repo: str, head_sha: str) -> int | None:
    """Open the in-progress funnel check run for ``agent`` on ``repo``@``head_sha``.

    Mints the agent's App installation token (so GitHub attributes the run to
    ``adr-<agent>-review[bot]``) and POSTs ``/repos/{repo}/check-runs`` with
    ``status=in_progress`` and ``started_at=now`` (an honest, tz-aware UTC
    timestamp — the breadcrumb OBS04 ages a wait window against). Returns the new
    run's id (or ``None`` if the response carried none).

    Honest by design: any failure (a missing scope 403 before the ``checks:write``
    re-grant, an auth failure, a ``gh`` failure) PROPAGATES. The best-effort
    swallowing that keeps a breadcrumb failure from failing the review lives in
    :func:`shipit.review.service.run_and_post`, so this function stays a thin,
    reusable base for WS02's terminal transition.
    """
    name = f"review: {reviewer_name(agent)}"
    token = ghauth.installation_token(agent, repo)
    body = {
        "name": name,
        "head_sha": head_sha,
        "status": "in_progress",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.debug(
        "checkrun.create: opening %r on %s @ %s (as the %r app)",
        name,
        repo,
        head_sha,
        agent,
    )
    response = gh.rest(
        f"/repos/{repo}/check-runs", method="POST", body=body, token=token
    )
    run_id = response.get("id") if isinstance(response, dict) else None
    logger.info("checkrun.create: opened %r on %s (run id=%s)", name, repo, run_id)
    return int(run_id) if run_id is not None else None


def transition(
    agent: str,
    repo: str,
    run_id: int,
    *,
    conclusion: str,
    title: str,
    summary: str,
) -> None:
    """Close the funnel check run ``run_id`` to its terminal ``conclusion``.

    PATCHes ``/repos/{repo}/check-runs/{run_id}`` — the SAME run :func:`create`
    opened, never a second run — with ``status=completed``, the mapped
    ``conclusion`` (``success`` for a posted review incl. a clean zero-findings
    one, ``failure`` for a failed *or* empty run, ``timed_out`` for a timeout;
    ``neutral`` is an accepted alternative for empty), an ``output`` (``title`` +
    ``summary``) message, and a tz-aware ``completed_at=now`` (the load-bearing
    timestamp OBS04 ages against, the mirror of ``create``'s ``started_at``).

    Authored via the agent's App installation token, so GitHub keeps attributing
    the run to ``adr-<agent>-review[bot]``; the token is threaded onto the ``gh``
    boundary but NEVER reaches a log record, mirroring :func:`create`.

    Honest by design like :func:`create`: any mint/PATCH failure PROPAGATES. The
    best-effort swallowing — and the "no run id ⇒ nothing to transition" skip when
    ``create`` opened no run (e.g. a ``403`` before the ``checks:write`` re-grant)
    — live in :func:`shipit.review.service.run_and_post`, so a breadcrumb failure
    never crashes the review and never masks its real outcome.
    """
    name = f"review: {reviewer_name(agent)}"
    token = ghauth.installation_token(agent, repo)
    body = {
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "output": {"title": title, "summary": summary},
    }
    logger.debug(
        "checkrun.transition: closing %r on %s (run id=%s) -> completed/%s "
        "(as the %r app)",
        name,
        repo,
        run_id,
        conclusion,
        agent,
    )
    gh.rest(
        f"/repos/{repo}/check-runs/{run_id}", method="PATCH", body=body, token=token
    )
    logger.info(
        "checkrun.transition: closed %r on %s (run id=%s) -> completed/%s",
        name,
        repo,
        run_id,
        conclusion,
    )
