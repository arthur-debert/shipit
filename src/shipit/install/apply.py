"""apply — the install domain's ONE effectful path: execute a :class:`Plan`.

Everything that touches the world lives here: unit writes and block splices,
retired-file unlinks, the policy seed, the manifest re-stamp, the bounded
``lefthook install`` activation, and the four write modes' git/gh side effects
(#359: the branch/PR side effect is explicit opt-in — the default mode
refreshes the working tree and stops). Returns a typed :class:`InstallResult`;
logs every milestone (the durable twin, ADR-0029) and never prints — the
terminal report is the renderer's (:mod:`shipit.verbs.install`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .. import buildid, config, execrun, gh, git, pixienv
from . import selfcert
from .errors import InstallError, SelfCertError
from .reconcile import Plan, consumer_inner, format_lefthook_conflict
from .splice import SETTINGS_MALFORMED, splice_block, splice_settings_hook
from .units import (
    FMT_JSON_HOOK,
    HOOK_RECOVERY_CMD,
    LINT_ENV,
    PIXI_FILE,
    Unit,
    pixi_manifest_seed,
)

logger = logging.getLogger("shipit.install")

#: The four write modes, in order of precedence at the CLI:
#:
#: - ``MODE_TREE`` (default) — refresh the managed set IN THE WORKING TREE and
#:   stop: no commit, no branch, no push, no PR. Committing the refresh is the
#:   caller's job — mid-workstream the refreshed files belong in the caller's
#:   own commit/PR, never in a parallel PR racing it to main (#359).
#: - ``MODE_LOCAL`` — commit the managed set on the CURRENT branch and stop: no
#:   branch switch, no push, no PR. This is the Tree-provisioning mode
#:   (``tree create``): the Tree is already on its planned holding branch and
#:   provisioning only needs the managed files committed there, never an origin
#:   side effect (no ``shipit/install`` branch, no draft PR). See #170.
#: - ``MODE_PUSH`` — break-glass: commit on the current branch and push straight
#:   to it (admin bypass), no PR.
#: - ``MODE_PR`` — switch to the ``shipit/install`` branch, commit, force-push,
#:   and open a DRAFT PR (the standalone consumer-onboarding/reconcile flow).
MODE_TREE = "tree"
MODE_LOCAL = "local"
MODE_PUSH = "push"
MODE_PR = "pr"
MODES = (MODE_TREE, MODE_LOCAL, MODE_PUSH, MODE_PR)

INSTALL_BRANCH = "shipit/install"
COMMIT_MESSAGE = "chore(shipit): install/update the managed set"

LEFTHOOK_BINARY = "lefthook"
HOOK_ACTIVATE_ARGV = ["install"]

#: The consumer-generated lockfile (#439): the managed set's decision is the
#: COMMITTED lockfile — pixi's own recommended practice, and what laptop/CI
#: parity via ``setup-pixi --locked`` wants. The lockfile is generated per
#: consumer (self-certification's lint-env solve materializes/refreshes it), so
#: it can never be a pristine-hashed managed unit; instead every committing
#: apply stages it alongside the managed set when present, and no consumer tree
#: is left dirty with an untracked ``pixi.lock`` after an install lands.
PIXI_LOCK = "pixi.lock"

#: The PR-body renderer apply calls at the boundary moment (``MODE_PR`` only):
#: ``(override_before, hooks_activated, stamped_pin, lint_debt) -> body``.
#: Injected by the verb so the body's sections stay a pure renderer concern
#: (ADR-0030) while apply supplies the inputs only it can know — the pre-write
#: consumer snapshots, the real activation outcome, the pin it stamped, and the
#: best-effort whole-tree debt count (``None`` when unreadable).
PrBody = Callable[[Mapping[str, str], "bool | None", str, "int | None"], str]


@dataclass(frozen=True)
class InstallResult:
    """What an :func:`apply` actually did — the typed result (ADR-0030).

    ``hooks_activated`` carries the real activation outcome so a renderer (or
    the PR body) never claims a success that did not happen: ``None`` when this
    apply had no checks to activate, ``True`` when ``lefthook install``
    succeeded where install ran, ``False`` when it was skipped/failed —
    ``hooks_detail`` then carries the human-oriented failure detail.
    """

    plan: Plan
    mode: str
    hooks_activated: bool | None = None
    hooks_detail: str = ""
    branch: str | None = None  # the committed/pushed branch (local/push modes)
    pr_url: str | None = None  # the draft PR (pr mode)
    pr_updated: bool = False  # True when an existing install PR was refreshed
    stamped_version: str | None = None  # the Shipit pin this apply stamped (#433)
    lint_debt: int | None = None  # whole-tree failing checks (MODE_PR; None = unread)


def write_unit(root: Path, unit: Unit) -> None:
    """Apply an ADD/UPDATE/OVERRIDE: write the file, or splice the block into its file."""
    dest = root / unit.dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if unit.kind == "block":
        existing = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        if unit.fmt == FMT_JSON_HOOK:
            spliced = splice_settings_hook(
                existing, unit.desired_inner(), unit.event, unit.marker
            )
        else:
            spliced = splice_block(
                existing,
                unit.desired_inner(),
                unit.open_marker,
                unit.close_marker,
                unit.anchor,
            )
        dest.write_text(spliced, encoding="utf-8")
        return
    dest.write_bytes(unit.content)
    if unit.executable:
        dest.chmod(0o755)


def _activate_hooks(root: Path) -> execrun.ExecResult:
    """Run ``lefthook install`` in ``root`` — the bounded side effect that turns
    the ``lefthook.yml`` config into live ``.git/hooks``.

    Runs THROUGH the consumer's OWN managed pixi lint env
    (:func:`shipit.pixienv.run_in_env`, ``environment=LINT_ENV`` — the same seam
    :func:`shipit.tree.create._activate_hooks` uses). This is the #478 fix: the
    ``.git/hooks/pre-push`` shim lefthook generates bakes ``os.Executable()`` (the
    absolute path of the lefthook that ran ``install``) into its ``call_lefthook``
    resolution chain as a fallback. Run bare off the install process's PATH, that
    executable is the INSTALLER's env — and when the installer is an ephemeral
    shipit Tree, a cross-tree absolute path that dies when the Tree is gc'd and
    exists on no other machine. Routing through the consumer's own lint env
    (where the managed blocks pin ``lefthook``) makes the baked fallback
    consumer-local and stable instead, and ``--manifest-path`` (built by
    :func:`shipit.pixienv.run_argv`) pins resolution to the consumer's manifest
    regardless of any inherited ``PIXI_PROJECT_MANIFEST``. The shim's *first-hit*
    resolution is still ``lefthook`` on PATH, which resolves consumer-locally
    inside any activated pixi env (how a pixi consumer runs hooks); the baked
    path is only its non-activated fallback.

    ``check=False`` because a nonzero rc is an outcome apply *degrades* on
    (activation is opportunistic setup, never a hard-fail check); a launch
    failure — ``pixi`` absent, or a hang killed at the adapter's long-runner
    bound — surfaces as the runner's :class:`~shipit.execrun.ExecError`, which
    apply likewise absorbs into the result's activation outcome and moves on
    (fail-open where the runtime is genuinely absent, #491's sibling theme). The
    adapter's long-runner bound covers the worst case: a first ``pixi run -e
    lint`` solving the lint env — provisioning-shaped work, the same reason
    :func:`shipit.tree.create._activate_hooks` uses it.
    """
    return pixienv.run_in_env(
        [LEFTHOOK_BINARY, *HOOK_ACTIVATE_ARGV],
        root,
        environment=LINT_ENV,
        check=False,
    )


def _activation_output(result: execrun.ExecResult) -> str:
    """Both streams of an activation run, joined for the renderer's warning.

    Joined with a newline so a stdout without a trailing newline does not run
    straight into stderr (e.g. ``donefatal: ...``) in the warning the verb prints.
    """
    return "\n".join(s for s in (result.stdout, result.stderr) if s)


def consumer_snapshot(root: Path, unit: Unit) -> str:
    """The consumer's current text for a unit — captured BEFORE any overwrite."""
    if unit.kind == "block":
        inner = consumer_inner(root, unit)
        if inner == SETTINGS_MALFORMED:
            # A malformed settings.json has no clean managed region; surface the
            # whole file so the OVERRIDE diff shows the human the real content.
            dest = root / unit.dest
            return (
                dest.read_text(encoding="utf-8", errors="replace")
                if dest.is_file()
                else ""
            )
        return "" if inner is None else inner + "\n"
    dest = root / unit.dest
    return dest.read_text(encoding="utf-8", errors="replace") if dest.is_file() else ""


def _shipit_version() -> str:
    """The FULL git sha of the build performing this install — the Shipit pin.

    ADR-0033: the stamp is the build's OWN commit identity
    (:func:`shipit.buildid.build_sha` — install record, build-time embed, or
    source-checkout HEAD), never an operator-supplied value and never the
    static package version (which identifies nothing). Fails CLOSED with
    :class:`InstallError` when no identity resolves: a pin the launcher cannot
    exec is worse than no install at all. The version string is a rendered
    artifact, so the typed :class:`~shipit.identity.Sha` stringifies here, at
    the seam.
    """
    sha = buildid.build_sha()
    if sha is None:
        raise InstallError(
            "cannot resolve this shipit build's own commit identity (no "
            "direct_url.json vcs record, no embedded build-sha, not a git "
            "checkout) — refusing to stamp a pin that identifies nothing "
            "(ADR-0033). Install shipit from git (uv records the commit) or "
            "run it from a checkout."
        )
    return str(sha)


def _activate(
    root: Path, activate_hooks: Callable[[Path], execrun.ExecResult]
) -> tuple[bool, str]:
    """Run the activation boundary, absorbing failure into a ``(ok, detail)`` outcome.

    A transport failure from the runner branches on the cause so a timeout or
    other OS error is not mislabelled as a missing binary. Activation now runs
    THROUGH the consumer's pixi lint env (#478, :func:`_activate_hooks`), so the
    missing-binary case is ``pixi`` absent (``pixi`` is ``argv[0]`` — a nonzero
    rc from a broken lint env is a normal ``check=False`` result, not this
    transport error). Either way the recovery the operator runs is the ONE
    shipit-level command (:data:`~shipit.install.units.HOOK_RECOVERY_CMD`):
    re-run ``shipit install``, which re-activates the hooks idempotently. The
    message never leaks the internal ``lefthook``/``pixi`` command under it —
    that is the layer the operator drives shipit over, not a command they run.
    """
    try:
        activation = activate_hooks(root)
    except execrun.ExecError as exc:
        if exc.cause == execrun.CAUSE_MISSING_BINARY:
            detail = (
                f"pixi not found on PATH — activation runs the checks through "
                f"the managed lint env, so pixi must be installed; then "
                f"`{HOOK_RECOVERY_CMD}` to activate the checks"
            )
        else:
            detail = (
                f"activation could not run ({exc}) — resolve the failure "
                f"above, then `{HOOK_RECOVERY_CMD}` to activate the checks"
            )
        return False, detail
    if activation.ok:
        logger.info(
            "git hooks activated",
            extra={"root": str(root), "duration_ms": activation.duration_ms},
        )
        return True, _activation_output(activation)
    return False, _activation_output(activation)


def reject_lefthook_conflicts(plan: Plan, mode: str) -> None:
    """Fail closed on a #544 lefthook merge conflict BEFORE any write or git
    side effect — the single guard shared by :func:`apply` and the verb's
    no-op shortcut (:mod:`shipit.verbs.install`), so a conflict-bearing but
    otherwise-empty plan cannot slip past a committing mode's no-op return.

    The managed ``lefthook.yml`` and the consumer's ``lefthook-local.yml`` merge
    into a config lefthook refuses to run, so publishing it would brick every
    commit in the consumer. Every committing mode refuses; ``MODE_TREE`` stays a
    warning (the plan's stderr lines) — a working-tree refresh publishes
    nothing, and the caller reviews ``git diff`` with the warning in hand. The
    refusal can originate on EITHER side (see :func:`format_lefthook_conflict`):
    usually the consumer's local config sets the option to drop, but when the
    managed side alone sets both the remedy is regenerating the managed
    ``lefthook.yml``. Either way the fix is a config edit the operator makes, so
    this is a plain :class:`InstallError`, never a :class:`SelfCertError` (which
    signals a self-certification postcondition failure of shipit's own staged
    managed content)."""
    if plan.lefthook_conflicts and mode != MODE_TREE:
        raise InstallError(
            "lefthook config conflict — refusing to publish a managed config "
            "that cannot run:\n"
            + "\n".join(
                f"  {format_lefthook_conflict(c)}" for c in plan.lefthook_conflicts
            )
        )


def apply(
    plan: Plan,
    mode: str = MODE_TREE,
    *,
    activate_hooks: Callable[[Path], execrun.ExecResult] | None = None,
    pr_body: PrBody | None = None,
    certify=None,
    debt=None,
) -> InstallResult:
    """Execute ``plan`` against its consumer root — the only effectful path.

    Writes every decided unit, unlinks every decided retired DELETE (the
    decision already proved the content is a known pristine version, so the
    unlink is the whole IO; KEEPs touch nothing), seeds the consumer-owned
    policy, re-stamps the manifest from the CURRENT decisions only (so a unit
    retired in a later shipit version drops out rather than lingering as a
    stale key), opportunistically activates the git hooks, and performs the
    ``mode``'s git/gh side effects.

    ``activate_hooks`` injects the lefthook boundary so tests exercise the
    activation contract without mutating a real ``.git/hooks``; ``pr_body`` is
    the verb's pure PR-body renderer, required for :data:`MODE_PR` (the draft
    PR's body is rendered at the boundary moment, from the pre-write override
    snapshots, the real activation outcome, the stamped pin, and the debt
    count). ``certify``/``debt`` inject the self-certification boundaries
    (defaults: :func:`shipit.install.selfcert.certify` /
    :func:`~shipit.install.selfcert.consumer_debt`).

    Every COMMITTING mode (``local``/``push``/``pr``) self-certifies after
    staging and BEFORE any git side effect (ADR-0033): a missed postcondition
    raises :class:`SelfCertError` — fail closed, no commit, no PR, the loud
    diagnostic naming each miss. The default working-tree refresh does not
    certify: nothing is being published, `git diff` is the caller's review
    surface, and the caller's own commit rides the repo's hooks. Install's OWN
    git operations — the reconcile commit AND its push — bypass the repo's
    hooks (``--no-verify``, #477): the whole-tree gate is the REPO'S bar, this
    very run just armed it (pre-push lints the whole tree, not the staged
    managed set), and pre-existing consumer debt is reported in the PR body,
    never a blocker.

    Raises :class:`InstallError` on a domain refusal (``local``/``push`` in
    detached HEAD, a failed self-certification, a lefthook merge conflict with
    the consumer's local config in any committing mode — #544) and lets a
    git/gh boundary
    failure propagate as :class:`~shipit.execrun.ExecError` — both members of
    the CLI error shell's known set. Callers decide nothing here: a no-op plan
    should simply never be applied (:attr:`Plan.nothing_to_do`).
    """
    if mode not in MODES:
        raise ValueError(f"unknown install mode: {mode!r}")
    if mode == MODE_PR and pr_body is None:
        raise ValueError("MODE_PR needs the pr_body renderer")
    reject_lefthook_conflicts(plan, mode)
    activate = activate_hooks or _activate_hooks
    started = time.monotonic()
    root = Path(plan.root)

    # Snapshot each override's consumer content BEFORE writing, so the PR diff
    # shows the real divergence rather than an empty diff against what we wrote.
    # Only MODE_PR renders these snapshots (into the draft PR body), so the
    # other modes skip the reads entirely.
    override_before = (
        {d.unit.key: consumer_snapshot(root, d.unit) for d in plan.overrides}
        if mode == MODE_PR
        else {}
    )

    # Seed the minimal valid pixi manifest BEFORE the unit writes (#432): the
    # pixi block splices below land inside a file pixi can parse, so the very
    # first commit — which fires the freshly-synced pre-commit hook, which
    # shells into pixi — sees a valid manifest. Guarded on the file still being
    # absent so a pixi.toml that appeared in the gather→apply window is never
    # clobbered (the same idempotence stance as the retired unlinks).
    if plan.seed_pixi_manifest:
        pixi_path = root / PIXI_FILE
        if not pixi_path.is_file():
            pixi_path.write_text(pixi_manifest_seed(root.name), encoding="utf-8")
            logger.info(
                "seeded pixi manifest",
                extra={"root": str(root), "path": PIXI_FILE},
            )

    for d in plan.writes:
        write_unit(root, d.unit)
    for d in plan.retire_deletes:
        # missing_ok: the decision proved a pristine copy existed at gather
        # time; if it vanished in the gather→apply window the goal state
        # ("file absent") already holds, so the unlink stays idempotent.
        (root / d.retired.path).unlink(missing_ok=True)
    cfg_path = root / config.CONFIG_NAME
    # Seed the consumer-owned policy BEFORE the manifest write, which preserves
    # `[secrets]`/`[reviewers]` textually while it re-stamps `[shipit]`/`[managed]`.
    if plan.seeds:
        config.apply_policy_seed(cfg_path)
    new_managed = {d.unit.key: d.desired_hash for d in plan.decisions}
    stamped_version = _shipit_version()
    config.write_manifest(cfg_path, version=stamped_version, managed=new_managed)
    # The reconcile milestone: the managed set (and manifest) is on disk.
    logger.info(
        "managed set written",
        extra={
            "root": str(root),
            "adds": sum(1 for d in plan.writes if d.action == "add"),
            "updates": sum(1 for d in plan.writes if d.action == "update"),
            "overrides": len(plan.overrides),
            "seeds": len(plan.seeds),
            "retire_deletes": len(plan.retire_deletes),
            "retire_keeps": len(plan.retire_keeps),
        },
    )

    # Turn the checks on: with lefthook.yml on disk, activate the local hooks so
    # `pixi run lint` fires at commit time — the checks ship LIVE, not dormant.
    # Opportunistic, so a missing lefthook degrades rather than aborting. Only
    # (re)activate when this install actually writes a managed unit; a seed-only
    # change touches just `.shipit.toml` and leaves the live hooks alone.
    hooks_activated: bool | None = None
    hooks_detail = ""
    if plan.writes and plan.activates_hooks:
        hooks_activated, hooks_detail = _activate(root, activate)
        if not hooks_activated:
            # Degraded-but-continuing: the config shipped, only local activation
            # was deferred — the PR body tells the merger to activate.
            logger.warning(
                "could not activate git hooks: %s",
                hooks_detail.strip(),
                extra={"root": str(root)},
            )

    result = InstallResult(
        plan=plan,
        mode=mode,
        hooks_activated=hooks_activated,
        hooks_detail=hooks_detail,
        stamped_version=stamped_version,
    )
    cwd = str(root)

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if mode == MODE_TREE:
        # Default: working-tree refresh ONLY (#359). The managed set and the
        # manifest are on disk, uncommitted — `git diff` is the review surface,
        # and the caller folds the refresh into their own commit/PR. Zero git/gh
        # side effects: no commit, no branch, no push, no PR.
        logger.info(
            "install refreshed working tree",
            extra={
                "root": str(root),
                "mode": MODE_TREE,
                "writes": len(plan.writes),
                "overrides": len(plan.overrides),
                "duration_ms": _elapsed(),
            },
        )
        return result

    # The committing modes self-certify BEFORE any git side effect (ADR-0033):
    # any missed postcondition fails closed — no commit, no push, no PR — with
    # the loud diagnostic naming every miss. Runs after the writes/stamp/
    # activation above, so it asserts exactly the state a commit would publish.
    certifier = certify or selfcert.certify
    cert_report = certifier(
        plan,
        root,
        hooks_activated=hooks_activated,
        stamped_pin=stamped_version,
    )
    if not cert_report.ok:
        message = selfcert.format_failure(cert_report)
        logger.error(
            "install self-certification failed — failing closed (no commit, no PR)",
            extra={
                "root": str(root),
                "mode": mode,
                "failed_checks": ", ".join(c.name for c in cert_report.failures),
            },
        )
        raise SelfCertError(message)

    # The consumer's whole-tree debt (#439's sibling scoping, ADR-0033):
    # REPORTED in the reconcile PR body, never a blocker — read best-effort
    # only where a PR body will carry it.
    if mode == MODE_PR:
        debt_reader = debt or selfcert.consumer_debt
        result = replace(result, lint_debt=debt_reader(root))

    changed_paths = list(plan.changed_paths)
    # The committed-lockfile decision (#439, see PIXI_LOCK): the lint-env solve
    # above materializes/refreshes pixi.lock; stage it with the managed set so
    # the install lands laptop/CI parity and never leaves the tree dirty.
    if PIXI_LOCK not in changed_paths and (root / PIXI_LOCK).is_file():
        changed_paths.append(PIXI_LOCK)
    try:
        if mode == MODE_LOCAL:
            branch = git.current_branch(cwd=cwd)
            if branch is None:
                raise InstallError("--local needs a checked-out branch")
            git.add(changed_paths, cwd=cwd)
            git.commit(COMMIT_MESSAGE, changed_paths, cwd=cwd, no_verify=True)
            logger.info(
                "install committed locally",
                extra={
                    "root": str(root),
                    "branch": branch,
                    "mode": MODE_LOCAL,
                    "duration_ms": _elapsed(),
                },
            )
            return replace(result, branch=branch)

        if mode == MODE_PUSH:
            branch = git.current_branch(cwd=cwd)
            if branch is None:
                raise InstallError("--push needs a checked-out branch")
            git.add(changed_paths, cwd=cwd)
            git.commit(COMMIT_MESSAGE, changed_paths, cwd=cwd, no_verify=True)
            git.push(branch, cwd=cwd, no_verify=True)
            logger.info(
                "install pushed break-glass",
                extra={
                    "root": str(root),
                    "branch": branch,
                    "mode": MODE_PUSH,
                    "duration_ms": _elapsed(),
                },
            )
            return replace(result, branch=branch)

        # MODE_PR: stage onto the install branch, push it, open a DRAFT PR — the
        # standalone consumer-onboarding/reconcile flow, explicit opt-in only.
        git.switch_create(INSTALL_BRANCH, cwd=cwd)
        git.add(changed_paths, cwd=cwd)
        git.commit(COMMIT_MESSAGE, changed_paths, cwd=cwd, no_verify=True)
        # The install branch is regenerated from HEAD each run; force so a re-run
        # with an open install PR updates it rather than failing non-fast-forward.
        git.push(INSTALL_BRANCH, cwd=cwd, force=True, no_verify=True)
        existing = gh.pr_url_for_head(INSTALL_BRANCH, cwd=cwd)
        if existing:
            # The force-push already refreshed the open PR's diff.
            logger.info(
                "install draft PR updated",
                extra={
                    "root": str(root),
                    "branch": INSTALL_BRANCH,
                    "url": existing,
                    "duration_ms": _elapsed(),
                },
            )
            return replace(
                result, branch=INSTALL_BRANCH, pr_url=existing, pr_updated=True
            )
        url = gh.pr_create(
            head=INSTALL_BRANCH,
            title="shipit: install/update the managed set",
            body=pr_body(
                override_before, hooks_activated, stamped_version, result.lint_debt
            ),
            draft=True,
            cwd=cwd,
        )
        logger.info(
            "install draft PR opened",
            extra={
                "root": str(root),
                "branch": INSTALL_BRANCH,
                "url": url,
                "duration_ms": _elapsed(),
            },
        )
        return replace(result, branch=INSTALL_BRANCH, pr_url=url)
    except execrun.ExecError:
        # The failure propagates to the CLI error shell (clean `error: …` +
        # exit 1); it is recorded here at ERROR with the exception attached so
        # the durable record survives the propagation (ADR-0029).
        logger.error(
            "install git/gh step failed", exc_info=True, extra={"root": str(root)}
        )
        raise
