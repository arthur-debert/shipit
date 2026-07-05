"""`shipit install` — vendor + reconcile the managed set, as glue + renderers.

The managed-unit domain lives in :mod:`shipit.install` (CLI02-WS01 promoted it
onto the ADR-0030 contract): :func:`~shipit.install.reconcile.gather` reads the
consumer, the pure :func:`~shipit.install.reconcile.reconcile` decides one
frozen :class:`~shipit.install.reconcile.Plan`, and
:func:`~shipit.install.apply.apply` is the only effectful path (writes,
retired-file unlinks, hook activation, git staging, PR creation), returning a
typed :class:`~shipit.install.apply.InstallResult`.

This module is ADR-0030 glue + renderers only:

- **params** — click validates the explicit primitives: PATH must be an
  existing directory (a usage error, exit 2, never verb-body code) and the
  three mode flags are mutually exclusive.
- **domain calls** — load the packaged desired state, gather → reconcile →
  Plan; dry-run stops there (rendered off the Plan, nothing touched);
  otherwise apply(Plan, mode) → InstallResult.
- **render** — the pure ``format_*`` functions below own every terminal line
  (the per-unit report, the retired delete/keep report and its kept-file
  warning, the nothing-to-do wording, the mode outcome) and the draft PR's
  body sections; the exit code derives from the result, with runtime failures
  (a git/gh :class:`~shipit.execrun.ExecError`, the domain's
  :class:`~shipit.install.errors.InstallError`) mapped by the one
  :func:`~._errors.cli_errors` shell (``error: …`` + exit 1).
"""

from __future__ import annotations

import difflib
import logging
import sys
from pathlib import Path

import click

from .. import events
from ..install.apply import (
    MODE_LOCAL,
    MODE_PR,
    MODE_PUSH,
    MODE_TREE,
    InstallResult,
)
from ..install.apply import (
    apply as apply_plan,
)
from ..install.reconcile import (
    ADD,
    DELETE,
    KEEP,
    NOOP,
    UPDATE,
    Plan,
    gather,
    load_retired,
    reconcile,
)
from ..install.units import Unit, load_units
from ._errors import cli_errors
from ._render import emit

logger = logging.getLogger("shipit.install")


@click.command(name="install")
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False))
@click.option(
    "--pr",
    is_flag=True,
    help="Stage the managed set on the `shipit/install` branch and open a DRAFT "
    "PR (the standalone onboarding/reconcile flow).",
)
@click.option(
    "--push",
    is_flag=True,
    help="Break-glass: commit and push straight to the branch (admin), no PR.",
)
@click.option(
    "--local",
    is_flag=True,
    help="Local-only: commit the managed set on the current branch; no push, no PR "
    "(used by `tree create` provisioning).",
)
@click.option(
    "--dry-run", is_flag=True, help="Print the reconciliation plan; touch nothing."
)
def cmd(path: str | None, pr: bool, push: bool, local: bool, dry_run: bool) -> None:
    """Vendor + reconcile shipit's managed set into the consumer at PATH.

    PATH defaults to the current directory. By default install refreshes the
    managed set IN THE WORKING TREE and stops — no commit, no branch, no push,
    no PR — so a mid-workstream refresh lands in the caller's own commit, never
    in a stray parallel PR (#359). Re-running with no changes is a clean no-op.

    ``--pr`` opts into the standalone reconcile flow: stage on the
    `shipit/install` branch and open a DRAFT PR (pull, never push); a
    consumer-edited unit is surfaced in the PR body rather than clobbered blind.

    ``--local`` commits the managed set on the current branch and stops (no push,
    no PR) — the mode Tree provisioning uses so creating a Tree never touches origin.
    """
    if sum((pr, push, local)) > 1:
        raise click.UsageError("--pr, --push, and --local are mutually exclusive.")
    raise SystemExit(run(path, dry_run=dry_run, pr=pr, push=push, local=local))


