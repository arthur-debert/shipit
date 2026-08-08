"""``repocreate/create`` — the repository-creation orchestrator; publishes by atomic rename."""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import execrun, git, pixienv, redact
from . import standout
from .errors import CreationError
from .names import ProjectName, validate_name
from .plan import CreationPlan, build_plan
from .profiles import RustProfile, RustScaffold, require_rust_only, resolve_profiles

logger = logging.getLogger("shipit.repocreate")

#: The public pixi tasks the staged Repo is certified against, in order.
CHECKS: tuple[str, ...] = ("lint", "test", "build")

#: Staged Checks are provisioning-shaped, so they share pixi's own budget.
_LONG_TIMEOUT: float = pixienv.INSTALL_TIMEOUT

Effect = Callable[[Path], None]

#: A scaffold producer: build the Rust application tree inside a private scratch dir.
Producer = Callable[[ProjectName, Path], RustScaffold]


@dataclass(frozen=True)
class CreationResult:
    """What a successful :func:`create_repo` produced; never returned on failure."""

    destination: Path
    initial_commit: str
    stacks: tuple[str, ...]


def _preflight(parent: Path, name_value: str) -> tuple[Path, Path]:
    resolved = parent.resolve() if parent.is_symlink() else parent
    if not resolved.is_dir():
        raise CreationError(f"parent {parent} is not an existing directory")
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise CreationError(f"parent {parent} is not a writable, traversable directory")
    # The destination stays a path THROUGH any symlinked parent; only staging
    dest = parent / name_value
    _assert_absent_or_empty(dest)
    return resolved, dest


def _assert_absent_or_empty(dest: Path) -> None:
    """Refuse a destination that is not absent or an empty directory, or cannot be inspected."""
    try:
        if not dest.exists() and not dest.is_symlink():
            return
        if dest.is_symlink():
            raise CreationError(f"destination {dest} is a symlink; refusing")
        if not dest.is_dir():
            raise CreationError(f"destination {dest} already exists; refusing")
        if any(dest.iterdir()):
            raise CreationError(
                f"destination {dest} is a non-empty directory; refusing"
            )
    except OSError as exc:
        raise CreationError(
            f"destination {dest} could not be inspected: {exc}"
        ) from exc


def _write_plan(plan: CreationPlan, root: Path) -> None:
    for owned in plan.files:
        dest = root / owned.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(owned.text, encoding="utf-8")
        if owned.executable:
            dest.chmod(0o755)


def default_installer(root: Path) -> None:
    """Install the managed baseline into the staged tree; raise if hooks did not activate."""
    from ..install.apply import MODE_TREE
    from ..install.apply import apply as apply_plan
    from ..install.reconcile import (
        detect_toolchains,
        gather,
        load_retired,
        load_retired_hooks,
        reconcile,
    )
    from ..install.units import load_units
    from ..verbs.install import (
        _declared_endpoints,
        _declared_platforms,
        _declared_signals,
    )

    toolchains = detect_toolchains(root) | _declared_signals(root)
    endpoints = _declared_endpoints(root)
    platforms = _declared_platforms(root)
    units = load_units(toolchains=toolchains, endpoints=endpoints, platforms=platforms)
    retired = load_retired()
    retired_hooks = load_retired_hooks()
    state = gather(root, units, retired, retired_hooks)
    plan = reconcile(units, retired, state, retired_hooks)
    result = apply_plan(plan, MODE_TREE)
    if result.hooks_activated is False:
        raise CreationError(
            "managed git hooks did not activate during creation "
            f"({result.hooks_detail.strip()}); the Repo was not published"
        )


def default_provisioner(root: Path) -> None:
    scrubbed = pixienv.scrub_env(dict(os.environ))
    pixienv.install(root, env=scrubbed)


def default_verifier(root: Path) -> None:
    """Run every Check in :data:`CHECKS` against the staged Repo; the first failure raises."""
    scrubbed = pixienv.scrub_env(dict(os.environ))
    for task in CHECKS:
        result = pixienv.run_task(
            task, root, env=scrubbed, check=False, timeout=_LONG_TIMEOUT
        )
        if not result.ok:
            raise CreationError(_check_failure_message(task, result))


def _check_failure_message(task: str, result: execrun.ExecResult) -> str:
    """The staged Check's failure message; redaction runs BEFORE the tail slice."""
    tails = [
        f"{label}:\n{tail}"
        for label, stream in (("stdout", result.stdout), ("stderr", result.stderr))
        if (tail := redact.redact_text(stream)[-execrun.TAIL_CHARS :].strip())
    ]
    output = ("\n" + "\n".join(tails)) if tails else ""
    return (
        f"staged Check `pixi run {task}` failed (rc={result.rc}); "
        f"the Repo was not published{output}"
    )


