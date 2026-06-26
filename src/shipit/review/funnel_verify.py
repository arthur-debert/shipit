"""funnel_verify — the OPT-IN, LIVE-GitHub verification harness for the OBS02 funnel.

OBS02-WS01 (kickoff create) and WS02 (terminal transition) write the local-review
funnel breadcrumb — an App-authored ``review: <reviewer>`` check run — and are
unit-tested with the App-token boundary FAKED (``tests/test_review_checkrun.py``,
``tests/test_review_funnel.py``). Those faked tests prove the *shape* shipit
writes; they cannot prove the breadcrumb is REAL on GitHub, because the load-bearing
fact — that the App's installation token actually carries ``checks: write`` and that
``POST .../check-runs`` returns **201, not 403** — only exists against live GitHub
(the ``checks:write`` re-grant + per-install owner consent, see
``docs/dev/review-app-provisioning.md``).

This module is that missing live counterpart: :func:`verify` drives the WHOLE
lifecycle end-to-end against a real canary PR, through the real App-installation-token
boundary, and asserts every breadcrumb fact:

  1. the create-installation-token response's ``permissions`` includes
     ``checks: write`` (the granted scope — the re-grant landed for this owner);
  2. :func:`shipit.review.checkrun.create` returns a run id — i.e.
     ``POST /repos/<repo>/check-runs`` returned **201, not 403** — and the run reads
     back ``in_progress`` with a ``started_at``;
  3. :func:`shipit.review.checkrun.transition` closes that SAME run id (no second
     run) to its terminal ``conclusion`` + ``output`` + ``completed_at``.

**Siting — why this is NOT in the test gate.** It hits live GitHub, needs Doppler
App creds, and needs a canary PR, so it must never run inside ``pixi run test`` / CI
(which have none of those and would fail). So it is a standalone ``python -m``
entrypoint (``pixi run -e verify verify-funnel``), it REFUSES to run without an
explicit ``--repo`` + ``--pr`` (or the ``SHIPIT_FUNNEL_CANARY_REPO`` /
``SHIPIT_FUNNEL_CANARY_PR`` env), and pytest never collects it (it lives in
``src/``, not ``tests/``). Its assertion/wiring logic is regression-covered in the
normal gate by ``tests/test_review_funnel_verify.py``, which drives :func:`verify`
with the same boundary FAKED — so the harness itself can't silently rot.

Run it (for the ``arthur-debert`` owner, whose re-grant is live)::

    pixi run -e verify verify-funnel --repo arthur-debert/shipit-canary --pr <N>
    # or:
    SHIPIT_FUNNEL_CANARY_REPO=arthur-debert/shipit-canary \
    SHIPIT_FUNNEL_CANARY_PR=<N> python -m shipit.review.funnel_verify

It exits ``0`` on a full PASS, ``1`` on any failed check. The check run it creates
is non-required (visible, never blocking) and harmless; on a throwaway canary PR,
deleting the PR's branch is the cleanup.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

from .. import gh
from . import checkrun, ghauth

logger = logging.getLogger("shipit.review")


@dataclass
class Check:
    """One asserted breadcrumb fact: a human-readable ``name``, whether it
    ``passed``, and a ``detail`` string carrying the observed value."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    """The accumulated result of a :func:`verify` run — an ordered list of
    :class:`Check`s plus the created ``run_id`` (so a caller can clean up)."""

    checks: list[Check] = field(default_factory=list)
    run_id: int | None = None

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        """Append a :class:`Check` and return its ``passed`` flag (so a caller can
        branch on it)."""
        self.checks.append(Check(name, passed, detail))
        return passed

    @property
    def passed(self) -> bool:
        """True only if at least one check ran and every check passed."""
        return bool(self.checks) and all(c.passed for c in self.checks)


def verify(agent: str, repo: str, pr: int, *, conclusion: str = "success") -> Report:
    """Drive the full OBS02 funnel lifecycle on ``repo``#``pr`` and assert it.

    Mints the ``agent`` App installation token (asserting its granted
    ``permissions`` include ``checks: write``), resolves the canary PR's head sha,
    opens the kickoff check run via :func:`shipit.review.checkrun.create` (asserting
    a 201 — a run id came back, not a 403 — and that the run reads back
    ``in_progress`` with a ``started_at``), then closes that SAME run via
    :func:`shipit.review.checkrun.transition` to ``conclusion`` (asserting the same
    run id, ``completed`` status, the conclusion, an ``output`` message, and a
    ``completed_at``).

    Returns a :class:`Report`; it NEVER raises — every failure, an *assertion*
    miss OR a boundary error (``ReviewAuthError`` minting the App token,
    ``GhError`` on a REST call), becomes a recorded failed check so the harness
    always prints a PASS/FAIL report and exits 0/1. It stops early (returning the
    partial report) when a step's failure makes the rest meaningless (no token, no
    head sha, no created run).
    """
    report = Report()

    # 1. Token scope — the create-installation-token response carries the scopes
    #    GitHub actually granted this token. This is the direct read of "the
    #    checks:write re-grant + per-install consent landed for this owner". A mint
    #    failure (missing PyJWT, app not installed, API error) is recorded, not
    #    raised, so the harness still reports — there is nothing further to drive.
    try:
        auth = ghauth.installation_auth(agent, repo)
    except ghauth.ReviewAuthError as exc:
        report.record(
            "installation token granted checks: write",
            False,
            f"could not mint the installation token: {exc}",
        )
        return report
    perms = auth.get("permissions", {}) if isinstance(auth, dict) else {}
    report.record(
        "installation token granted checks: write",
        perms.get("checks") == "write",
        f"permissions.checks={perms.get('checks')!r}",
    )

    # The canary PR's head sha — the commit the funnel run attaches to. A `gh`
    # failure (repo/PR not accessible, auth) is recorded as the failed head-sha
    # check, not raised.
    try:
        head_sha = _pr_head_sha(repo, pr)
    except gh.GhError as exc:
        report.record("resolved canary PR head sha", False, f"{repo}#{pr}: {exc}")
        return report
    if not report.record(
        "resolved canary PR head sha",
        bool(head_sha),
        f"{repo}#{pr} head={head_sha!r}",
    ):
        return report
    assert head_sha is not None  # for the type checker; guarded by the record above

    # 2. Kickoff create — drive WS01's real code. A returned run id means
    #    POST .../check-runs was a 201; a missing checks:write scope would 403,
    #    which `gh` surfaces as a GhError (and a token mint can ReviewAuthError) —
    #    both caught here and recorded as the failed "201, not 403" check rather
    #    than crashing the harness.
    try:
        run_id = checkrun.create(agent, repo, head_sha)
    except (gh.GhError, ghauth.ReviewAuthError) as exc:
        report.record(
            "POST /repos/<repo>/check-runs returned 201 (not 403)",
            False,
            f"check-run create failed: {exc}",
        )
        return report
    report.run_id = run_id
    if not report.record(
        "POST /repos/<repo>/check-runs returned 201 (not 403)",
        run_id is not None,
        f"run_id={run_id}",
    ):
        return report
    assert run_id is not None  # guarded by the record above

    created = _read_run(report, repo, run_id, "kickoff")
    report.record(
        "kickoff run is in_progress",
        _field(created, "status") == "in_progress",
        f"status={_field(created, 'status')!r}",
    )
    report.record(
        "kickoff run has a started_at",
        bool(_field(created, "started_at")),
        f"started_at={_field(created, 'started_at')!r}",
    )

    # 3. Terminal transition — drive WS02's real code on the SAME run id. A
    #    transition (or read-back) boundary error is recorded, not raised, so the
    #    harness still prints a structured FAIL report.
    title = f"OBS02 funnel verify ({agent}-local)"
    summary = (
        "Lifecycle verification harness drove this run to its terminal "
        f"conclusion ({conclusion})."
    )
    try:
        checkrun.transition(
            agent, repo, run_id, conclusion=conclusion, title=title, summary=summary
        )
    except (gh.GhError, ghauth.ReviewAuthError) as exc:
        report.record(
            f"run conclusion is {conclusion}",
            False,
            f"terminal transition failed: {exc}",
        )
        return report
    closed = _read_run(report, repo, run_id, "terminal")
    report.record(
        "terminal transition hit the SAME run (no second run)",
        _field(closed, "id") == run_id,
        f"id={_field(closed, 'id')!r} (expected {run_id})",
    )
    report.record(
        "run is completed",
        _field(closed, "status") == "completed",
        f"status={_field(closed, 'status')!r}",
    )
    report.record(
        f"run conclusion is {conclusion}",
        _field(closed, "conclusion") == conclusion,
        f"conclusion={_field(closed, 'conclusion')!r}",
    )
    output = _field(closed, "output") or {}
    report.record(
        "run carries an output message",
        bool(output.get("title")) if isinstance(output, dict) else False,
        f"output.title={output.get('title')!r}"
        if isinstance(output, dict)
        else f"output={output!r}",
    )
    report.record(
        "run has a completed_at",
        bool(_field(closed, "completed_at")),
        f"completed_at={_field(closed, 'completed_at')!r}",
    )
    return report


def _pr_head_sha(repo: str, pr: int) -> str | None:
    """The head-commit sha of ``repo``#``pr`` (``GET /repos/{repo}/pulls/{pr}``)."""
    obj = gh.rest(f"/repos/{repo}/pulls/{pr}")
    head = obj.get("head") if isinstance(obj, dict) else None
    return head.get("sha") if isinstance(head, dict) else None