@cli_errors
def run(
    path: str | None = None,
    *,
    dry_run: bool = False,
    pr: bool = False,
    push: bool = False,
    local: bool = False,
    activate_hooks=None,
) -> int:
    """gather → reconcile → render the Plan → apply → render the result.

    Returns an int exit code: 0 on success (a no-op re-run and a dry-run
    included), with runtime failures — the domain's
    :class:`~shipit.install.errors.InstallError` refusals and any git/gh
    :class:`~shipit.execrun.ExecError` — mapped to ``error: …`` + exit 1 by the
    :func:`~._errors.cli_errors` shell.

    ``activate_hooks`` threads the injectable lefthook boundary through to
    :func:`shipit.install.apply.apply` (tests exercise the activation contract
    without mutating a real ``.git/hooks``).

    The run's milestones are dev-cycle events (#434, ADR-0032): ``install.started``
    at entry, ``install.completed`` on any clean exit (no-op and dry-run
    included), and — the reason this exists — ``install.failed`` carrying the
    failing step on the failure paths, so a failed run is legible in
    ``shipit logs --flow`` instead of leaving only a session-end record.
    """
    mode = MODE_LOCAL if local else MODE_PUSH if push else MODE_PR if pr else MODE_TREE
    root = str(Path(path or ".").resolve())
    events.emit(
        logger,
        "install.started",
        "install started in %s (mode=%s%s)",
        root,
        mode,
        ", dry-run" if dry_run else "",
        extra={"mode": mode, "dry_run": dry_run or None},
    )
    step = "gather/reconcile"
    try:
        units = load_units()
        retired = load_retired()
        state = gather(Path(path or "."), units, retired)
        plan = reconcile(units, retired, state)

        emit(plan, lambda p: format_plan(p, dry_run=dry_run))
        warnings = format_plan_warnings(plan)
        if warnings:
            print(warnings, file=sys.stderr)
        if plan.nothing_to_do or dry_run:
            # Dry-run has NO side effects (no writes, no deletes, no git, no PR);
            # a nothing-to-do plan is a clean no-op either way.
            events.emit(
                logger,
                "install.completed",
                "install completed in %s — nothing to do"
                if plan.nothing_to_do
                else "install completed in %s — dry-run",
                root,
                extra={"mode": mode},
            )
            return 0

        step = "apply"
        result = apply_plan(
            plan,
            mode,
            activate_hooks=activate_hooks,
            pr_body=lambda before, hooks, pin, debt: format_pr_body(
                plan, before, hooks, stamped_version=pin, lint_debt=debt
            ),
        )
    except Exception as exc:
        # The failure still propagates to the CLI error shell / the caller;
        # the event is the flow record's legibility, never a swallow (#434).
        events.emit(
            logger,
            "install.failed",
            "install failed at %s: %s",
            getattr(exc, "step", step),
            exc,
            extra={"step": getattr(exc, "step", step), "mode": mode},
        )
        raise
    emit(result, format_result)
    warnings = format_result_warnings(result)
    if warnings:
        print(warnings, file=sys.stderr)
    events.emit(
        logger,
        "install.completed",
        "install completed in %s (mode=%s)",
        root,
        mode,
        extra={"mode": mode},
    )
    return 0


# --------------------------------------------------------------------------
# Renderers — pure string functions over the Plan / InstallResult
# --------------------------------------------------------------------------


def format_plan(plan: Plan, *, dry_run: bool = False) -> str:
    """The reconciliation report: one line per decided change, off the Plan.

    Retired-file outcomes render alongside the managed results: a pristine copy
    is deleted, a locally modified copy is kept LOUDLY (the stderr warning is
    :func:`format_plan_warnings`), an absent path stays silent like any
    managed NOOP. A nothing-to-do plan says so — with the wording shifted when
    a kept retired file was just warned about, where "managed set is current"
    would read as a contradiction.

    Each line carries the unit's KEY, not its dest (#433): a file whose key is
    its path renders unchanged, while the marker blocks sharing one dest render
    with their block identity (``pixi.toml#shipit-lint-deps``) — the same names
    the ``.shipit.toml [managed]`` table uses — so three ``add pixi.toml``
    lines can never read as one repeated write.
    """
    lines = [f"install: {plan.root}{' (dry-run)' if dry_run else ''}"]
    for d in plan.decisions:
        if d.action != NOOP:
            lines.append(f"  {d.action:8} {d.unit.key}")
    if plan.seed_pixi_manifest:
        lines.append(
            f"  {'seed':8} pixi.toml ([workspace] table — consumer has no manifest)"
        )
    for item in plan.seeds:
        lines.append(f"  {'seed':8} {item}")
    for d in plan.retire_deletes:
        lines.append(f"  {DELETE:8} {d.retired.path} (retired)")
    for d in plan.retire_keeps:
        lines.append(f"  {KEEP:8} {d.retired.path} (retired; locally modified)")
    if plan.nothing_to_do:
        lines.append(
            "  nothing to do — no automated changes to apply."
            if plan.retire_keeps
            else "  nothing to do — managed set is current."
        )
    elif dry_run:
        lines.append(
            f"  ({len(plan.writes)} to write, {len(plan.overrides)} override(s), "
            f"{len(plan.seeds)} policy seed(s), {len(plan.retire_deletes)} retired "
            f"delete(s)) — dry-run, nothing written"
        )
    return "\n".join(lines)


