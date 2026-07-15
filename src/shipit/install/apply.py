"""apply — the install domain's ONE effectful path: execute a :class:`Plan`.

Everything that touches the world lives here: unit writes and block splices,
the changelog re-render (#578), retired-file unlinks, retired-hook-entry
removals (#619), the policy seed, the
manifest re-stamp, the bounded
``lefthook install`` activation, and the four write modes' git/gh side effects
(#359: the branch/PR side effect is explicit opt-in — the default mode
refreshes the working tree and stops). Returns a typed :class:`InstallResult`;
logs every milestone (the durable twin, ADR-0029) and never prints — the
terminal report is the renderer's (:mod:`shipit.verbs.install`).
"""

from __future__ import annotations

import logging
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from .. import buildid, config, execrun, gh, git, pixienv
from ..changelog import CHANGELOG_FILE
from . import selfcert
from .errors import InstallError, SelfCertError
from .reconcile import Plan, consumer_inner, format_lefthook_conflict
from .splice import (
    SETTINGS_MALFORMED,
    remove_retired_hooks,
    splice_block,
    splice_settings_hook,
)
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
#: - ``MODE_PR`` — (re)create the ``shipit/install`` branch based on the CURRENT
#:   origin default (never the Tree's cut point, #852), commit, force-push, and
#:   open a DRAFT PR against that default (the standalone
#:   consumer-onboarding/reconcile flow).
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

#: The suffix ``lefthook install`` appends when it renames a pre-existing hook
#: ``<hook>`` to ``<hook>.old`` before writing its own shim, and the markers
#: that positively identify a lefthook-generated shim (#777 mode 2). A stale
#: ``.git/hooks/pre-commit.old``
#: left by a prior (release-era) ``lefthook install`` makes the next
#: activation's rename fail ("can't rename pre-commit to pre-commit.old — file
#: already exists"), which absorbs into a failed ``hooks`` postcondition and
#: fails self-cert CLOSED. Pre-cleaning that stale backup unblocks activation.
#: The markers are lefthook's OWN generated content — the ``LEFTHOOK`` env
#: guards and the ``call_lefthook`` dispatch function every shim it writes
#: carries, at every version/size — so requiring BOTH positively identifies a
#: tool-authored shim and never a hand-written consumer hook (the conservative
#: bar the issue sets: only remove backups you can prove are release-managed).
HOOK_BACKUP_SUFFIX = ".old"
LEFTHOOK_SHIM_MARKERS = ("LEFTHOOK", "call_lefthook")

#: The MANAGED git-hook slots the shipped ``lefthook.yml`` activates a shim into
#: — the hooks whose activation this module owns (#912). Keep in sync with the
#: top-level hook keys in ``src/shipit/data/lefthook.yml``. This is the
#: managed-only set on purpose: a consumer's ``lefthook-local.yml`` can configure
#: ADDITIONAL hooks, and an obstruction in one of those paths can block
#: ``lefthook install`` too — but self-healing another tool's hook slot is not
#: this preclean's job, so it scopes itself to the slots shipit ships.
#: A pre-existing DANGLING symlink at one of these paths defeats
#: ``lefthook install``: lefthook's existence ``stat`` FOLLOWS the link, sees the
#: dead target as absent, skips its move-to-``.old`` step, and goes straight to
#: ``open(<hook>, O_CREATE|O_WRONLY)`` — which also follows the dead link and
#: tries to create the missing target in a directory that does not exist →
#: ``ENOENT``. :func:`_preclean_dangling_hook_symlinks` unlinks the dead link at
#: these paths so lefthook writes a fresh shim.
MANAGED_HOOK_NAMES = ("pre-commit", "pre-push", "post-commit")

#: The PR-body renderer apply calls at the boundary moment (``MODE_PR`` only):
#: ``(override_before, hooks_activated, rerendered, stamped_pin, lint_debt) ->
#: body``. Injected by the verb so the body's sections stay a pure renderer
#: concern (ADR-0030) while apply supplies the inputs only it can know — the
#: pre-write consumer snapshots, the real activation outcome, whether the
#: changelog re-render ACTUALLY ran (never just what the plan decided — the
#: gather→apply window can skip it), the pin it stamped, and the best-effort
#: whole-tree debt count (``None`` when unreadable).
PrBody = Callable[[Mapping[str, str], "bool | None", bool, str, "int | None"], str]


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


