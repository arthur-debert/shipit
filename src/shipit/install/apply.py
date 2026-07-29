"""apply — the install domain's ONE effectful path: execute a :class:`Plan`.

Every write, unlink, splice, activation and git/gh side effect lives here.
Returns a typed :class:`InstallResult` and never prints.
"""

from __future__ import annotations

import logging
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from .. import buildid, config, execrun, gh, git, pixienv
from ..changelog import CHANGELOG_FILE
from . import selfcert
from .errors import InstallError, SelfCertError
from .reconcile import (
    DELETE,
    KEEP,
    ClaudeSkillsLink,
    Plan,
    consumer_inner,
    format_lefthook_conflict,
    format_pixi_key_conflict,
    format_stale_provision,
    format_symlinked_dest,
    symlinked_dest_component,
)
from .splice import (
    ENV_MEMBER_MALFORMED,
    ENV_MEMBER_UNSUPPORTED,
    SETTINGS_MALFORMED,
    remove_retired_hooks,
    splice_block,
    splice_env_member,
    splice_settings_hook,
)
from .units import (
    CLAUDE_SKILLS_DIR,
    CLAUDE_SKILLS_LINK_TARGET,
    FMT_ENV_MEMBER,
    FMT_JSON_HOOK,
    HOOK_RECOVERY_CMD,
    LINT_ENV,
    PIXI_FILE,
    Unit,
    pixi_manifest_seed,
)

logger = logging.getLogger("shipit.install")

MODE_TREE = "tree"
MODE_LOCAL = "local"
MODE_PUSH = "push"
MODE_PR = "pr"
MODES = (MODE_TREE, MODE_LOCAL, MODE_PUSH, MODE_PR)

INSTALL_BRANCH = "shipit/install"
COMMIT_MESSAGE = "chore(shipit): install/update the managed set"

LEFTHOOK_BINARY = "lefthook"
HOOK_ACTIVATE_ARGV = ["install"]

PIXI_LOCK = "pixi.lock"

HOOK_BACKUP_SUFFIX = ".old"
LEFTHOOK_SHIM_MARKERS = ("LEFTHOOK", "call_lefthook")

MANAGED_HOOK_NAMES = ("pre-commit", "pre-push", "post-commit")

PrBody = Callable[[Mapping[str, str], "bool | None", bool, str, "int | None"], str]


