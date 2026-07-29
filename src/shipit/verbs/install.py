"""`shipit install` — vendor + reconcile the managed set: glue + pure renderers.

See docs/adr/0030-cli-boundary-parse-to-values-typed-results.md.
"""

from __future__ import annotations

import difflib
import logging
import sys
import tomllib
from pathlib import Path

import click

from .. import config, events, gh, git, identity, sessionstore
from ..install import artifactdeps
from ..install import units as install_units
from ..install.apply import (
    MODE_LOCAL,
    MODE_PR,
    MODE_PUSH,
    MODE_TREE,
    InstallResult,
    reject_lefthook_conflicts,
    reject_pixi_key_conflicts,
    reject_stale_provision,
    reject_symlinked_dests,
)
from ..install.apply import (
    apply as apply_plan,
)
from ..install.reconcile import (
    ADD,
    DELETE,
    KEEP,
    LINK_BLOCKED,
    NOOP,
    UPDATE,
    Plan,
    detect_toolchains,
    format_claude_skills_link,
    format_lefthook_conflict,
    format_pixi_key_conflict,
    format_pixi_table_conflict,
    format_pixi_task_conflict,
    format_stale_provision,
    format_symlinked_dest,
    gather,
    load_retired,
    load_retired_hooks,
    reconcile,
)
from ..install.units import HOOK_RECOVERY_CMD, Unit, load_units
from ._errors import cli_errors
from ._render import emit
from ._tool import load_config

logger = logging.getLogger("shipit.install")


def _declared_signals(root: Path) -> set[str]:
    """Toolchain signals the consumer's `.shipit.toml` declarations need beyond its tracked manifests; an unparseable config raises ConfigError rather than declaring none."""
    from ..release import bundle as bundle_registry
    from ..tools import registry as toolchain_registry

    cfg = load_config(root)
    artifacts = config.load_artifacts(cfg)
    entries = config.load_toolchains(cfg)
    signals: set[str] = set()
    for artifact in artifacts:
        if artifact.bundle is None:
            continue
        comp = bundle_registry.composition(artifact.bundle.composition)
        if comp is not None and comp.provisions_signal is not None:
            signals.add(comp.provisions_signal)
    for entry in entries:
        tc = toolchain_registry.toolchain(entry.toolchain)
        if tc is not None and tc.provisions_signal is not None:
            signals.add(tc.provisions_signal)
    return signals


def _declared_endpoints(root: Path) -> frozenset[str]:
    """Distribution endpoints declared across the consumer's ``[artifacts.*]`` map."""
    cfg = load_config(root)
    artifacts = config.load_artifacts(cfg)
    endpoints: set[str] = set()
    for artifact in artifacts:
        endpoints.update(artifact.endpoints)
    return frozenset(endpoints)


