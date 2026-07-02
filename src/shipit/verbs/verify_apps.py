"""verify-apps — per-consumer App-liveness verification for the local-review funnel.

The local-review funnel (OBS02, ADR-0005) rides on App-authored GitHub **check
runs**: a ``review: <reviewer>`` run created by the reviewer's own GitHub App
(``adr-codex-review[bot]`` / ``adr-agy-review[bot]``). Creating that run needs the
App's installation token to carry **``checks: write``**, which is a one-time,
owner-only install + re-consent per ``docs/dev/review-app-provisioning.md``. Until
that lands for an owner, the funnel breadcrumb create returns **403** and the
``review: <reviewer>`` signal silently never appears on the PR.

This verb makes that provisioning state **mechanically checkable** before a
rollout: given a target repo, for each configured local-agent reviewer App it
mints the App installation token (the cheap, side-effect-free read — NOT a
check-run create) and asserts the token GitHub actually granted carries
``checks: write``. It returns a clear **pass-or-instruct** result:

  * **pass** — the App is installed on the repo's owner AND holds ``checks: write``
    (the install + re-consent landed); and
  * **not live** — either the App is not installed on the owner (the mint
    ``ReviewAuthError``) or it is installed but the token lacks ``checks: write``
    (the re-consent was missed). Either way the result NAMES the missing
    App/permission and points at ``docs/dev/review-app-provisioning.md`` for the
    one-time install/consent.

It only VERIFIES and INSTRUCTS. The actual per-repo App install/consent EXECUTION
is the ROL01 rollout's job (one sub-issue per repo) — out of scope here — and the
install-seeds-secrets change is issue #25. ``run`` exits non-zero when any App is
not live, so a rollout can branch on it: ``shipit verify-apps owner/repo; echo $?``.

The probe is the SAME in-memory App-auth path the funnel itself uses
(:mod:`shipit.review.ghauth`: Doppler-sourced PEM → in-memory RS256 JWT →
installation token; the PEM never lands on disk). It checks the granted
``permissions`` map on the minted token rather than driving a check-run create, so
verifying liveness leaves no breadcrumb on the target repo.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .. import execrun, gh
from ..agent import backend as _agent_backend
from ..agent.backend import Backend
from ..review import ghauth

#: The runbook for the one-time, owner-only install + ``checks: write`` re-consent.
#: Every not-live result points here (it is the authority on what "live" means).
PROVISIONING_DOC = "docs/dev/review-app-provisioning.md"


def known_agents() -> list[str]:
    """The local-agent reviewer Apps this verb can probe — the funnel backends.

    These are exactly the backends the ONE identity registry (ADR-0025) marks as
    funnel App reviewers (``codex`` / ``agy``); a reviewer with no App
    (``copilot``) has no installation token to probe and is not a local-agent App.
    """
    return sorted(b.funnel_agent or "" for b in _agent_backend.funnel_backends())


@dataclass(frozen=True)
class AppLiveness:
    """The verified liveness of one reviewer App on a target repo.

    ``live`` is True only when the App is installed on the repo's owner AND its
    minted installation token carries ``checks: write``. ``reason`` is empty on a
    pass and otherwise carries the human-readable "what's missing + go here" — the
    INSTRUCT half of pass-or-instruct.
    """

    agent: str
    app: str
    live: bool
    reason: str = ""


def verify_app(backend: Backend, repo: str, *, mint=None) -> AppLiveness:
    """Probe whether ``backend``'s review App is LIVE on ``repo`` — pass-or-instruct.

    Mints the App installation token (``mint`` defaults to
    :func:`shipit.review.ghauth.installation_auth`) and reads the ``permissions``
    map GitHub granted it. This is a cheap read — it creates no check run, so the
    probe leaves no breadcrumb on the target.

    Two not-live shapes, each named with its remedy:

      * a :class:`~shipit.review.ghauth.ReviewAuthError` minting the token — the
        App is not installed on the repo's owner (404) or its credentials can't be
        sourced — instruct to INSTALL the App; and
      * the token's ``permissions.checks`` is not ``write`` — the App is installed
        but the ``checks: write`` re-grant/consent was missed — instruct to
        RE-CONSENT.

    Both point at :data:`PROVISIONING_DOC`. A pass returns ``reason=""``.
    """
    minter = mint if mint is not None else ghauth.installation_auth
    agent = backend.funnel_agent or backend.name
    # The App slug is a registry alias (ADR-0025), never composed here.
    slug = backend.app_slug
    try:
        auth = minter(backend, repo)
    except ghauth.ReviewAuthError as exc:
        return AppLiveness(
            agent,
            slug,
            False,
            f"App {slug!r} is not installed on {repo}'s owner (or its credentials "
            f"could not be sourced): {exc} Install the App and re-consent per "
            f"{PROVISIONING_DOC}.",
        )
    perms = auth.get("permissions", {}) if isinstance(auth, dict) else {}
    granted = perms.get("checks")
    if granted != "write":
        return AppLiveness(
            agent,
            slug,
            False,
            f"App {slug!r} is installed on {repo}'s owner but its token lacks the "
            f"'checks: write' permission (checks={granted!r}). Accept the updated "
            f"permissions for this owner's installation per {PROVISIONING_DOC}.",
        )
    return AppLiveness(agent, slug, True)


def format_report(repo: str, results: list[AppLiveness]) -> str:
    """A clear, line-per-App pass-or-instruct block for the console."""
    all_live = bool(results) and all(r.live for r in results)
    verdict = "LIVE" if all_live else "NOT LIVE"
    lines = [f"verify-apps: {repo} — {verdict}"]
    for result in results:
        mark = "live" if result.live else "NOT LIVE"
        line = f"  [{mark}] {result.app} ({result.agent})"
        if result.reason:
            line += f"\n         {result.reason}"
        lines.append(line)
    return "\n".join(lines)


def run(repo: str | None, *, agents: list[str] | None = None, mint=None) -> int:
    """Verify each local-agent reviewer App on ``repo`` — exit 0 (all live) / 1.

    ``repo`` (``owner/name``) defaults to the current checkout's repo, mirroring
    ``gh-setup`` / ``logs``. ``agents`` selects which App reviewers to probe;
    omitted, it probes every known local-agent App (:func:`known_agents`). Prints
    a pass-or-instruct line per App and returns ``0`` only when ALL are live, ``1``
    otherwise — the mechanical verdict a rollout reads.
    """
    target = repo
    if not target:
        try:
            target = gh.current_repo()
        except execrun.ExecError:
            target = None
    if not target:
        print(
            "verify-apps: no repo given and not inside a GitHub checkout",
            file=sys.stderr,
        )
        return 1

    selected = agents or known_agents()
    # The CLI selects by the funnel-agent alias; resolve each back to the ONE
    # registry identity (the `--agents` choices are built from the same registry,
    # so the lookup cannot miss for CLI input).
    results = [
        verify_app(_agent_backend.by_funnel_agent(agent), target, mint=mint)
        for agent in selected
    ]
    print(format_report(target, results))
    # An empty probe set is NOT a pass: `all([])` is True, but a check with nothing
    # verified must fail (and `format_report` already renders empty as NOT LIVE).
    # Mirror that verdict so the exit code and the printed report never disagree.
    return 0 if (bool(results) and all(r.live for r in results)) else 1