def _read_run(report: Report, repo: str, run_id: int, phase: str) -> object:
    """Read back run ``run_id``, recording a failed check (and returning ``{}``)
    on a `gh` boundary error so the downstream field assertions degrade to clean
    FAILs instead of crashing the harness."""
    try:
        return _get_run(repo, run_id)
    except gh.GhError as exc:
        report.record(f"read back the {phase} run", False, f"read failed: {exc}")
        return {}


def _get_run(repo: str, run_id: int) -> object:
    """The current state of check run ``run_id`` (``GET .../check-runs/{id}``)."""
    return gh.rest(f"/repos/{repo}/check-runs/{run_id}")


def _field(obj: object, key: str) -> object:
    """``obj[key]`` when ``obj`` is a dict, else ``None`` — a tolerant reader for
    the live GitHub responses the harness inspects."""
    return obj.get(key) if isinstance(obj, dict) else None


def format_report(report: Report, *, agent: str, repo: str, pr: int) -> str:
    """A clear, line-per-check PASS/FAIL block for the console."""
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        f"OBS02 funnel verification — {verdict}",
        f"  agent={agent}  repo={repo}  pr=#{pr}  run_id={report.run_id}",
        "",
    ]
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        line = f"  [{mark}] {check.name}"
        if check.detail:
            line += f"  ({check.detail})"
        lines.append(line)
    lines.append("")
    lines.append(f"OBS02 funnel verification — {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint: parse the canary target, run :func:`verify`, print the
    PASS/FAIL report, and exit ``0``/``1``. Refuses to run without an explicit
    ``--repo`` + ``--pr`` (or the env equivalents), so it can never fire by
    accident inside the test gate."""
    parser = argparse.ArgumentParser(
        prog="shipit-funnel-verify",
        description=(
            "OPT-IN live-GitHub verification of the OBS02 review funnel "
            "(kickoff create -> terminal transition) on a canary PR."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("SHIPIT_FUNNEL_CANARY_REPO"),
        help="owner/name of the canary repo (or SHIPIT_FUNNEL_CANARY_REPO).",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=_env_int("SHIPIT_FUNNEL_CANARY_PR"),
        help="canary PR number (or SHIPIT_FUNNEL_CANARY_PR).",
    )
    parser.add_argument(
        "--agent",
        default="codex",
        choices=sorted(ghauth._DOPPLER_KEYS),
        help="review agent whose App authors the run (default: codex).",
    )
    parser.add_argument(
        "--conclusion",
        default="success",
        choices=["success", "failure", "timed_out", "neutral"],
        help="terminal conclusion to drive the run to (default: success).",
    )
    args = parser.parse_args(argv)

    if not args.repo or not args.pr:
        parser.error(
            "a canary --repo and --pr are required (or set "
            "SHIPIT_FUNNEL_CANARY_REPO / SHIPIT_FUNNEL_CANARY_PR). This harness "
            "hits LIVE GitHub and is never run by the test gate."
        )

    report = verify(args.agent, args.repo, args.pr, conclusion=args.conclusion)
    print(format_report(report, agent=args.agent, repo=args.repo, pr=args.pr))
    return 0 if report.passed else 1


def _env_int(name: str) -> int | None:
    """Parse an int env var, or ``None`` when unset/blank/non-numeric."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