def _declared_platforms(root: Path) -> frozenset[str]:
    """The consumer's declared pixi ``[workspace].platforms``; a manifest that declares none yields an empty set, an absent/unparseable one the seed defaults."""
    default = frozenset(install_units.PIXI_SEED_PLATFORMS)
    pixi = root / install_units.PIXI_FILE
    try:
        data = tomllib.loads(pixi.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return default
    for table in ("workspace", "project"):
        table_data = data.get(table)
        if not isinstance(table_data, dict):
            continue
        platforms = table_data.get("platforms")
        if isinstance(platforms, list):
            return frozenset(str(p) for p in platforms)
    return frozenset()


def _artifact_dep_units(root: Path, *, is_private=gh.repo_is_private) -> list[Unit]:
    """The managed pixi channel blocks projected from the consumer's ``[artifact-deps]``, or ``[]`` when none are declared."""
    cfg = load_config(root)
    deps = config.load_artifact_deps(cfg)
    if not deps:
        return []
    _require_consumer_pins(root, deps)
    visibility: dict[str, bool] = {}
    resolved = []
    for dep in deps:
        if dep.repo not in visibility:
            visibility[dep.repo] = is_private(dep.repo)
        resolved.append(
            (dep, artifactdeps.channel_url(dep.repo, private=visibility[dep.repo]))
        )
    return artifactdeps.project(resolved)


def _require_consumer_pins(root: Path, deps) -> None:
    """Raise unless every declared ``[artifact-deps.<pkg>]`` has its consumer-owned version pin in the artifact's pixi feature."""
    pixi = root / install_units.PIXI_FILE
    try:
        manifest = tomllib.loads(pixi.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        manifest = {}
    missing = artifactdeps.missing_pins(deps, manifest)
    if not missing:
        return
    lines = [
        f"`[artifact-deps.{dep.package}]` (from {dep.repo}) has no consumer-owned "
        f'version pin: add `{table}` `{dep.package} = "<version>"` to pixi.toml'
        for dep, table in missing
    ]
    raise config.ConfigError(
        "conda-direct (ADR-0077): the version of a cross-repo artifact is "
        "consumer-owned and must be pinned in the SAME pixi feature that carries "
        "its derived channel, so pixi resolves the pin against the channel. "
        "Missing pin(s):\n  - " + "\n  - ".join(lines)
    )


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
    """Vendor + reconcile shipit's managed set into the consumer at PATH."""
    if sum((pr, push, local)) > 1:
        raise click.UsageError("--pr, --push, and --local are mutually exclusive.")
    raise SystemExit(run(path, dry_run=dry_run, pr=pr, push=push, local=local))


def _consumer_root(path: str | None) -> tuple[Path, Path | None]:
    """The consumer root (the git working-tree root of PATH) and, when the call was redirected up from a subdirectory, that requested path."""
    requested = Path(path or ".").resolve()
    toplevel = git.repo_root(cwd=str(requested))
    if toplevel is None:
        return requested, None
    root = Path(toplevel).resolve()
    if root == requested:
        return requested, None
    return root, requested


def _plant_session_store(root: Path) -> None:
    """Link the canonical checkout's harness slug dir to the repo's session store; fail-open."""
    try:
        repo = identity.resolve_repo(str(root))
        sessionstore.plant(root, repo)
    except Exception:  # noqa: BLE001 — fail-open: never cost an install its exit code
        logger.debug("session store not planted for %s", root, exc_info=True)


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
    """gather → reconcile → render the Plan → apply → render the result; returns the exit code."""
    mode = MODE_LOCAL if local else MODE_PUSH if push else MODE_PR if pr else MODE_TREE
    root_path, redirected_from = _consumer_root(path)
    root = str(root_path)
    if redirected_from is not None:
        print(
            f"install: invoked from {redirected_from}, a subdirectory of the "
            f"git working tree; operating on the repo root {root_path} instead "
            f"of bootstrapping a nested consumer (#916). Pass the repo root as "
            f"PATH or `cd` to the repo root to silence this.",
            file=sys.stderr,
        )
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
        toolchains = detect_toolchains(root_path) | _declared_signals(root_path)
        endpoints = _declared_endpoints(root_path)
        platforms = _declared_platforms(root_path)
        units = load_units(
            toolchains=toolchains, endpoints=endpoints, platforms=platforms
        )
        units += _artifact_dep_units(root_path)
        retired = load_retired()
        retired_hooks = load_retired_hooks()
        state = gather(root_path, units, retired, retired_hooks)
        plan = reconcile(units, retired, state, retired_hooks)

        emit(plan, lambda p: format_plan(p, dry_run=dry_run))
        warnings = format_plan_warnings(plan)
        if warnings:
            print(warnings, file=sys.stderr)
        if not dry_run:
            step = "apply"
            reject_lefthook_conflicts(plan, mode)
            reject_symlinked_dests(plan)
            reject_stale_provision(plan)
            reject_pixi_key_conflicts(plan)
        if plan.nothing_to_do or dry_run:
            if not dry_run:
                _plant_session_store(root_path)
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
            pr_body=lambda before, hooks, rerendered, pin, debt: format_pr_body(
                plan,
                before,
                hooks,
                rerendered=rerendered,
                stamped_version=pin,
                lint_debt=debt,
            ),
        )
    except Exception as exc:
        events.emit(
            logger,
            "install.failed",
            "install failed at %s: %s",
            getattr(exc, "step", step),
            exc,
            extra={"step": getattr(exc, "step", step), "mode": mode},
        )
        raise
    _plant_session_store(root_path)
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


def format_plan(plan: Plan, *, dry_run: bool = False) -> str:
    """The reconciliation report: one line per decided change, keyed by unit KEY."""
    lines = [f"install: {plan.root}{' (dry-run)' if dry_run else ''}"]
    for d in plan.decisions:
        if d.action != NOOP:
            lines.append(f"  {d.action:8} {d.unit.key}")
    for key in plan.declined:
        lines.append(
            f"  {'decline':8} {key} (kept as this repo's own — "
            f".shipit.toml [managed.decline])"
        )
    if plan.seed_pixi_manifest:
        lines.append(
            f"  {'seed':8} pixi.toml ([workspace] table — consumer has no manifest)"
        )
    for item in plan.seeds:
        lines.append(f"  {'seed':8} {item}")
    if plan.rerender_changelog:
        lines.append(
            f"  {'render':8} CHANGELOG.md (stale against the current renderer "
            f"— regenerated from CHANGELOG/)"
        )
    for d in plan.retire_deletes:
        lines.append(f"  {DELETE:8} {d.retired.path} (retired)")
    for d in plan.retire_keeps:
        lines.append(f"  {KEEP:8} {d.retired.path} (retired; locally modified)")
    for d in plan.retire_hook_deletes:
        lines.append(f"  {DELETE:8} {d.retired.key} (retired hook entry)")
    if plan.claude_skills_link.is_work:
        lines.append(f"  {format_claude_skills_link(plan.claude_skills_link)}")
    if plan.pin_stale:
        before = plan.current_pin[:12] if plan.current_pin else "(pinless)"
        lines.append(f"  {'pin':8} {before} -> {plan.target_pin[:12]}")
    if plan.nothing_to_do:
        lines.append(
            "  nothing to do — no automated changes to apply."
            if plan.retire_keeps or plan.declined
            else "  nothing to do — managed set is current."
        )
    elif dry_run:
        lines.append(
            f"  ({len(plan.writes)} to write, {len(plan.overrides)} override(s), "
            f"{len(plan.seeds)} policy seed(s), "
            f"{len(plan.retire_deletes) + len(plan.retire_hook_deletes)} retired "
            f"delete(s)) — dry-run, nothing written"
        )
    return "\n".join(lines)


def format_plan_warnings(plan: Plan) -> str:
    """The Plan's stderr lines: unreadable manifest, kept retired files, and each undeliverable managed block."""
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
    for c in plan.lefthook_conflicts:
        lines.append(
            f"install: lefthook config conflict: {format_lefthook_conflict(c)}"
        )
    for kc in plan.pixi_key_conflicts:
        lines.append(f"install: pixi key conflict: {format_pixi_key_conflict(kc)}")
    for tc in plan.pixi_task_conflicts:
        lines.append(f"install: pixi block skipped: {format_pixi_task_conflict(tc)}")
    for bc in plan.pixi_table_conflicts:
        lines.append(f"install: pixi block skipped: {format_pixi_table_conflict(bc)}")
    for sd in plan.symlinked_dests:
        lines.append(f"install: symlinked dest: {format_symlinked_dest(sd)}")
    for sp in plan.stale_provision:
        lines.append(f"install: retired command: {format_stale_provision(sp)}")
    if plan.claude_skills_link.action == LINK_BLOCKED:
        lines.append(
            f"install: claude skills link: {format_claude_skills_link(plan.claude_skills_link)}"
        )
    for key in plan.decline_unmatched:
        lines.append(
            f"install: declined key {key!r} names no managed unit in this "
            f"catalog — check .shipit.toml [managed.decline].keep for a typo "
            f"(unit keys are the [managed] table's names; toolchain-conditional "
            f"pixi blocks only join the catalog when their signal manifest is "
            f"tracked)"
        )
    return "\n".join(lines)


def format_result(result: InstallResult) -> str:
    """The apply outcome: the pin stamp, the activation line, and the mode's line."""
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
    elif result.pr_url:
        lines.append(f"  opened draft PR: {result.pr_url}")
    else:
        lines.append(
            "  the managed set is already current on the default branch — "
            "nothing to publish (no draft PR needed)"
        )
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
    rerendered: bool = False,
    stamped_version: str | None = None,
    lint_debt: int | None = None,
) -> str:
    """The draft PR body. ``override_before`` holds each overridden unit's content captured BEFORE the branch write; ``hooks_activated`` is None when there was nothing to activate; ``lint_debt`` None means unreadable."""
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
    if plan.declined:
        lines.append("### Declined units — kept as this repo's own")
        lines.append(
            "This repo's `.shipit.toml` `[managed.decline].keep` declines these "
            "managed units (#600), so this install did not deliver them and no "
            "override is proposed — the committed copies stay authoritative. To "
            "adopt shipit's content again, remove the entry and re-run "
            "`shipit install`."
        )
        lines += [f"- `{key}`" for key in plan.declined]
        lines.append("")
    if plan.retire_deletes:
        lines.append("### Retired files removed")
        lines.append(
            "shipit no longer distributes these files; each matched a known "
            "pristine version, so this PR deletes them:"
        )
        lines += [f"- `{d.retired.path}`" for d in plan.retire_deletes]
        lines.append("")
    if plan.retire_hook_deletes:
        lines.append("### Retired hook entries removed")
        lines.append(
            "shipit no longer prescribes these consumer-local hook entries "
            "(each identified by the command it runs; shipit's own managed "
            "entries are never touched), so this install removes them from their "
            "hooks file — unless that file can't be safely read or written, in "
            "which case it is left untouched with a warning logged and the entry "
            "may remain:"
        )
        lines += [f"- `{d.retired.key}`" for d in plan.retire_hook_deletes]
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
    if rerendered:
        lines.append("### Changelog re-rendered")
        lines.append(
            "The committed `CHANGELOG.md` no longer matched a re-render of "
            "`CHANGELOG/` with the current renderer (`shipit changelog check` "
            "was failing), so this install regenerated it. The fragments stay "
            "authoritative — nothing was added or removed, only the rendered "
            "projection refreshed."
        )
        lines.append("")
    if hooks_activated is True:
        lines.append("### Checks activated locally")
        lines.append(
            "`lefthook install` ran where this install was invoked, so its "
            "`.git/hooks/{pre-commit,pre-push}` fire `pixi run -e lint lint` there "
            "now — the same task, in the same pinned env, a bare `pixi run lint` "
            "resolves to. "
            f"Reviewers/mergers: run `{HOOK_RECOVERY_CMD}` on your own checkout "
            "(shipit-self: `pixi run -e lint install-hooks`) to make the checks live "
            "for you too. Activation is idempotent and leaves unrelated hooks intact."
        )
        lines.append("")
    elif hooks_activated is False:
        lines.append("### Checks configured — local activation skipped")
        lines.append(
            "`lefthook.yml` is in this PR, but `lefthook install` did not run here "
            f"(lefthook missing or it errored). After merging, run `{HOOK_RECOVERY_CMD}` "
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