def format_plan_warnings(plan: Plan) -> str:
    """The Plan's stderr lines: the unreadable manifest, each kept retired file."""
    lines = []
    if plan.manifest_error is not None:
        lines.append(f"install: ignoring unreadable manifest: {plan.manifest_error}")
    for d in plan.retire_keeps:
        lines.append(
            f"install: retired file kept: {d.retired.path} differs from every "
            f"known pristine version, so it was NOT deleted — shipit no longer "
            f"distributes this file; remove it yourself once your local edits "
            f"are no longer needed"
        )
    return "\n".join(lines)


def format_result(result: InstallResult) -> str:
    """The apply outcome: the pin stamp, the activation line (when live), and
    the mode's line. The pin gets its OWN line (#433 round-7): the stamp is the
    ADR-0033 lifecycle's payload, not a detail of the commit."""
    lines = []
    if result.stamped_version:
        lines.append(f"  pinned to {result.stamped_version}")
    if result.hooks_activated:
        lines.append("  activated git hooks (lefthook install) — the checks are live")
    if result.mode == MODE_TREE:
        lines.append(
            "  refreshed the managed set in the working tree — review with "
            "`git diff` and commit it with your own work (use --pr for the "
            "standalone reconcile draft PR)"
        )
    elif result.mode == MODE_LOCAL:
        lines.append(f"  committed to {result.branch} (local-only --local)")
    elif result.mode == MODE_PUSH:
        lines.append(f"  pushed to {result.branch} (break-glass --push)")
    elif result.pr_updated:
        lines.append(f"  updated draft PR: {result.pr_url}")
    else:
        lines.append(f"  opened draft PR: {result.pr_url}")
    return "\n".join(lines)


def format_result_warnings(result: InstallResult) -> str:
    """The apply's stderr lines: a failed activation, overrides refreshed in place."""
    lines = []
    if result.hooks_activated is False:
        lines.append(
            f"install: could not activate git hooks: {result.hooks_detail.strip()}"
        )
    if result.mode == MODE_TREE and result.plan.overrides:
        names = ", ".join(sorted(d.unit.dest for d in result.plan.overrides))
        lines.append(
            f"install: {len(result.plan.overrides)} consumer-edited unit(s) "
            f"overwritten with shipit's content in the working tree: {names} — "
            f"review `git diff` before committing (recover yours from git "
            f"history if the edit was committed)"
        )
    return "\n".join(lines)


def _desired_text(unit: Unit) -> str:
    return (
        unit.desired_inner() + "\n"
        if unit.kind == "block"
        else unit.content.decode("utf-8", errors="replace")
    )


def _override_diff(unit: Unit, consumer_text: str) -> str:
    """A unified diff of the consumer's edit vs shipit's intended content."""
    diff = difflib.unified_diff(
        consumer_text.splitlines(keepends=True),
        _desired_text(unit).splitlines(keepends=True),
        fromfile=f"{unit.dest} (consumer)",
        tofile=f"{unit.dest} (shipit)",
    )
    return "".join(diff)