@dataclass(frozen=True)
class InstallResult:
    """What an :func:`apply` actually did; ``hooks_activated`` is ``None`` when there was nothing to activate."""

    plan: Plan
    mode: str
    hooks_activated: bool | None = None
    hooks_detail: str = ""
    branch: str | None = None
    pr_url: str | None = None
    pr_updated: bool = False
    stamped_version: str | None = None
    lint_debt: int | None = None


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
        elif unit.fmt == FMT_ENV_MEMBER:
            spliced = splice_env_member(
                existing,
                unit.env_name or "",
                unit.desired_inner(),
                unit.required_features,
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


def ensure_claude_skills_link(root: Path, link: ClaudeSkillsLink) -> bool:
    """Execute the skills-symlink decision; returns whether this apply CREATED the link."""
    if not link.is_work:
        return False
    dest = root / CLAUDE_SKILLS_DIR
    parent_link = symlinked_dest_component(root, str(Path(CLAUDE_SKILLS_DIR).parent))
    if parent_link is not None:
        logger.warning(
            "claude skills link skipped — a parent of %s became a symlink (%s) in "
            "the gather→apply window; left untouched",
            CLAUDE_SKILLS_DIR,
            parent_link,
            extra={"root": str(root), "path": CLAUDE_SKILLS_DIR},
        )
        return False
    if dest.is_symlink() or dest.exists():
        if dest.is_symlink() and str(dest.readlink()) == CLAUDE_SKILLS_LINK_TARGET:
            return False
        logger.warning(
            "claude skills link skipped — %s appeared in the gather→apply "
            "window; left untouched",
            CLAUDE_SKILLS_DIR,
            extra={"root": str(root), "path": CLAUDE_SKILLS_DIR},
        )
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(CLAUDE_SKILLS_LINK_TARGET, target_is_directory=True)
    logger.info(
        "linked %s -> %s",
        CLAUDE_SKILLS_DIR,
        CLAUDE_SKILLS_LINK_TARGET,
        extra={"root": str(root), "path": CLAUDE_SKILLS_DIR},
    )
    return True


def _rerender_changelog(root: Path) -> bool:
    """Regenerate ``CHANGELOG.md`` from ``CHANGELOG/``; ``False`` when the fragment tree is gone and it skipped."""
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
    """Run ``lefthook install`` through the consumer's own managed pixi lint env, ``check=False``."""
    return pixienv.run_in_env(
        [LEFTHOOK_BINARY, *HOOK_ACTIVATE_ARGV],
        root,
        environment=LINT_ENV,
        check=False,
    )


def _activation_output(result: execrun.ExecResult) -> str:
    """Both streams of an activation run, newline-joined for the renderer's warning."""
    return "\n".join(s for s in (result.stdout, result.stderr) if s)


def consumer_snapshot(root: Path, unit: Unit) -> str:
    """The consumer's current text for a unit — captured BEFORE any overwrite."""
    if unit.kind == "block":
        inner = consumer_inner(root, unit)
        if inner in (SETTINGS_MALFORMED, ENV_MEMBER_MALFORMED, ENV_MEMBER_UNSUPPORTED):
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
    """The FULL git sha of the build performing this install; raises :class:`InstallError` when none resolves."""
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
    """Run the activation boundary, absorbing failure into an ``(ok, detail)`` outcome."""
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
    """Remove ``.old`` hook backups that carry BOTH :data:`LEFTHOOK_SHIM_MARKERS`; best-effort."""
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
    """Unlink a DANGLING symlink at a managed hook path; best-effort, any other ``OSError`` leaves it."""
    hooks_dir = git.hooks_dir(cwd=str(root))
    if hooks_dir is None or not hooks_dir.is_dir():
        return
    for name in MANAGED_HOOK_NAMES:
        hook = hooks_dir / name
        try:
            if not hook.is_symlink():
                continue
            try:
                hook.stat()
            except FileNotFoundError:
                pass
            else:
                continue
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
    """Every consumer path a committing apply may write, delete or stamp — the roll-back set."""
    paths = set(plan.changed_paths)
    if plan.seed_pixi_manifest:
        paths.add(PIXI_FILE)
    paths.add(PIXI_LOCK)
    return sorted(paths)


class _SnapshotCell(NamedTuple):
    """One path's pre-write bytes AND permission bits, so a restore returns the mode too."""

    data: bytes
    mode: int


def _snapshot_committing_writes(
    root: Path, plan: Plan
) -> dict[str, _SnapshotCell | None]:
    """Pre-write cells for every :func:`_snapshot_paths` entry; ``None`` marks a path absent."""
    return {rel: _snapshot_cell(root / rel) for rel in _snapshot_paths(plan)}


def _snapshot_cell(path: Path) -> _SnapshotCell | None:
    """The path's pre-write cell (bytes + permission bits), or ``None`` if absent."""
    if not path.is_file():
        return None
    return _SnapshotCell(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def _restore_committing_writes(
    root: Path, snapshot: dict[str, _SnapshotCell | None]
) -> None:
    """Roll the working tree back to ``snapshot``, best-effort per path, then raise a combined ``OSError``."""
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


def _restore_caller_branch(
    cwd: str, original_ref: str | None, staged_writes: set[str]
) -> None:
    """Switch the caller's checkout back to ``original_ref``; best-effort, never raises.

    ``staged_writes`` members that are UNTRACKED and carried by HEAD and absent
    from ``original_ref`` are staged first — those and only those would make git
    refuse the switch, and staging any other member would clobber the caller's
    own index entry.
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
        if git.current_branch(cwd=cwd) == INSTALL_BRANCH:
            tracked = git.ls_files_matching(sorted(staged_writes), cwd=cwd)
            in_head = git.tree_paths("HEAD", sorted(staged_writes), cwd=cwd)
            in_original = git.tree_paths(original_ref, sorted(staged_writes), cwd=cwd)
            if tracked is not None and in_head is not None and in_original is not None:
                blocked = sorted(set(in_head) - set(tracked) - set(in_original))
                if blocked:
                    git.add(blocked, cwd=cwd)
    except execrun.ExecError:
        logger.warning(
            "could not stage the newly added managed paths into the caller's "
            "index before the install PR restore — a newly added managed path "
            "may block the switch back to %s",
            original_ref,
            exc_info=True,
            extra={"root": cwd, "branch": original_ref},
        )
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
    """Raise :class:`InstallError` on a lefthook merge conflict in a committing mode, before any write."""
    if plan.lefthook_conflicts and mode != MODE_TREE:
        raise InstallError(
            "lefthook config conflict — refusing to publish a managed config "
            "that cannot run:\n"
            + "\n".join(
                f"  {format_lefthook_conflict(c)}" for c in plan.lefthook_conflicts
            )
        )


def reject_symlinked_dests(plan: Plan) -> None:
    """Raise :class:`InstallError` on a symlinked dest component in EVERY mode, before any write."""
    if plan.symlinked_dests:
        raise InstallError(
            "symlinked destination — refusing to write a managed file through a "
            "consumer symlink (it would overwrite the link's target, outside "
            "this repo):\n"
            + "\n".join(f"  {format_symlinked_dest(sd)}" for sd in plan.symlinked_dests)
        )


def reject_pixi_key_conflicts(plan: Plan) -> None:
    """Raise :class:`InstallError` in EVERY mode when a consumer key shadows an undelivered managed pixi block."""
    if plan.pixi_key_conflicts:
        raise InstallError(
            "pixi key conflict — refusing to reconcile a repo that would silently "
            "under-deliver its managed set:\n"
            + "\n".join(
                f"  {format_pixi_key_conflict(kc)}" for kc in plan.pixi_key_conflicts
            )
        )


def reject_stale_provision(plan: Plan) -> None:
    """Raise :class:`InstallError` in EVERY mode on a surviving retired ``shipit provision lexd`` call."""
    if plan.stale_provision:
        raise InstallError(
            "retired command in pixi.toml — refusing to reconcile a repo whose "
            "pixi tasks still call `shipit provision lexd`:\n"
            + "\n".join(
                f"  {format_stale_provision(sp)}" for sp in plan.stale_provision
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

    ``activate_hooks``, ``pr_body``, ``certify`` and ``debt`` inject the
    boundaries; ``pr_body`` is required for :data:`MODE_PR`. Every committing
    mode self-certifies after staging and before any git side effect, rolling
    its writes back and raising :class:`SelfCertError` on a miss. Raises
    :class:`InstallError` on a domain refusal and lets a git/gh failure
    propagate as :class:`~shipit.execrun.ExecError`.
    """
    if mode not in MODES:
        raise ValueError(f"unknown install mode: {mode!r}")
    if mode == MODE_PR and pr_body is None:
        raise ValueError("MODE_PR needs the pr_body renderer")
    reject_lefthook_conflicts(plan, mode)
    reject_symlinked_dests(plan)
    reject_stale_provision(plan)
    reject_pixi_key_conflicts(plan)
    activate = activate_hooks or _activate_hooks
    started = time.monotonic()
    root = Path(plan.root)

    override_before = (
        {d.unit.key: consumer_snapshot(root, d.unit) for d in plan.overrides}
        if mode == MODE_PR
        else {}
    )

    committing_snapshot = (
        _snapshot_committing_writes(root, plan) if mode != MODE_TREE else None
    )

    if plan.seed_pixi_manifest:
        pixi_path = root / PIXI_FILE
        if not pixi_path.is_file():
            pixi_path.write_text(pixi_manifest_seed(root.name), encoding="utf-8")
            logger.info(
                "seeded pixi manifest",
                extra={"root": str(root), "path": PIXI_FILE},
            )

    touched: set[str] = set()
    staged_writes: set[str] = set()
    retired_removals: set[str] = set()

    for d in plan.decisions:
        touched.add(d.unit.dest)
        staged_writes.add(d.unit.dest)
    for d in plan.writes:
        write_unit(root, d.unit)

    rerendered = plan.rerender_changelog and _rerender_changelog(root)
    touched.add(CHANGELOG_FILE)
    if rerendered:
        staged_writes.add(CHANGELOG_FILE)

    for d in plan.retired:
        if d.action == KEEP:
            continue
        touched.add(d.retired.path)
        retired_removals.add(d.retired.path)
        dest = root / d.retired.path
        if d.action == DELETE and dest.is_file():
            dest.unlink(missing_ok=True)
    for d in plan.retire_hook_deletes:
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
            continue
        touched.add(d.retired.file)
        staged_writes.add(d.retired.file)

    if ensure_claude_skills_link(root, plan.claude_skills_link):
        touched.add(CLAUDE_SKILLS_DIR)
        staged_writes.add(CLAUDE_SKILLS_DIR)

    cfg_path = root / config.CONFIG_NAME
    if plan.seeds:
        config.apply_policy_seed(cfg_path, toolchains=config.derive_toolchains(root))
    new_managed = {d.unit.key: d.desired_hash for d in plan.decisions}
    stamped_version = _shipit_version()
    config.write_manifest(cfg_path, version=stamped_version, managed=new_managed)
    touched.add(config.CONFIG_NAME)
    staged_writes.add(config.CONFIG_NAME)
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

    hooks_activated: bool | None = None
    hooks_detail = ""
    if plan.writes and plan.activates_hooks:
        _preclean_stale_hook_backups(root)
        _preclean_dangling_hook_symlinks(root)
        hooks_activated, hooks_detail = _activate(root, activate)
        if not hooks_activated:
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

    certifier = certify or selfcert.certify
    cert_report = certifier(
        plan,
        root,
        hooks_activated=hooks_activated,
        stamped_pin=stamped_version,
    )
    if not cert_report.ok:
        message = selfcert.format_failure(cert_report)
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

    if mode == MODE_PR:
        debt_reader = debt or selfcert.consumer_debt
        result = replace(result, lint_debt=debt_reader(root))

    changed_paths = list(plan.changed_paths)
    if plan.rerender_changelog and not rerendered:
        changed_paths = [p for p in changed_paths if p != CHANGELOG_FILE]
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

        original_ref = git.current_branch(cwd=cwd)
        # Fetch BEFORE resolving the default branch: with `<remote>/HEAD` absent,
        # `default_branch` probes the remote-tracking refs a fetch populates.
        git.fetch(cwd=cwd)
        base_branch = git.default_branch(cwd=cwd)
        try:
            git.switch_create(INSTALL_BRANCH, cwd=cwd)
            git.reset_soft(f"origin/{base_branch}", cwd=cwd)
            if (root / PIXI_LOCK).is_file():
                touched.add(PIXI_LOCK)
                staged_writes.add(PIXI_LOCK)
            with tempfile.TemporaryDirectory(prefix="shipit-pr-index-") as index_dir:
                index_file = str(Path(index_dir) / "index")
                git.read_tree(f"origin/{base_branch}", cwd=cwd, index_file=index_file)
                git.add(sorted(staged_writes), cwd=cwd, index_file=index_file)
                # Removals go through `git rm --cached --ignore-unmatch`, never
                # `git add`, which errors on an absent or untracked pathspec.
                git.rm_cached(sorted(retired_removals), cwd=cwd, index_file=index_file)
                pr_paths = git.staged_paths(
                    sorted(touched), cwd=cwd, index_file=index_file
                )
                if not pr_paths:
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
                # Whole-index, never a pathspec commit: a pathspec commit runs
                # git's PARTIAL-commit mode, which builds the tree from the
                # working tree and disregards the index — silently dropping the
                # `rm --cached` deletions staged above.
                git.commit_all(
                    COMMIT_MESSAGE, cwd=cwd, no_verify=True, index_file=index_file
                )
            git.push(INSTALL_BRANCH, cwd=cwd, force=True, no_verify=True)
            existing = gh.pr_url_for_head(INSTALL_BRANCH, cwd=cwd)
            if existing:
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
            _restore_caller_branch(cwd, original_ref, staged_writes)
    except execrun.ExecError:
        logger.error(
            "install git/gh step failed", exc_info=True, extra={"root": str(root)}
        )
        raise
