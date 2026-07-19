"""`shipit install` — vendor + reconcile the managed set, as glue + renderers.

The managed-unit domain lives in :mod:`shipit.install` (CLI02-WS01 promoted it
onto the ADR-0030 contract): :func:`~shipit.install.reconcile.gather` reads the
consumer, the pure :func:`~shipit.install.reconcile.reconcile` decides one
frozen :class:`~shipit.install.reconcile.Plan`, and
:func:`~shipit.install.apply.apply` is the only effectful path (writes,
retired-file unlinks, retired-hook-entry removals, hook activation, git
staging, PR creation), returning a
typed :class:`~shipit.install.apply.InstallResult`.

This module is ADR-0030 glue + renderers only:

- **params** — click validates the explicit primitives: PATH must be an
  existing directory (a usage error, exit 2, never verb-body code) and the
  three mode flags are mutually exclusive.
- **domain calls** — load the packaged desired state, gather → reconcile →
  Plan; dry-run stops there (rendered off the Plan, nothing touched);
  otherwise apply(Plan, mode) → InstallResult. Every non-dry-run path then
  plants the canonical checkout's session-store link
  (:func:`_plant_session_store`, ADR-0073 — the one effect that is NOT a
  managed unit: it writes outside the consumer, under ``~/.claude``, and is
  fail-open rather than part of the Plan). *Every* includes the nothing-to-do
  return: the link is outside the Plan, so a current managed set is no evidence
  the link exists.
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
import tomllib
from pathlib import Path

import click

from .. import config, events, gh, git, identity, sessionstore
from ..channel import cascade_receive
from ..install import artifactdeps
from ..install import units as install_units
from ..install.apply import (
    MODE_LOCAL,
    MODE_PR,
    MODE_PUSH,
    MODE_TREE,
    InstallResult,
    reject_lefthook_conflicts,
    reject_symlinked_dests,
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
    detect_toolchains,
    format_lefthook_conflict,
    format_pixi_key_conflict,
    format_pixi_table_conflict,
    format_pixi_task_conflict,
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
    """Toolchain signals the consumer's DECLARATIONS need beyond its tracked
    manifests (issue #788 review; #890).

    :func:`~shipit.install.reconcile.detect_toolchains` reads manifests only
    (a tracked ``package.json`` → the node signal that delivers ``npm``).
    Two declaration surfaces union more signals off ``.shipit.toml``:

    - a declared BUNDLE COMPOSITION (#788): a ``wasm-pack`` composition runs
      ``npm pack`` at bundle (:mod:`shipit.release.bundle`) so it NEEDS
      ``npm``, yet it rides the RUST signal and the crate's npm
      ``package.json`` is generated into ``pkg/``, never tracked — a
      rust-only wasm crate would get ``wasm-pack`` without ``npm`` and fail
      the bundle. Each registry entry names the signal it provisions
      (:attr:`shipit.release.bundle.Composition.provisions_signal`), read off
      the artifact map here so the node-deps block ships wherever the
      composition is declared;
    - a declared TOOLCHAIN leg (#890): a tree-sitter grammar has NO manifest
      for the walk to find, so its ``[toolchains]`` declaration is the only
      signal — the registry entry names the signal its own CLI rides
      (:attr:`shipit.tools.registry.Toolchain.provisions_signal`), delivering
      the ``tree-sitter-cli`` block wherever a tree-sitter leg is declared.

    Degrades to ``set()`` when the config is absent or unparseable — the
    toolchain augmentation never itself fails install (the config's own parse
    errors surface on the verbs that read the map, not here).
    """
    from ..release import bundle as bundle_registry  # lazy — keep install import-light
    from ..tools import registry as toolchain_registry

    try:
        cfg = load_config(root)
        artifacts = config.load_artifacts(cfg)
        entries = config.load_toolchains(cfg)
    except config.ConfigError:
        return set()
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
    """Distribution endpoints declared across the consumer's ``[artifacts.*]``
    map (#1071).

    The endpoint-gated managed pixi blocks (:data:`shipit.install.units.ENDPOINT_UNITS`
    — currently the conda packager) ride a declared ENDPOINT, not a toolchain:
    ``rattler-build`` is the ``conda`` endpoint's packager, so it must ship
    wherever ANY artifact names ``conda`` (regardless of its composition or
    build toolchain), the #1071 gap where a non-rust conda producer got no
    packager. Returns the union of every artifact's ``endpoints`` list.

    Degrades to ``frozenset()`` when the config is absent or unparseable — the
    endpoint augmentation never itself fails install (the config's own parse
    errors surface on the verbs that read the map, not here), the same posture
    as :func:`_declared_signals`.
    """
    try:
        cfg = load_config(root)
        artifacts = config.load_artifacts(cfg)
    except config.ConfigError:
        return frozenset()
    endpoints: set[str] = set()
    for artifact in artifacts:
        endpoints.update(artifact.endpoints)
    return frozenset(endpoints)


def _declared_platforms(root: Path) -> frozenset[str]:
    """The consumer's declared pixi ``[workspace].platforms`` (#1072).

    The managed lexd block scopes its ``[target]`` tables to the platforms the
    workspace actually declares (:func:`shipit.install.units.lexd_block`) — a
    ``[target]`` selector for an undeclared platform makes pixi warn on every
    invocation. This reads that platform list from the consumer's ``pixi.toml``
    (``[workspace]``, or the legacy ``[project]`` alias).

    The scope is the repo's OWN declaration, so an EXISTING manifest names only
    what it names:

    - NO ``pixi.toml`` (or an unparseable one) degrades to the seed defaults
      (:data:`shipit.install.units.PIXI_SEED_PLATFORMS`) — exactly the set a
      fresh install is about to SEED (:func:`shipit.install.units.pixi_manifest_seed`),
      so the virgin-repo plan's lexd scope matches the platforms it writes and a
      re-install reconciles to a clean noop (the seed set carries no ``win-64``);
    - a PRESENT, parsed manifest that declares no ``platforms`` list is NOT a
      virgin repo — it gets ``frozenset()``, never the seed defaults, because
      emitting a target for a platform the manifest never declared is the exact
      #1072 dangling-selector warning this scoping removes (the reviewer's
      existing-manifest shape). An EXPLICIT list wins verbatim, including an
      explicitly empty ``platforms = []`` (the consumer declared none).

    A ``[workspace]``/``[project]`` that is a scalar rather than a table (invalid
    schema, but valid TOML) is treated as no platform source, never crashed on.
    """
    default = frozenset(install_units.PIXI_SEED_PLATFORMS)
    pixi = root / install_units.PIXI_FILE
    try:
        data = tomllib.loads(pixi.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return default
    for table in ("workspace", "project"):
        table_data = data.get(table)
        if not isinstance(table_data, dict):
            continue  # absent, or a malformed scalar — not a platform source
        platforms = table_data.get("platforms")
        if isinstance(platforms, list):
            return frozenset(str(p) for p in platforms)
    return frozenset()


def _artifact_dep_units(root: Path, *, is_private=gh.repo_is_private) -> list[Unit]:
    """The managed pixi blocks projected from the consumer's ``[artifact-deps]``
    declarations (ARF01-WS02 #952), or ``[]`` when none are declared.

    The consumer half of the Artifact channel: each ``[artifact-deps.<pkg>]``
    entry is parsed to a typed :class:`~shipit.config.ArtifactDep`
    (construction-is-validation — a MALFORMED entry fails loudly HERE, exactly
    like the toolchain/artifact maps, so ``shipit install`` aborts rather than
    projecting a broken block), and the pure network-free projection core
    (:func:`shipit.install.artifactdeps.project`) turns the resolved pins into
    managed blocks the reconcile then treats like any other.

    The ONLY network read is the producing repo's VISIBILITY (``gh.repo_is_private``,
    injectable for tests) — the access tier is DERIVED from it (ADR-0065), never
    declared; it is resolved once per distinct producing repo, and ONLY when a
    dep is actually declared, so a repo with no ``[artifact-deps]`` (shipit's own
    included) stays fully offline. A generally-unreadable manifest DEGRADES to no
    artifact units (gather warns about it downstream, matching the rest of
    install); only a well-formed manifest carrying a malformed ``[artifact-deps]``
    entry fails loud.
    """
    try:
        cfg = load_config(root)
    except config.ConfigError:
        # A generally-unreadable manifest degrades like gather's read boundary —
        # no artifact units; the warning surfaces there. A malformed
        # [artifact-deps] entry, by contrast, is a parse error load_config never
        # raises (it validates only table shape), so it still fails loud below.
        return []
    deps = config.load_artifact_deps(cfg)
    if not deps:
        return []
    visibility: dict[str, bool] = {}
    resolved = []
    for dep in deps:
        if dep.repo not in visibility:
            visibility[dep.repo] = is_private(dep.repo)
        resolved.append(
            (dep, artifactdeps.channel_url(dep.repo, private=visibility[dep.repo]))
        )
    # The consumer half also carries the receive-workflow (ARF01-WS07 #956): a
    # repo that declares a cross-repo pin gets the managed workflow that, on the
    # producer's release cascade, bumps the pin and opens a draft PR. Delivered
    # ONLY when `[artifact-deps]` exist, so a repo with no pin never carries a
    # dead cascade workflow; reconciled like every other whole-file unit.
    return [cascade_receive.receive_workflow_unit(), *artifactdeps.project(resolved)]


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

    PATH defaults to the current directory. A consumer lives at its git root,
    so when PATH (or the cwd) sits inside a git working tree BELOW its root,
    install redirects UP to that root rather than bootstrapping a nested
    consumer at the subdirectory (#916); a standalone non-git directory
    bootstraps in place as before. By default install refreshes the
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


def _consumer_root(path: str | None) -> tuple[Path, Path | None]:
    """Resolve the consumer root install operates on, redirecting a
    subdirectory invocation up to the git working-tree root (#916).

    A consumer lives at its git ROOT: ``.shipit.toml``, the managed set, and
    the activated hooks all hang off the top of the checkout. Running install
    from a SUBDIRECTORY used to bootstrap a brand-new nested consumer rooted at
    the cwd (a fresh ``pixi.toml`` / ``.shipit.toml`` seeded and pinned from
    pinless), which is never the intent and is a footgun — a duplicate managed
    set, a stray pin, and a polluted ``git status`` inside the real repo.

    So the effective root is the git working-tree root of the requested path
    whenever that path sits inside a checkout:

    - a SUBDIRECTORY invocation is redirected UP to the root — the second
      element of the return is the requested path, so the caller can announce
      the redirect on stderr rather than acting silently;
    - a ROOT or virgin-repo invocation is unchanged (the toplevel equals the
      requested path, so the redirect field is ``None``);
    - a path that is not inside any git checkout (a genuinely standalone
      directory) bootstraps at the requested path exactly as before — there is
      no working-tree root to redirect to.
    """
    requested = Path(path or ".").resolve()
    toplevel = git.repo_root(cwd=str(requested))
    if toplevel is None:
        return requested, None
    root = Path(toplevel).resolve()
    if root == requested:
        return requested, None
    return root, requested


def _plant_session_store(root: Path) -> None:
    """Link the CANONICAL checkout's harness slug dir to the repo's session store (ADR-0073).

    The other half of :func:`shipit.tree.create._plant_session_store`: every Tree links
    itself at birth, and install links the plain checkout, so work done in a Tree and
    work done in the canonical checkout share one store rather than splitting into two.

    Runs on EVERY non-dry-run path — after ``apply``, and equally on the nothing-to-do
    return. The link is not a managed unit and so is not in the Plan: a current managed
    set implies nothing about whether the slug dir is linked, and the already-managed
    checkout — whose plan is nothing-to-do — is exactly the migration case this seam
    exists for. Only ``--dry-run`` plants nothing, which is its no-side-effects contract.

    The canonical checkout is the case the ADR calls hard and common: its slug dir
    typically **already exists as a real directory with real content**, so this is the
    call that actually exercises adoption. :func:`shipit.sessionstore.plant` is
    content-preserving and idempotent, so re-running install is free.

    **Fail-open at DEBUG** (#348 calibration), for the same reason as the Tree seam: the
    store is additive, nothing durable degrades without it, and an environment with no
    ``~/.claude`` at all must not WARN on every install. A *refusal* is already logged
    loudly by ``plant`` itself — that one IS durable degraded state.
    """
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
    # A consumer lives at its git root; a subdirectory invocation is redirected
    # UP to that root rather than bootstrapping a nested consumer (#916).
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
        # The catalog is signal-conditional (#547 Layer 1): a consumer whose
        # tracked manifests declare a toolchain (Cargo.toml/go.mod/package.json)
        # gets the matching pinned pixi dep block alongside the unconditional set.
        # Declarations union more signals — a wasm-pack bundle composition needs
        # npm for its `npm pack`, which no tracked manifest signals (issue #788),
        # and a declared tree-sitter [toolchains] leg needs its own CLI, which no
        # manifest can signal at all (#890).
        toolchains = detect_toolchains(root_path) | _declared_signals(root_path)
        # The conda packager (rattler-build) is gated on a declared ENDPOINT,
        # not a toolchain (#1071): a repo declaring a `conda` endpoint on any
        # artifact gets it regardless of its build toolchain, so a non-rust
        # conda producer (a tree-sitter `tarball` grammar) is no longer starved
        # of its packager.
        endpoints = _declared_endpoints(root_path)
        # The managed lexd block's `[target]` set is scoped to the consumer's
        # declared platforms (#1072): a target selector for a platform the
        # workspace does not declare makes pixi warn on every invocation.
        platforms = _declared_platforms(root_path)
        units = load_units(
            toolchains=toolchains, endpoints=endpoints, platforms=platforms
        )
        # The consumer half of the Artifact channel (ARF01-WS02 #952): project
        # the repo's `[artifact-deps]` declarations into managed pixi blocks the
        # reconcile then treats like any other. Malformed entries fail loud here;
        # a repo declaring none (shipit's own) stays offline.
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
            # Fail closed on a #544 lefthook conflict BEFORE the no-op shortcut
            # can swallow it: a committing-mode run whose ONLY finding is the
            # conflict (managed set already current — the future-regression
            # shape) has an empty write set, so `nothing_to_do` would otherwise
            # exit 0 and never reach apply()'s guard. MODE_TREE is a no-op here
            # (warn-only, like below); dry-run previews without side effects, so
            # it stays on the early-return path and never refuses.
            step = "apply"
            reject_lefthook_conflicts(plan, mode)
            # Fail closed on a symlinked dest in EVERY applying mode (MODE_TREE
            # included, unlike lefthook): the breach is the raw filesystem write,
            # not a config publish. Placed before the no-op shortcut so a plan
            # whose only finding is the symlink (its unit excluded from the write
            # set) still refuses rather than exiting 0.
            reject_symlinked_dests(plan)
        if plan.nothing_to_do or dry_run:
            # Dry-run has NO side effects (no writes, no deletes, no git, no PR);
            # a nothing-to-do plan is a clean no-op either way.
            if not dry_run:
                # ...but a no-op MANAGED SET is not a no-op STORE: the link lives
                # outside the Plan, so "every managed file is current" says nothing
                # about whether the slug dir is linked. Planting here is what makes
                # the advertised install-based migration reachable — the common
                # migration case IS an already-managed checkout, whose plan is
                # nothing-to-do, and gating the link on unrelated managed-file drift
                # would mean the store gets adopted only by coincidence.
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


# --------------------------------------------------------------------------
# Renderers — pure string functions over the Plan / InstallResult
# --------------------------------------------------------------------------


def format_plan(plan: Plan, *, dry_run: bool = False) -> str:
    """The reconciliation report: one line per decided change, off the Plan.

    Retired-file outcomes render alongside the managed results: a pristine copy
    is deleted, a locally modified copy is kept LOUDLY (the stderr warning is
    :func:`format_plan_warnings`), an absent path stays silent like any
    managed NOOP. A retired hook ENTRY (#619) renders its delete line the same
    way (there is no keep case — the match itself protects shipit's own
    managed entries). A DECLINED unit (#600) renders its standing ``decline`` line
    on every run — the decision must stay visible in-repo, never silently
    absorbed like a NOOP. A nothing-to-do plan says so — with the wording
    shifted when a kept retired file or a declined unit was just listed, where
    "managed set is current" would read as a contradiction.

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
    for key in plan.declined:
        # #600: the standing consumer decision, rendered every run so it stays
        # visible in-repo — the unit is skipped, never written or re-proposed.
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
        # #578: the committed projection went stale against a renderer change;
        # this install regenerates it — a plan line like any other write.
        lines.append(
            f"  {'render':8} CHANGELOG.md (stale against the current renderer "
            f"— regenerated from CHANGELOG/)"
        )
    for d in plan.retire_deletes:
        lines.append(f"  {DELETE:8} {d.retired.path} (retired)")
    for d in plan.retire_keeps:
        lines.append(f"  {KEEP:8} {d.retired.path} (retired; locally modified)")
    for d in plan.retire_hook_deletes:
        # #619: a consumer-local hook entry shipit used to prescribe — removed
        # from its event array; shipit's own managed entries are never touched.
        lines.append(f"  {DELETE:8} {d.retired.key} (retired hook entry)")
    if plan.pin_stale:
        # ADR-0033: a pin roll-forward is a reconcile outcome in its own right —
        # it can be the ONLY change when a code-only shipit build ships (every
        # managed file byte-identical), so it earns a plan line like any write.
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
    """The Plan's stderr lines: the unreadable manifest, each kept retired
    file, each lefthook merge conflict (#544 — the committing modes also
    fail closed on these in apply; the warning is the working-tree/dry-run
    surface, worded off the same formatter so the two can never drift), and
    each pixi block skipped over a consumer-owned duplicate key (#547) or a
    consumer-owned same-named task (TOL01-WS01 — a pixi-task ambiguity; both
    warn-only in every mode: the skip already keeps the write set safe), each
    whole-file unit whose dest crosses a consumer symlink (#1088 review — EVERY
    applying mode also fails closed on these in apply/verb; the warning is the
    dry-run surface, worded off the same formatter so the two can never drift),
    and each declined key that names no unit in this catalog (#600 — a typo must
    not silently decline nothing)."""
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
        lines.append(f"install: pixi block skipped: {format_pixi_key_conflict(kc)}")
    for tc in plan.pixi_task_conflicts:
        lines.append(f"install: pixi block skipped: {format_pixi_task_conflict(tc)}")
    for bc in plan.pixi_table_conflicts:
        lines.append(f"install: pixi block skipped: {format_pixi_table_conflict(bc)}")
    for sd in plan.symlinked_dests:
        lines.append(f"install: symlinked dest: {format_symlinked_dest(sd)}")
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
    elif result.pr_url:
        lines.append(f"  opened draft PR: {result.pr_url}")
    else:
        # MODE_PR with no PR: after the staging branch was reset onto the current
        # default the managed set already matched the base, so nothing was
        # published (#852 review — no crash on an empty commit).
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
    """The draft PR body: the stamped pin, what was added/updated (by unit KEY,
    #433 — block identity, never a bare repeated filename), every override with
    its diff, the declined units (#600 — kept as the repo's own, the standing
    `.shipit.toml [managed.decline]` decision), the retired delete/keep
    sections (files AND hook entries, #619), the policy seed, the changelog
    re-render (#578), the activation
    outcome, and the consumer's whole-tree lint debt (reported, never blocking).

    ``override_before`` holds each overridden unit's consumer content captured
    BEFORE the branch write (apply supplies it), so the diff shows the real
    divergence (not an empty diff against the content shipit just wrote over
    it). ``hooks_activated`` carries the real activation outcome so the body
    never claims a success that did not happen: ``None`` when the set has no
    checks to activate, ``True`` when ``lefthook install`` succeeded where
    install ran, ``False`` when it was skipped/failed (binary missing) and a
    merger must activate the checks themselves. ``rerendered`` is the same
    claim-nothing-that-did-not-happen discipline for the changelog axis: the
    body renders the re-render section only when apply ACTUALLY regenerated
    ``CHANGELOG.md``, never merely because the plan decided it — the
    gather→apply window can skip the write (``CHANGELOG/`` gone), in which case
    the file is dropped from the commit set and the section must not claim it.
    ``stamped_version`` is the Shipit pin this install stamped (ADR-0033);
    ``lint_debt`` is the best-effort whole-tree failing-check count (``None`` =
    unreadable, ``0`` = green — only red debt renders a section).
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
            "`.git/hooks/{pre-commit,pre-push}` fire `pixi run lint` there now. "
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
