"""The opt-in, live-GitHub verification harness for the review funnel: drive one
canary PR's check-run lifecycle end to end and assert every breadcrumb fact.

See docs/dev/review-app-provisioning.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

from .. import execrun, gh
from ..agent import backend as _agent_backend
from ..agent.backend import Backend
from . import checkrun, ghauth

logger = logging.getLogger("shipit.review")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    run_id: int | None = None

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, passed, detail))
        return passed

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def verify(
    backend: Backend, repo: str, pr: int, *, conclusion: str = "success"
) -> Report:
    """Drive the full funnel lifecycle on ``repo``#``pr`` and assert it. NEVER
    raises: a boundary error becomes a recorded failed check."""
    report = Report()

    try:
        auth = ghauth.installation_auth(backend, repo)
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

    try:
        head_sha = _pr_head_sha(repo, pr)
    except execrun.ExecError as exc:
        report.record("resolved canary PR head sha", False, f"{repo}#{pr}: {exc}")
        return report
    if not report.record(
        "resolved canary PR head sha",
        bool(head_sha),
        f"{repo}#{pr} head={head_sha!r}",
    ):
        return report
    assert head_sha is not None  # guarded by the record above

    # A returned run id means the POST was a 201; a missing checks:write scope
    # would 403, which `gh` surfaces as an ExecError.
    try:
        run_id = checkrun.create(backend, repo, head_sha)
    except (execrun.ExecError, ghauth.ReviewAuthError) as exc:
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

    title = f"OBS02 funnel verify ({backend.check_run_name})"
    summary = (
        "Lifecycle verification harness drove this run to its terminal "
        f"conclusion ({conclusion})."
    )
    try:
        checkrun.transition(
            backend, repo, run_id, conclusion=conclusion, title=title, summary=summary
        )
    except (execrun.ExecError, ghauth.ReviewAuthError) as exc:
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
    obj = gh.rest(f"/repos/{repo}/pulls/{pr}")
    head = obj.get("head") if isinstance(obj, dict) else None
    return head.get("sha") if isinstance(head, dict) else None


def _read_run(report: Report, repo: str, run_id: int, phase: str) -> object:
    """Records a failed check and returns ``{}`` rather than raising."""
    try:
        return _get_run(repo, run_id)
    except execrun.ExecError as exc:
        report.record(f"read back the {phase} run", False, f"read failed: {exc}")
        return {}


def _get_run(repo: str, run_id: int) -> object:
    return gh.rest(f"/repos/{repo}/check-runs/{run_id}")


def _field(obj: object, key: str) -> object:
    return obj.get(key) if isinstance(obj, dict) else None


def format_report(report: Report, *, agent: str, repo: str, pr: int) -> str:
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
    """Refuses to run without an explicit canary target, so it can never fire
    by accident inside the test checks."""
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
        choices=sorted(b.funnel_agent or "" for b in _agent_backend.funnel_backends()),
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
            "hits LIVE GitHub and is never run by the test checks."
        )

    backend = _agent_backend.by_funnel_agent(args.agent)
    report = verify(backend, args.repo, args.pr, conclusion=args.conclusion)
    print(format_report(report, agent=args.agent, repo=args.repo, pr=args.pr))
    return 0 if report.passed else 1


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