def _rerender_changelog(root: Path) -> bool:
    """Regenerate ``CHANGELOG.md`` from ``CHANGELOG/`` with the CURRENT renderer
    — the write half of the plan's re-render decision (TOL01-WS08 #578).

    Recomputed at the write, like the policy seed re-reads the config text: the
    render over what NOW holds is always the current one, so a fragment that
    changed in the gather→apply window still lands rendered, never half-stale.
    Skipped (idempotence, the retired-unlink stance) when the fragment tree
    vanished in that window — :func:`shipit.verbs.changelog.render_current`
    returns ``None`` and the goal state "nothing to render" already holds.
    Imported at call time for the same ``_errors``-shell cycle the selfcert
    lint import breaks lazily.

    Returns ``True`` when it wrote the file, ``False`` when it skipped — the
    caller drops the now-phantom ``CHANGELOG.md`` from the commit set on a skip
    so a committing mode's ``git add`` never chokes on it.
    """
    from ..verbs.changelog import render_current

    rendered = render_current(root)
    if rendered is None:
        return False
    (root / CHANGELOG_FILE).write_text(rendered, encoding="utf-8")
    logger.info(
        "changelog re-rendered with the current renderer",
        extra={"root": str(root), "path": CHANGELOG_FILE},
    )
    return True


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


def _preclean_stale_hook_backups(root: Path) -> None:
    """Remove stale lefthook-shim ``.old`` hook backups before ``lefthook
    install`` re-runs — the #777 mode 2 fix.

    ``lefthook install`` renames any pre-existing ``.git/hooks/<hook>`` to
    ``<hook>.old`` before writing its own shim; when a prior (release-era)
    activation already left a ``<hook>.old`` behind, that rename collides
    ("can't rename pre-commit to pre-commit.old — file already exists"), the
    activation fails, and self-cert fails CLOSED on the ``hooks`` postcondition.
    The fleet is full of ex-release repos carrying exactly this leftover.

    Conservative by construction (the issue's bar — only remove backups
    positively identifiable as tool-managed): a ``.old`` file is removed only
    when its content carries BOTH :data:`LEFTHOOK_SHIM_MARKERS`, which lefthook
    bakes into every shim it generates and a hand-written consumer hook would
    not. A backup that is not a lefthook shim is left untouched. Best-effort:
    an unreadable/unremovable backup is logged and skipped, never fatal — the
    worst case is the pre-existing collision, which the activation degrades on
    as before.

    Resolves the hooks dir through the git adapter (:func:`shipit.git.hooks_dir`,
    #914) so a linked-worktree consumer — whose ``.git`` is a *file* and whose
    hooks live in the shared common dir — is cleaned too, not silently skipped by
    a hardcoded ``root / ".git" / "hooks"`` that does not exist there.
    """
    hooks_dir = git.hooks_dir(cwd=str(root))
    if hooks_dir is None or not hooks_dir.is_dir():
        return
    for backup in sorted(hooks_dir.glob(f"*{HOOK_BACKUP_SUFFIX}")):
        try:
            text = backup.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning(
                "skipping unreadable stale .old hook-backup file",
                exc_info=True,
                extra={"root": str(root), "backup": backup.name},
            )
            continue
        if not all(marker in text for marker in LEFTHOOK_SHIM_MARKERS):
            continue
        try:
            backup.unlink()
        except OSError:
            logger.warning(
                "could not remove stale lefthook-generated .old hook-backup file",
                exc_info=True,
                extra={"root": str(root), "backup": backup.name},
            )
            continue
        logger.info(
            "removed a stale lefthook-generated .old hook-backup file before "
            "activation (#777 mode 2)",
            extra={"root": str(root), "backup": backup.name},
        )