def default_author(root: Path) -> str:
    """The Git author name for the ``LICENSE`` holder; raises unless BOTH identities resolve."""
    name = git.author_name(cwd=str(root))
    committer = git.committer_name(cwd=str(root))
    if not name or not committer:
        raise CreationError(
            "git could not resolve a full author + committer identity; configure "
            "it before creating a Repo — e.g. `git config --global user.name "
            '"Your Name"` and `git config --global user.email "you@example.com"` '
            "(or set GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL and GIT_COMMITTER_NAME/"
            "GIT_COMMITTER_EMAIL)"
        )
    return name


def create_repo(
    raw_name: str,
    parent: Path,
    stacks: tuple[str, ...],
    *,
    standout_wizard: bool = False,
    producer: Producer = standout.produce,
    installer: Effect = default_installer,
    provisioner: Effect = default_provisioner,
    verifier: Effect = default_verifier,
    author_reader: Callable[[Path], str] = default_author,
    year: int | None = None,
) -> CreationResult:
    """Create, verify, and publish a new local Repo; ``year`` defaults to the creation year."""
    if standout_wizard:
        require_rust_only(stacks, "--standout-wizard")
    profiles = resolve_profiles(stacks)
    name = validate_name(raw_name)
    resolved_parent, dest = _preflight(parent, name.value)
    creation_year = year if year is not None else datetime.date.today().year
    if standout_wizard:
        profiles = (RustProfile(scaffold=_scaffold(producer, name)),)

    staging = Path(tempfile.mkdtemp(dir=resolved_parent, prefix=".shipit-repo-new-"))
    # `mkdtemp` hard-codes 0o700 and the rename publishes that mode verbatim, so
    # widen to the umask, read off a throwaway dir (an `os.umask` probe would
    # impose its momentary mask on concurrent threads).
    try:
        probe = staging / ".shipit-umask-probe"
        probe.mkdir(mode=0o777)
        umask_mode = probe.stat().st_mode & 0o777
        probe.rmdir()
        current_mode = staging.stat().st_mode
        staging.chmod((current_mode & ~0o777) | umask_mode)
        logger.info(
            "staging new Repo",
            extra={"staging": str(staging), "destination": str(dest)},
        )
        git.init_main(cwd=str(staging))
        author = author_reader(staging)
        plan = build_plan(name, profiles, author=author, year=creation_year)
        _write_plan(plan, staging)
        # Stage BEFORE install so tracked manifests signal toolchains to the catalog.
        git.add_all(cwd=str(staging))
        installer(staging)
        provisioner(staging)
        verifier(staging)
        git.add_all(cwd=str(staging))
        git.commit_all("Initial commit", cwd=str(staging))
        head = git.head_commit(cwd=str(staging))
        if head is None:
            raise CreationError("Initial commit did not produce a resolvable HEAD")
        # Certification artifacts bake in the staging path; strip before renaming.
        git.clean_non_committed(cwd=str(staging))
        _relocate_hook_shims(staging, dest)
        _publish(staging, dest)
    except BaseException as primary:
        cleanup_report = _cleanup(staging)
        if cleanup_report is not None:
            primary.add_note(cleanup_report)
        raise
    return CreationResult(
        destination=dest,
        initial_commit=head.value,
        stacks=tuple(p.key for p in profiles),
    )


def _scaffold(producer: Producer, name: ProjectName) -> RustScaffold:
    """Run ``producer`` in a private scratch directory, always removed; its product is data."""
    scratch = Path(tempfile.mkdtemp(prefix="shipit-repo-new-scaffold-"))
    try:
        return producer(name, scratch)
    finally:
        try:
            shutil.rmtree(scratch)
        except OSError:
            logger.exception(
                "failed to remove the scaffold scratch directory",
                extra={"scratch": str(scratch)},
            )


def _relocate_hook_shims(staging: Path, dest: Path) -> None:
    """Rewrite the staging path lefthook baked into each hook shim; skips symlinked hooks."""
    hooks = staging / ".git" / "hooks"
    if not hooks.is_dir():
        return
    needle = str(staging if staging.is_absolute() else Path.cwd() / staging)
    replacement = str(dest if dest.is_absolute() else Path.cwd() / dest)
    for shim in hooks.iterdir():
        if not shim.is_file() or shim.is_symlink():
            continue
        # `newline=""` keeps the POSIX `\n` endings a bash shim needs verbatim.
        body = shim.read_text(encoding="utf-8", errors="surrogateescape", newline="")
        if needle not in body:
            continue
        shim.write_text(
            body.replace(needle, replacement),
            encoding="utf-8",
            errors="surrogateescape",
            newline="",
        )


def _publish(staging: Path, dest: Path) -> None:
    _assert_absent_or_empty(dest)
    os.rename(staging, dest)
    logger.info("published new Repo", extra={"destination": str(dest)})


def _cleanup(staging: Path) -> str | None:
    """Remove the temporary sibling; ``None`` when gone, else a report of why it could not be."""
    try:
        shutil.rmtree(staging, ignore_errors=False)
        return None
    except OSError as exc:
        logger.exception(
            "failed to remove staging directory after a creation failure",
            extra={"staging": str(staging)},
        )
        return (
            f"additionally, the staging sibling {staging} could not be removed: {exc}"
        )
