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
``checks: write``. It reports **which of four distinct situations** it is in —
never collapsing them, because they have different owners and different remedies
(#969):

  * :data:`LIVE` — the App is installed on the repo's owner AND holds
    ``checks: write`` (the install + re-consent landed);
  * :data:`NOT_LIVE` — GitHub answered and the answer is a verdict: the App is not
    installed on the owner (404), or it is installed but the token lacks
    ``checks: write`` (the re-consent was missed). A REMOTE gap, fixed by the
    one-time install/consent in ``docs/dev/review-app-provisioning.md``;
  * :data:`UNCONFIGURED` — this machine cannot mint App credentials at all (no
    `doppler`, no login, no such key), so the App's state was never asked about.
    A LOCAL gap; it says NOTHING about the repo; and
  * :data:`UNDETERMINED` — credentials worked but the probe failed (GitHub error,
    timeout). Nothing is known either way.

The last two are the false negative #969 reports: a consumer repo whose operator
has no App credentials to hand printed ``NOT LIVE`` — reading as "the Apps are
not installed on your repo" when the Apps were in fact live and only the local
auth was missing. Saying "unverified here" is the truthful answer, and a rollout
must not treat it as a remote gap.

It only VERIFIES and INSTRUCTS. The actual per-repo App install/consent EXECUTION
is the ROL01 rollout's job (one sub-issue per repo) — out of scope here — and the
install-seeds-secrets change is issue #25. ``run``'s exit code collapses those
four situations into the three ANSWERS a rollout acts on (0 live / 1 a real remote
gap / 2 unverified here — the two no-verdict situations share code ``2``), so a
rollout can branch on it: ``shipit verify-apps owner/repo; echo $?``.

The probe is the SAME in-memory App-auth path the funnel itself uses
(:mod:`shipit.review.ghauth`: Doppler-sourced PEM → in-memory RS256 JWT →
installation token; the PEM never lands on disk). It checks the granted
``permissions`` map on the minted token rather than driving a check-run create, so
verifying liveness leaves no breadcrumb on the target repo.
"""

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

#: The runbook for the one-time, owner-only install + ``checks: write`` re-consent.
#: Every not-live result points here (it is the authority on what "live" means).
PROVISIONING_DOC = "docs/dev/review-app-provisioning.md"

#: The four situations a probe can land in (#969) — see the module docstring.
#: :data:`LIVE` and :data:`NOT_LIVE` are VERDICTS about the App on the target
#: repo; :data:`UNCONFIGURED` and :data:`UNDETERMINED` are admissions that no
#: verdict was reached, and must never render or exit as one.
LIVE = "live"
NOT_LIVE = "not-live"
UNCONFIGURED = "unconfigured"
UNDETERMINED = "undetermined"

#: :class:`ReviewAuthError` kind → the situation it puts the probe in. The
#: registry IS the mapping (no branch chain): an auth kind shipit adds later must
#: choose its situation here rather than defaulting into a verdict.
_KIND_STATUS = {
    ghauth.UNCONFIGURED: UNCONFIGURED,
    ghauth.NOT_INSTALLED: NOT_LIVE,
    ghauth.API_ERROR: UNDETERMINED,
}

#: The console mark per situation. Both no-verdict situations read UNVERIFIED —
#: the distinction between them lives in the line's reason, which names whether it
#: is this machine's credentials or GitHub that failed.
_MARKS = {
    LIVE: "live",
    NOT_LIVE: "NOT LIVE",
    UNCONFIGURED: "UNVERIFIED",
    UNDETERMINED: "UNVERIFIED",
}

#: The three words a whole run can be headed by (:func:`verdict`).
VERDICT_LIVE = "LIVE"
VERDICT_NOT_LIVE = "NOT LIVE"
VERDICT_UNVERIFIED = "UNVERIFIED"

#: Exit code per verdict — the mechanical signal a rollout branches on.
#: ``0`` all live; ``1`` a REMOTE gap someone must fix on the repo; ``2`` nothing
#: was verified here, which is a different job (configure this machine) and must
#: not be mistaken for ``1``.
RC_LIVE = 0
RC_NOT_LIVE = 1
RC_UNVERIFIED = 2

_VERDICT_RC = {
    VERDICT_LIVE: RC_LIVE,
    VERDICT_NOT_LIVE: RC_NOT_LIVE,
    VERDICT_UNVERIFIED: RC_UNVERIFIED,
}


def known_agents() -> list[str]:
    """The local-agent reviewer Apps this verb can probe — the funnel backends.

    These are exactly the backends the ONE identity registry (ADR-0025) marks as
    funnel App reviewers (``codex`` / ``agy``); a reviewer with no App
    (``copilot``) has no installation token to probe and is not a local-agent App.
    """
    # ``funnel_backends()`` already guarantees a funnel identity; the truthy filter
    # narrows ``funnel_agent`` to ``str`` and refuses to fabricate a blank alias
    # (matching this module's raise-don't-fabricate stance) rather than leaking a
    # ``""`` choice into CLI help that would then fail at ``by_funnel_agent("")``.
    return sorted(
        b.funnel_agent for b in _agent_backend.funnel_backends() if b.funnel_agent
    )


@dataclass(frozen=True)
class AppLiveness:
    """What one probe of one reviewer App on a target repo established.

    ``status`` is one of :data:`LIVE`, :data:`NOT_LIVE`, :data:`UNCONFIGURED`, or
    :data:`UNDETERMINED` — the four situations, kept apart on purpose (#969): only
    the first two are verdicts about the App. ``reason`` is empty on a pass and
    otherwise carries the human-readable "what's missing + go here", addressed to
    whoever owns THAT situation (the repo's owner for a remote gap, this machine's
    operator for a local one).
    """

    agent: str
    app: str
    status: str
    reason: str = ""

    @property
    def live(self) -> bool:
        """Whether this App is usable — ``status == LIVE`` and nothing else.

        Deliberately NOT true for :data:`UNCONFIGURED` / :data:`UNDETERMINED`: an
        unverified App is not a live one. Read :attr:`status` (never ``not live``)
        to tell a verdict from an admission.
        """
        return self.status == LIVE


def _auth_failure(
    exc: ghauth.ReviewAuthError, agent: str, slug: str, repo: str
) -> AppLiveness:
    """Read a mint failure as the situation it actually is (#969).

    The exception's OWN ``kind`` decides — never the target repo, never the
    message text — so "no App credentials on this machine" and "that owner has not
    installed the App" cannot render as the same verdict. Each situation's reason
    is addressed to the person who can fix it:

      * :data:`NOT_LIVE` (``NOT_INSTALLED``) — the repo's owner installs the App;
      * :data:`UNCONFIGURED` — this machine's operator supplies App credentials,
        and the App's real state on ``repo`` stays UNKNOWN; and
      * :data:`UNDETERMINED` (``API_ERROR``) — nobody has anything to fix yet; the
        probe failed and should be re-run.
    """
    # The situations are string constants, so they compare by VALUE (`==`): `is`
    # would ride CPython's interning of these literals — true today, silently
    # false the moment a status arrives from a config read or a round-trip.
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
    # The underlying message goes LAST, parenthesised: it is arbitrary text from
    # Doppler / GitHub / urllib with no reliable punctuation, so interpolating it
    # mid-sentence ran two sentences together ("…not found on PATH Configure the…").
    return AppLiveness(agent, slug, status, f"{reason} ({exc})")


def verify_app(backend: Backend, repo: str, *, mint=None) -> AppLiveness:
    """Probe ``backend``'s review App on ``repo`` — one of the four situations.

    Mints the App installation token (``mint`` defaults to
    :func:`shipit.review.ghauth.installation_auth`) and reads the ``permissions``
    map GitHub granted it. This is a cheap read — it creates no check run, so the
    probe leaves no breadcrumb on the target.

    Outcomes:

      * :data:`LIVE` — installed, and the token carries ``checks: write``;
      * :data:`NOT_LIVE` — either the mint said the App is not installed on the
        owner (404), or the token's ``permissions.checks`` is not ``write`` (the
        re-grant/consent was missed) — both instruct against
        :data:`PROVISIONING_DOC`; and
      * :data:`UNCONFIGURED` / :data:`UNDETERMINED` — the probe never reached a
        verdict (see :func:`_auth_failure`).

    A pass returns ``reason=""``.
    """
    minter = mint if mint is not None else ghauth.installation_auth
    agent = backend.funnel_agent or backend.name
    # The App slug is a registry alias (ADR-0025), never composed here.
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
            # EXPECTED and operator-actionable — no App credentials HERE. Log the
            # fact, not a traceback: the same no-spray posture the reviewer adapter
            # takes for this exact failure, so a consumer running `verify-apps`
            # without Doppler reads the remedy instead of a stack dump (#969).
            logger.warning(
                "app credentials unavailable — app not checked", extra=record
            )
        else:
            # A verdict (not installed) or a genuine probe failure — attach the
            # exception.
            logger.error(
                "app installation token mint failed", exc_info=True, extra=record
            )
        return result
    perms = auth.get("permissions", {}) if isinstance(auth, dict) else {}
    granted = perms.get("checks")
    if granted != "write":
        # Reachable but missing the permission — degraded, not a hard failure.
        record = {
            "repo": repo,
            "agent": agent,
            "app": slug,
            "status": NOT_LIVE,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if granted is not None:
            # Present only when GitHub granted SOMETHING — absent, not null.
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
    # A per-App pass is a mechanic; the run verdict is the milestone.
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
    """The one word for a whole run — the weakest claim its probes support.

    Precedence: any :data:`NOT_LIVE` makes the run ``NOT LIVE`` (a real remote gap
    outranks an unverified sibling — it is the finding a rollout must act on);
    otherwise any non-live probe makes it ``UNVERIFIED``; all live is ``LIVE``. An
    EMPTY run is ``NOT LIVE``, not a vacuous pass: ``all([])`` is True, but a check
    that probed nothing has established nothing to pass on.
    """
    if not results or any(r.status == NOT_LIVE for r in results):
        return VERDICT_NOT_LIVE
    return VERDICT_LIVE if all(r.live for r in results) else VERDICT_UNVERIFIED


def exit_code(results: list[AppLiveness]) -> int:
    """The run's exit code — ``0`` live / ``1`` a remote gap / ``2`` unverified here.

    Derived FROM :func:`verdict` rather than re-deciding, so the code a rollout
    branches on and the word the operator reads cannot drift apart.
    """
    return _VERDICT_RC[verdict(results)]


def format_report(repo: str, results: list[AppLiveness]) -> str:
    """A clear, line-per-App block for the console, headed by the run's verdict.

    Each line's mark is the probe's OWN situation, so an unverified App reads
    ``UNVERIFIED`` even in a run headed ``NOT LIVE`` by one of its siblings.
    """
    lines = [f"verify-apps: {repo} — {verdict(results)}"]
    for result in results:
        line = f"  [{_MARKS[result.status]}] {result.app} ({result.agent})"
        if result.reason:
            line += f"\n         {result.reason}"
        lines.append(line)
    return "\n".join(lines)


def run(repo: str | None, *, agents: list[str] | None = None, mint=None) -> int:
    """Verify each local-agent reviewer App on ``repo`` — exit 0 / 1 / 2.

    ``repo`` (``owner/name``) defaults to the current checkout's repo, mirroring
    ``gh-setup`` / ``logs``. ``agents`` selects which App reviewers to probe;
    omitted, it probes every known local-agent App (:func:`known_agents`). Prints
    a line per App and returns :func:`exit_code`: ``0`` all live, ``1`` a remote
    gap on the repo, ``2`` nothing could be verified from here (#969).
    """
    started = time.monotonic()
    target = repo
    if not target:
        try:
            # The typed adapter read (PROC03); the App probes speak slugs.
            # ValueError (no usable owner/name from gh) reads like the transport
            # failure: no inferable repo.
            target = gh.current_repo().slug
        except (execrun.ExecError, ValueError):
            # The no-repo dead end — the stderr line's durable twin, recorded
            # while the resolution failure is still live so it rides the record.
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
    # The CLI selects by the funnel-agent alias; resolve each back to the ONE
    # registry identity (the `--agents` choices are built from the same registry,
    # so the lookup cannot miss for CLI input).
    results = [
        verify_app(_agent_backend.by_funnel_agent(agent), target, mint=mint)
        for agent in selected
    ]
    print(format_report(target, results))
    # ONE decision, read twice: the printed verdict and the exit code both come
    # from `verdict`, so they cannot disagree.
    rc = exit_code(results)
    # The mechanical verdict a rollout reads — the lifecycle milestone, pass or
    # fail (the verdict propagates through the exit code, not an exception).
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
        # Present only when meaningful — the absent-not-null record contract.
        summary["not_live_apps"] = ", ".join(not_live)
    if unverified:
        # Kept SEPARATE from `not_live_apps` (#969): an App nobody could check is
        # not an App found missing, and a rollout reading this record must be able
        # to tell them apart without re-parsing the reason prose.
        summary["unverified_apps"] = ", ".join(unverified)
    logger.info("app liveness verified", extra=summary)
    return rc