def _preclean_dangling_hook_symlinks(root: Path) -> None:
    """Remove a pre-existing DANGLING symlink at a managed hook path before
    ``lefthook install`` re-runs — the #912 fix.

    A repo carrying a legacy ``.git/hooks/<hook>`` symlink whose target no longer
    resolves defeats ``lefthook install``: lefthook decides a hook already exists
    with a ``stat`` that FOLLOWS the link, so a dangling link reads as absent;
    lefthook then skips its normal move-to-``.old`` step and goes straight to
    ``open(<hook>, O_CREATE|O_WRONLY)``, which ALSO follows the dead link and
    tries to create the missing target in a directory that does not exist →
    ``ENOENT`` ("no such file or directory"). lefthook cannot self-heal this, so
    that one hook never activates while the others do. Unlinking the dead link
    here lets lefthook write a fresh shim into the now-empty slot.

    Same category as :func:`_preclean_stale_hook_backups` (a leftover blocking
    activation), and held to the same conservative bar: only a DANGLING symlink
    is removed — the path is a symlink (:meth:`~pathlib.Path.is_symlink`, an
    ``lstat`` that does NOT follow) whose target does not resolve (a following
    :meth:`~pathlib.Path.stat` raises :class:`FileNotFoundError`). A symlink
    whose target resolves is a working consumer hook and is left untouched, as is
    a real (non-symlink) file. The classification stats rather than calling
    :meth:`~pathlib.Path.exists` on purpose: ``exists`` returns false for ANY
    ``stat`` failure — including a :class:`PermissionError` reaching an existing
    target — which would misread a LIVE but momentarily-unreachable link as
    dangling and destroy it. So ONLY :class:`FileNotFoundError` counts as
    dangling; any other :class:`OSError` leaves the link untouched. Best-effort:
    the classification calls are themselves OSError-guarded (a restrictive parent
    directory would otherwise crash the install), so an unclassifiable or
    unremovable link is logged and skipped, never fatal — the worst case is
    today's degraded activation warning.

    Resolves the hooks dir through the git adapter (:func:`shipit.git.hooks_dir`,
    #914) so a linked-worktree consumer — whose ``.git`` is a *file* and whose
    hooks live in the shared common dir — is cleaned too, not silently skipped by
    a hardcoded ``root / ".git" / "hooks"`` that does not exist there.
    """
    hooks_dir = git.hooks_dir(cwd=str(root))
    if hooks_dir is None or not hooks_dir.is_dir():
        return
    for name in MANAGED_HOOK_NAMES:
        hook = hooks_dir / name
        try:
            # DANGLING = a symlink (lstat, does not follow) whose target does not
            # resolve (a following stat raises FileNotFoundError). A real file or
            # an absent path is not a symlink; a link whose target resolves stats
            # cleanly. Stat rather than `exists()` keeps the bar honest —
            # `exists()` also reads false on a PermissionError while following the
            # link, which would destroy a LIVE but unreachable hook — so only
            # FileNotFoundError is dangling and any other OSError leaves it be.
            if not hook.is_symlink():
                continue
            try:
                hook.stat()  # follows the link
            except FileNotFoundError:
                pass  # dead target → dangling → removed below
            else:
                continue  # target resolves → live consumer hook → leave it
        except OSError:
            logger.warning(
                "could not classify a git-hook path before activation; "
                "leaving it untouched",
                exc_info=True,
                extra={"root": str(root), "hook": name},
            )
            continue
        try:
            hook.unlink()
        except OSError:
            logger.warning(
                "could not remove a dangling git-hook symlink before activation",
                exc_info=True,
                extra={"root": str(root), "hook": name},
            )
            continue
        logger.info(
            "removed a dangling git-hook symlink before activation (#912)",
            extra={"root": str(root), "hook": name},
        )


def _snapshot_paths(plan: Plan) -> list[str]:
    """Every consumer path a committing apply may write, delete, or stamp — the
    superset of :attr:`Plan.changed_paths` plus the two generated files it does
    not track (:data:`PIXI_LOCK`, and the seeded :data:`~shipit.install.units.PIXI_FILE`).

    The roll-back set for the #777 mode 3 transaction (see
    :func:`_snapshot_committing_writes`): capturing these before the writes and
    restoring them on a failed self-cert leaves NOTHING half-applied.
    """
    paths = set(plan.changed_paths)
    if plan.seed_pixi_manifest:
        paths.add(PIXI_FILE)
    paths.add(PIXI_LOCK)
    return sorted(paths)


