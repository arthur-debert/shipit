"""`shipit gh-setup` — click glue and renderer over the gh-setup domain."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from .. import events
from ..config import CONFIG_NAME
from ..ghsetup import SetupReport, setup
from ..identity import Repo
from ._context import current_root_context
from ._errors import cli_errors
from ._params import dry_run_option, json_option, repo_argument
from ._render import emit

logger = logging.getLogger("shipit.ghsetup")

_NO_CHECKS_WARNING = (
    "  warning: no required checks found — the ruleset carries no "
    "required-status-checks gate (the API rejects an empty set). "
    "Pass --checks a,b to set them explicitly."
)


@click.command(name="gh-setup")
@repo_argument
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to .shipit.toml (default: the repo root's).",
)
@click.option(
    "--checks",
    "checks",
    default=None,
    help="Comma-separated required checks (skip auto-discovery).",
)
@dry_run_option
@json_option
def cmd(
    repo: Repo | None,
    config_path: str | None,
    checks: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Make REPO conform to the portfolio standard (ruleset, labels, secrets)."""
    checks_override = (
        [c.strip() for c in checks.split(",") if c.strip()]
        if checks is not None
        else None
    )
    raise SystemExit(
        run(
            repo,
            config_path=config_path,
            checks_override=checks_override,
            dry_run=dry_run,
            as_json=as_json,
            prompt=lambda name: click.prompt(f"secret {name}", hide_input=True),
        )
    )


@cli_errors
def run(
    repo: Repo | None = None,
    *,
    config_path: str | None = None,
    checks_override: list[str] | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    prompt=None,
) -> int:
    """Run the four gh-setup passes and render the report; returns an exit code."""
    ctx = current_root_context()
    target = repo if repo is not None else ctx.require_repo()
    wd = ctx.working_dir
    local = wd.path if (wd is not None and target == wd.repo) else None
    cfg_path = config_path or str(Path(ctx.default_path()) / CONFIG_NAME)
    events.emit(
        logger,
        "ghsetup.started",
        "gh-setup started for %s%s",
        target.slug,
        " (dry-run)" if dry_run else "",
        extra={"dry_run": dry_run or None},
    )
    try:
        report = setup(
            target,
            checks_override=checks_override,
            local_checkout=local,
            config_path=cfg_path,
            dry_run=dry_run,
            prompt=prompt,
        )
    except Exception as exc:
        events.emit(
            logger,
            "ghsetup.failed",
            "gh-setup failed for %s: %s",
            target.slug,
            exc,
            extra={"step": "setup (ruleset/labels/access/secrets)"},
        )
        raise
    if report.ruleset_refused:
        print(f"  error: {report.ruleset.refusal}", file=sys.stderr)
    elif not report.ruleset.checks:
        print(_NO_CHECKS_WARNING, file=sys.stderr)
    emit(report, format_setup, as_json=as_json)
    events.emit(
        logger,
        "ghsetup.completed",
        "gh-setup completed for %s (%d secret failure(s))",
        target.slug,
        report.secrets_failed,
        extra={
            "secrets_failed": report.secrets_failed or None,
            "ruleset_refused": report.ruleset_refused or None,
        },
    )
    return 1 if report.secrets_failed or report.ruleset_refused else 0


def format_setup(report: SetupReport) -> str:
    """The frozen gh-setup output, rendered off the typed report."""
    lines = [f"gh-setup: {report.repo}{' (dry-run)' if report.dry_run else ''}"]

    rs = report.ruleset
    lines.append("ruleset:")
    if rs.action == "refused":
        lines.append("  REFUSED — ruleset NOT written (auto-discovery uncertain)")
        for detail in (rs.refusal or "").splitlines():
            lines.append(f"  {detail}")
    else:
        if rs.list_error is not None:
            lines.append(
                "  warning: could not list rulesets — assumed none exists"
                f" ({rs.list_error})"
            )
        lines.append(
            f"  ruleset: {rs.name} (existing id: "
            f"{rs.existing_id if rs.existing_id is not None else 'none'})"
        )
        lines.append(f"  checks:  {', '.join(rs.checks) if rs.checks else '(none)'}")
        if rs.action == "dry-run":
            lines.append("  --- payload (dry-run, not sent) ---")
            lines.append(json.dumps(rs.payload, indent=2))
        else:
            lines.append(f"  ruleset {rs.action}")

    lines.append("labels:")
    for label in report.labels:
        prefix = "[dry] label" if label.action == "dry-run" else "label"
        lines.append(f"  {prefix} {label.name}")

    wa = report.workflow_access
    lines.append("workflow access:")
    if wa.status == "warn":
        lines.append(f"  WARN {wa.reason}")
    elif wa.status == "unknown":
        lines.append(f"  warning: {wa.reason}")
    elif wa.status == "acceptable":
        lines.append(f"  access level: {wa.access_level} (acceptable)")
    elif wa.status == "not-applicable":
        lines.append(f"  not applicable: {wa.reason}")
    else:
        raise ValueError(f"unknown workflow access status: {wa.status!r}")

    lines.append("secrets:")
    if report.secrets_error is not None:
        lines.append(f"  no secrets applied: {report.secrets_error}")
    for secret in report.secrets:
        if secret.action == "dry-run":
            lines.append(f"  [dry] secret {secret.name} (from {secret.source})")
        elif secret.action == "failed":
            lines.append(f"  FAIL {secret.name}: {secret.reason}")
        elif secret.action == "skipped":
            lines.append(f"  skip {secret.name} ({secret.reason})")
        elif secret.action == "orphan":
            lines.append(f"  ORPHAN {secret.name}: {secret.reason}")
        else:
            lines.append(f"  secret {secret.name}")
    if report.secrets:
        would_set = sum(1 for s in report.secrets if s.action in ("set", "dry-run"))
        summary = (
            f"  {would_set} secret(s) set, "
            f"{report.secrets_skipped} skipped, {report.secrets_failed} failed"
        )
        if report.secrets_orphaned:
            summary += f", {report.secrets_orphaned} orphaned"
        lines.append(summary)

    lines.append("done.")
    return "\n".join(lines)