def format_pr_body(
    plan: Plan,
    override_before: dict[str, str] | None = None,
    hooks_activated: bool | None = None,
    *,
    stamped_version: str | None = None,
    lint_debt: int | None = None,
) -> str:
    """The draft PR body: the stamped pin, what was added/updated (by unit KEY,
    #433 — block identity, never a bare repeated filename), every override with
    its diff, the retired delete/keep sections, the policy seed, the activation
    outcome, and the consumer's whole-tree lint debt (reported, never blocking).

    ``override_before`` holds each overridden unit's consumer content captured
    BEFORE the branch write (apply supplies it), so the diff shows the real
    divergence (not an empty diff against the content shipit just wrote over
    it). ``hooks_activated`` carries the real activation outcome so the body
    never claims a success that did not happen: ``None`` when the set has no
    checks to activate, ``True`` when ``lefthook install`` succeeded where
    install ran, ``False`` when it was skipped/failed (binary missing) and a
    merger must activate the checks themselves. ``stamped_version`` is the
    Shipit pin this install stamped (ADR-0033); ``lint_debt`` is the
    best-effort whole-tree failing-check count (``None`` = unreadable, ``0`` =
    green — only red debt renders a section).
    """
    override_before = override_before or {}
    adds = [d for d in plan.decisions if d.action == ADD]
    updates = [d for d in plan.decisions if d.action == UPDATE]

    lines = ["`shipit install` reconciled the managed set.", ""]
    if stamped_version:
        lines.append(
            f"Pinned to `{stamped_version}` — the build that wrote this managed "
            f"set and passed its self-certification (ADR-0033); the managed "
            f"`bin/shipit` launcher execs exactly this build."
        )
        lines.append("")
    if adds:
        lines.append("### Added")
        lines += [f"- `{d.unit.key}`" for d in adds]
        lines.append("")
    if updates:
        lines.append("### Updated")
        lines += [f"- `{d.unit.key}`" for d in updates]
        lines.append("")
    if plan.overrides:
        lines.append("### Overrides — consumer-edited, review before merging")
        lines.append(
            "These units were edited in the consumer since the last shipit install. "
            "This PR proposes restoring shipit's content (the diff below); **merging "
            "discards the consumer edit**. Review each diff and decide — closing the "
            "PR keeps the consumer's version."
        )
        lines.append("")
        for d in plan.overrides:
            lines.append(f"<details><summary><code>{d.unit.key}</code></summary>")
            lines.append("")
            lines.append("```diff")
            lines.append(
                _override_diff(d.unit, override_before.get(d.unit.key, "")).rstrip("\n")
            )
            lines.append("```")
            lines.append("</details>")
            lines.append("")
    if plan.retire_deletes:
        lines.append("### Retired files removed")
        lines.append(
            "shipit no longer distributes these files; each matched a known "
            "pristine version, so this PR deletes them:"
        )
        lines += [f"- `{d.retired.path}`" for d in plan.retire_deletes]
        lines.append("")
    if plan.retire_keeps:
        lines.append("### Retired files kept — locally modified")
        lines.append(
            "shipit no longer distributes these files, but their content "
            "differs from every known pristine version, so they were NOT "
            "deleted. Remove them yourself once the local edits are no "
            "longer needed:"
        )
        lines += [f"- `{d.retired.path}`" for d in plan.retire_keeps]
        lines.append("")
    if plan.seed_pixi_manifest:
        lines.append("### Pixi manifest seeded")
        lines.append(
            "The consumer had no `pixi.toml`, so this install seeded a minimal "
            "valid `[workspace]` table around the managed blocks (pixi requires "
            "one). The table is consumer-owned from here on — edit the name, "
            "channels, or platforms freely; a re-install never rewrites it."
        )
        lines.append("")
    if plan.seeds:
        lines.append("### Policy seeded")
        lines.append(
            "Consumer-owned pr-flow policy in `.shipit.toml` (seed-if-absent — "
            "existing entries are never clobbered, only absent ones are added):"
        )
        lines += [f"- `{s}`" for s in plan.seeds]
        lines.append("")
    if hooks_activated is True:
        lines.append("### Checks activated locally")
        lines.append(
            "`lefthook install` ran where this install was invoked, so its "
            "`.git/hooks/{pre-commit,pre-push}` fire `pixi run lint` there now. "
            "Reviewers/mergers: run `lefthook install` on your own checkout "
            "(shipit-self: `pixi run -e lint install-hooks`) to make the checks live "
            "for you too. Activation is idempotent and leaves unrelated hooks intact."
        )
        lines.append("")
    elif hooks_activated is False:
        lines.append("### Checks configured — local activation skipped")
        lines.append(
            "`lefthook.yml` is in this PR, but `lefthook install` did not run here "
            "(lefthook missing or it errored). After merging, run `lefthook install` "
            "(shipit-self: `pixi run -e lint install-hooks`) to activate the checks. "
            "The config is correct; only local activation was deferred."
        )
        lines.append("")
    if lint_debt:
        lines.append("### Consumer lint debt — reported, not blocking")
        lines.append(
            f"whole-tree lint currently red: {lint_debt} failing check(s) — "
            f"debt-clear pending. Install self-certified only the files it "
            f"delivered (ADR-0033); the whole-tree gate is this repo's bar "
            f"(the ADP01 checklist's lint step), cleared with the very env "
            f"this PR delivers."
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