class _SnapshotCell(NamedTuple):
    """One path's pre-write state: its bytes AND its permission bits.

    The mode is carried because a managed write can CHANGE it —
    :func:`write_unit` ``chmod(0o755)``\\s an executable unit (``bin/shipit``),
    and the rollback recreates retire-deleted files — so a bytes-only restore
    would return the content but leave the mode dirty (the #838 review's major
    finding). ``mode`` is the :func:`stat.S_IMODE` permission bits only, so the
    restore reapplies exactly what the file carried before, executable bit
    included.
    """

    data: bytes
    mode: int


def _snapshot_committing_writes(
    root: Path, plan: Plan
) -> dict[str, _SnapshotCell | None]:
    """Capture the pre-write bytes+mode of every :func:`_snapshot_paths` entry.

    ``None`` marks a path absent before the writes (so the restore UNLINKS it
    rather than resurrecting stale state). The map is the transaction the
    committing modes roll back to on a failed self-cert (#777 mode 3): the
    fail-closed run had already applied the full managed set AND stamped
    ``.shipit.toml``, so a rerun saw matching hashes, reported "nothing to do",
    and exited 0 — the half-applied state was unrecoverable by the tool. Taken
    only for committing modes; the default working-tree refresh publishes
    nothing and keeps its writes for the caller to review (``git diff``).
    """
    return {rel: _snapshot_cell(root / rel) for rel in _snapshot_paths(plan)}


