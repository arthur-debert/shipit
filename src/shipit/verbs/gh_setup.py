"""`shipit gh-setup` — ADR-0030 glue + renderer over the gh-setup domain.

The verb is click glue and a renderer (CLI02-WS04): the four setup passes
live in :mod:`shipit.ghsetup` and return one typed
:class:`~shipit.ghsetup.SetupReport`; this module resolves the ambient identity
(the root context — never a per-run API shellout), threads it as values, and
renders the report through the shared :func:`~._render.emit` seam (``--json``
serializes ``SetupReport.to_dict()``). One deliberate out-of-band write stays
outside that seam: the empty-checks warning goes to stderr (derived from the
report, kept off the result stream so ``--json`` output stays clean). The exit
code derives from the report — any failed secret makes the run rc 1 — with
runtime failures mapped by the one :func:`~._errors.cli_errors` shell
(``error: …`` + exit 1).

Dry-run renders off the SAME report shape: the domain performs no mutations
and the renderer shows exactly what would change, payload included.
"""

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

#: The out-of-band stderr warning for an empty required-checks set — kept on
#: stderr (not part of the rendered report) so piped/--json consumers still
#: see it without it polluting the result stream.
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
    """Make REPO conform to the portfolio standard (ruleset, labels, secrets)
    and verify the Actions access level of a private workflow publisher
    (warn-only — never set; #739).

    REPO is owner/name; omitted, it defaults to the current checkout's repo.
    Idempotent — safe to re-run for both install and update.
    """
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
    """Resolve identity → run the four passes → render. Returns an exit code.

    ``repo`` arrives as a value: the shared REPO argument mints it at parse
    (explicit slug) or defaults it to the ambient repo from the root context;
    a direct caller (a test) injects it. Omitted outside a checkout, the ONE
    uniform refusal (:class:`~._context.NoAmbientRepoError`) maps to
    ``error: …`` + exit 1 via the shell.

    0 when every pass applied (dry or real); 1 when any secret failed OR the
    ruleset pass refused (auto-discovery could not confidently name a PR
    workflow's checks — #1056) — the exit contract derives from the report. The
    workflow-access pass is advisory (verify-and-warn, #739): a warn or unknown
    outcome renders in the report but never changes the exit code. Local workflow
    auto-discovery is enabled only when the target IS the ambient checkout's
    repo; the config default is the ambient checkout's ``.shipit.toml`` either
    way.
    """
    ctx = current_root_context()
    target = repo if repo is not None else ctx.require_repo()
    wd = ctx.working_dir
    # Auto-discovery reads the target's own workflow files, so it needs the
    # target's local checkout. For a different remote target, pass --checks.
    local = wd.path if (wd is not None and target == wd.repo) else None
    cfg_path = config_path or str(Path(ctx.default_path()) / CONFIG_NAME)
    # The run's milestones are dev-cycle events (#434, ADR-0032): a gh-setup
    # that dies mid-pass leaves `ghsetup.failed` with the failing step in the
    # flow record instead of nothing at all. A completed run with failed
    # secrets is still `ghsetup.completed` — the report (and rc 1) carries
    # that outcome; `failed` is reserved for a run that could not finish.
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
        # A refusal is not the empty-gate case — it has its own rendered message
        # and a distinct exit code; the "pass --checks" nudge would double up.
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
    """The pure text renderer — the frozen gh-setup output, off the typed report.

    Dry-run and the real run render from the SAME shape; the only branches are
    per-outcome ``action`` values (the dry ruleset shows the full would-be
    payload; a dry secret shows its intended source).
    """
    lines = [f"gh-setup: {report.repo}{' (dry-run)' if report.dry_run else ''}"]

    rs = report.ruleset
    lines.append("ruleset:")
    if rs.action == "refused":
        # Auto-discovery could not confidently name a PR workflow's checks, so
        # the ruleset was NOT written (#1056) — show the actionable breakdown,
        # then fall through to the remaining passes.
        lines.append("  REFUSED — ruleset NOT written (auto-discovery uncertain)")
        for detail in (rs.refusal or "").splitlines():
            lines.append(f"  {detail}")
    else:
        if rs.list_error is not None:
            # Degraded path only: the listing failed and the pass assumed no
            # existing ruleset — say so, or "existing id: none" reads as verified.
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
        # The historical summary counts a dry secret as "set" — it is the
        # number of secrets the run WOULD push.
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