def _snapshot_cell(path: Path) -> _SnapshotCell | None:
    """The path's pre-write cell (bytes + permission bits), or ``None`` if absent."""
    if not path.is_file():
        return None
    return _SnapshotCell(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def _restore_committing_writes(
    root: Path, snapshot: dict[str, _SnapshotCell | None]
) -> None:
    """Roll the working tree back to ``snapshot`` — the #777 mode 3 rollback.

    Each cell is restored to exactly its pre-write state: bytes are rewritten
    verbatim AND the original permission bits reapplied (a spliced block file
    returns to its original content, a stamped ``.shipit.toml`` to its prior
    stamp, an executable-managed ``bin/shipit`` to both its old bytes and old
    mode — never a mode-only dirty file after the content restore). A ``None``
    cell (the path was absent) is unlinked so a freshly-added managed file
    leaves no trace. Emptied parent directories are left in place — inert (git
    does not track them, reconcile reads files) and cheaper than proving a dir
    was created by this run rather than pre-existing. Hook activation is
    deliberately NOT rolled back either: the ``lefthook install`` shims are
    idempotent and sit outside the managed-set/manifest state that decides
    :attr:`Plan.nothing_to_do`, so a rerun re-activates them cleanly.

    Best-effort per path: a failure on one entry does NOT abort the rest — this
    is an emergency rollback, so every path that CAN be restored is, and only
    then is a combined :class:`OSError` raised naming the paths that could not be
    (chained from the first cause). The caller wraps that raise so a rollback
    failure never masks the ``SelfCertError`` that triggered it and logs it as a
    partial rollback.
    """
    failures: list[tuple[str, OSError]] = []
    for rel, cell in snapshot.items():
        dest = root / rel
        try:
            if cell is None:
                dest.unlink(missing_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(cell.data)
            dest.chmod(cell.mode)
        except OSError as exc:
            failures.append((rel, exc))
    if failures:
        names = ", ".join(rel for rel, _ in failures)
        raise OSError(f"could not roll back staged writes for: {names}") from failures[
            0
        ][1]


def _restore_caller_branch(cwd: str, original_ref: str | None) -> None:
    """Switch the caller's checkout back to ``original_ref`` after the MODE_PR
    flow — the #777 mode 1 fix.

    MODE_PR switches onto the ``shipit/install`` scratch branch to stage its
    commit; without this the operator is silently left off their own branch.
    Best-effort and no-op when there is nothing to restore to — a detached HEAD
    (``original_ref`` is ``None``). A caller who STARTED on the scratch branch
    has no other branch to return to, but the flow's
    ``reset --soft origin/<default>`` left the index staged with the
    pre-reset..base diff, so this unstages it (:func:`git.reset_index`) rather
    than leaving the operator a heavily-polluted index (#852 review). A restore
    that cannot run is logged, never raised: it must not mask the real apply
    outcome (or a git/gh error already unwinding through the ``finally`` that
    calls this).
    """
    if original_ref is None:
        return
    if original_ref == INSTALL_BRANCH:
        try:
            git.reset_index(cwd=cwd)
        except execrun.ExecError:
            logger.warning(
                "could not unstage the soft-reset index after the install PR "
                "flow — the caller was already on %s, so its index may retain "
                "staged changes",
                INSTALL_BRANCH,
                exc_info=True,
                extra={"root": cwd},
            )
        return
    try:
        git.switch(original_ref, cwd=cwd)
    except execrun.ExecError:
        logger.warning(
            "could not restore the caller's branch after the install PR flow — "
            "the checkout is left on %s",
            INSTALL_BRANCH,
            exc_info=True,
            extra={"root": cwd, "branch": original_ref},
        )


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

    Writes every decided unit, regenerates a stale ``CHANGELOG.md`` from
    ``CHANGELOG/`` with the current renderer when the plan decided the
    re-render (#578, :func:`_rerender_changelog`), unlinks every decided
    retired DELETE (the decision already proved the content is a known
    pristine version, so the
    unlink is the whole IO; KEEPs touch nothing), removes every decided
    retired hook ENTRY from its hooks file (#619 — shipit's own managed
    entries are protected inside the match), seeds the consumer-owned
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
    diagnostic naming each miss. The fail-closed is TRANSACTIONAL (#777 mode 3):
    a committing mode snapshots every path its writes will touch before the
    first write and rolls them back on a failed self-cert, so a fail-closed run
    leaves NOTHING half-applied — otherwise the stamped manifest and written
    managed set make the next run read "nothing to do" and exit 0 over an
    unrecoverable partial state. The default working-tree refresh does not
    certify (nor snapshot): nothing is being published, `git diff` is the
    caller's review surface, and the caller's own commit rides the repo's hooks.
    Install's OWN git operations — the reconcile commit AND its push — bypass the
    repo's hooks (``--no-verify``, #477): the whole-tree gate is the REPO'S bar,
    this very run just armed it (pre-push lints the whole tree, not the staged
    managed set), and pre-existing consumer debt is reported in the PR body,
    never a blocker. ``MODE_PR`` stages onto the ``shipit/install`` scratch
    branch, which it (re)creates and resets onto the CURRENT origin default
    branch (#852) — never the HEAD the Tree was cut from — so a Tree cut from a
    stale leftover remote ``shipit/install`` head can never stack a conflicting
    commit; and, in a ``finally``, always restores the caller's original checkout
    (#777 mode 1 — never strand the operator off their own branch).

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

    # Snapshot every path the committing writes below will touch BEFORE the
    # first write (#777 mode 3): a committing mode self-certifies AFTER staging,
    # and a failed self-cert must roll the tree back to leave NOTHING
    # half-applied — otherwise the stamped manifest + written managed set make a
    # rerun read "nothing to do" and exit 0 over an unrecoverable partial state.
    # The default working-tree refresh keeps its writes (its whole point), so it
    # takes no snapshot.
    committing_snapshot = (
        _snapshot_committing_writes(root, plan) if mode != MODE_TREE else None
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
    rerendered = plan.rerender_changelog and _rerender_changelog(root)
    for d in plan.retire_deletes:
        # missing_ok: the decision proved a pristine copy existed at gather
        # time; if it vanished in the gather→apply window the goal state
        # ("file absent") already holds, so the unlink stays idempotent.
        (root / d.retired.path).unlink(missing_ok=True)
    for d in plan.retire_hook_deletes:
        # Retired hook entries (#619): rewrite the hooks file without the
        # matched consumer-local entries. Runs AFTER the unit writes above, so
        # shipit's own freshly-spliced entries are already in place (and
        # protected inside the match — splice.is_retired_hook). Same
        # idempotence stance as the unlinks: a file gone in the gather→apply
        # window means the goal state already holds, and a file turned
        # malformed in that window is preserved verbatim by
        # remove_retired_hooks (never a clobber).
        #
        # Fails OPEN in lockstep with the gather side (reconcile.retired_hook_count,
        # #619): a consumer-owned hooks file that turns unreadable/non-UTF-8 — or
        # an unwritable dest — in the gather→apply window degrades to "nothing to
        # remove" with a logged warning rather than crashing the whole install.
        dest = root / d.retired.file
        if not dest.is_file():
            continue
        try:
            text = dest.read_text(encoding="utf-8")
            dest.write_text(
                remove_retired_hooks(text, d.retired.event, d.retired.marker),
                encoding="utf-8",
            )
        except (OSError, UnicodeDecodeError):
            logger.warning(
                "skipping unreadable/unwritable hooks file in the retired-hooks pass",
                exc_info=True,
                extra={"root": str(root), "file": d.retired.file},
            )
    cfg_path = root / config.CONFIG_NAME
    # Seed the consumer-owned policy BEFORE the manifest write, which preserves
    # `[secrets]`/`[reviewers]` textually while it re-stamps `[shipit]`/`[managed]`.
    if plan.seeds:
        # Re-derive the [toolchains] entries at the write (#578) — the same
        # derivation gather planned with, recomputed the way the whole seed
        # pass re-reads the config text (idempotent either way: an entry that
        # appeared in the gather→apply window just seeds what NOW holds).
        config.apply_policy_seed(cfg_path, toolchains=config.derive_toolchains(root))
    # Stamped from the CURRENT decisions only: a unit retired in a later shipit
    # version — or DECLINED by the consumer (#600, excluded from the decisions
    # at reconcile) — drops out of [managed] here rather than lingering as a
    # stale key that re-proposes the same override every reconcile.
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
            "retire_hook_deletes": len(plan.retire_hook_deletes),
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
        # Clear any stale lefthook `.old` backup first (#777 mode 2) so the
        # rename `lefthook install` performs never collides — a collision fails
        # the whole activation, which then fails self-cert closed on a virgin
        # ex-release consumer. Same for a pre-existing DANGLING hook symlink
        # (#912): lefthook's stat follows the dead link, reads it as absent, and
        # then ENOENTs trying to create its missing target — so that one hook
        # never activates. Both are leftovers that block activation; clear them
        # before `lefthook install` writes its fresh shims.
        _preclean_stale_hook_backups(root)
        _preclean_dangling_hook_symlinks(root)
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
        # Roll the staged writes back BEFORE raising (#777 mode 3): fail-closed
        # must be fully closed, leaving the tree exactly as apply found it. Skip
        # only the (unreachable-here) missing-snapshot guard for MODE_TREE, which
        # never certifies. Otherwise the stamped manifest + written managed set
        # would make the next run read "nothing to do" over a half-applied tree.
        # A rollback IO failure must not mask the SelfCertError (the real fault):
        # log it and still raise the diagnostic that names every missed check.
        if committing_snapshot is not None:
            try:
                _restore_committing_writes(root, committing_snapshot)
            except OSError:
                logger.warning(
                    "could not fully roll the staged writes back after a failed "
                    "self-cert — the working tree may retain partial managed state",
                    exc_info=True,
                    extra={"root": str(root)},
                )
        logger.error(
            "install self-certification failed — failing closed (no commit, no "
            "PR); staged writes rolled back best-effort (a preceding warning "
            "flags any partial rollback)",
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
    # The re-render can be skipped in the gather→apply window (CHANGELOG/ gone
    # or turned unrenderable → render_current is None, the retired-unlink
    # idempotence stance, #578). Complete the skip: drop the now-phantom
    # CHANGELOG.md from the commit set so a committing mode's `git add -f` never
    # crashes with an opaque pathspec error on a file that was never (re)written
    # and is absent+untracked (#578 review).
    if plan.rerender_changelog and not rerendered:
        changed_paths = [p for p in changed_paths if p != CHANGELOG_FILE]
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
        # Capture the caller's branch first so the `finally` returns their
        # checkout to it (#777 mode 1): `shipit/install` is a shipit-owned
        # scratch ref, and leaving the operator sitting on it — off their own
        # branch, with no notice — is the surprise the issue reports.
        original_ref = git.current_branch(cwd=cwd)
        # Base the staging branch on the CURRENT origin default, never on the
        # HEAD the Tree was cut from (#852): a Tree cut from a STALE leftover
        # remote `shipit/install` head (the reconcile merge does not delete the
        # staging branch) would otherwise stack the fresh managed commit onto
        # stale commits, minting a PR that conflicts with main. Fetch the base,
        # (re)create the branch, then reset it onto origin/<default> so the
        # managed commit lands as ONE clean refresh regardless of the cut point —
        # which also RECYCLES a stale leftover branch on the next run. The reset
        # is `--soft`: it moves only the branch pointer, so the rendered managed
        # files stay in the working tree for the pathspec commit below (which
        # takes the listed paths from the working tree and everything else from
        # HEAD, now origin/<default>).
        base_branch = git.default_branch(cwd=cwd)
        git.fetch(cwd=cwd)
        # The branch switch and the reset live INSIDE the try/finally: a
        # `reset_soft` (or `switch_create`) that raises AFTER the checkout has
        # moved onto `shipit/install` must still restore the caller's branch
        # (#852 review) — leaving the operator stranded on the scratch branch is
        # exactly the #777 mode 1 surprise the restore exists to prevent.
        try:
            git.switch_create(INSTALL_BRANCH, cwd=cwd)
            git.reset_soft(f"origin/{base_branch}", cwd=cwd)
            # Commit the FULL managed universe, not just the pre-reset writes:
            # `changed_paths` is computed against the Tree's cut point, where a
            # managed file already at desired content is a NOOP and drops out of
            # the write set. After the reset onto origin/<base> that same file may
            # be ABSENT from the base (a Tree cut from a stale `shipit/install`
            # head), so a changed_paths-only commit would silently DROP it from
            # the refreshed PR (#852 review). Staging every managed destination
            # makes the commit deterministically origin/<base> + the whole managed
            # set, whatever HEAD the Tree was cut from — the pathspec commit still
            # takes everything else from HEAD, so a NOOP already matching the base
            # contributes no diff.
            pr_paths = sorted(
                set(changed_paths) | {d.unit.dest for d in plan.decisions}
            )
            git.add(pr_paths, cwd=cwd)
            if not git.has_staged_changes(pr_paths, cwd=cwd):
                # After the reset the managed set already matches origin/<base> —
                # a stale Tree duplicating an already-merged reconcile. A pathspec
                # `git commit` over an empty diff fails with "nothing to commit"
                # (exit 1); report the clean no-op and skip the commit/PR rather
                # than crashing the install (#852 review). The `finally` still
                # restores the caller's branch.
                logger.info(
                    "install PR: the managed set is already current on "
                    "origin/%s — nothing to publish",
                    base_branch,
                    extra={
                        "root": str(root),
                        "branch": INSTALL_BRANCH,
                        "base": base_branch,
                        "duration_ms": _elapsed(),
                    },
                )
                return result
            git.commit(COMMIT_MESSAGE, pr_paths, cwd=cwd, no_verify=True)
            # The install branch is regenerated on top of origin/<default> each
            # run; force so a re-run with an open install PR (or a stale leftover
            # branch) updates it rather than failing non-fast-forward.
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
                base=base_branch,
                title="shipit: install/update the managed set",
                body=pr_body(
                    override_before,
                    hooks_activated,
                    rerendered,
                    stamped_version,
                    result.lint_debt,
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
        finally:
            # Success or failure, restore the caller's checkout: the operator
            # never asked to move off their branch, and a git/gh failure mid-flow
            # would otherwise strand them on the scratch branch too.
            _restore_caller_branch(cwd, original_ref)
    except execrun.ExecError:
        # The failure propagates to the CLI error shell (clean `error: …` +
        # exit 1); it is recorded here at ERROR with the exception attached so
        # the durable record survives the propagation (ADR-0029).
        logger.error(
            "install git/gh step failed", exc_info=True, extra={"root": str(root)}
        )
        raise
