"""The deep repository-creation orchestrator — ``shipit repo new``'s one API.

This is the deep module the CLI is thin over (``docs/spec/repo-new.md`` §Design
Decisions): its small interface takes a name, a parent, and the selected stacks
and returns a typed :class:`CreationResult`, owning every step in between —
request preflight, plan composition, staging, managed installation, pixi
provisioning, staged verification, the root commit, and the atomic publish.
Callers never coordinate those steps.

The atomic-rename contract (ADR-0059) is the spine of the effectful path: the
complete, verified, initially-committed Repo is built in a temporary sibling
UNDER the requested parent (staging and destination share one filesystem), and
only after every step succeeds does one :func:`os.rename` publish it. Any
handled failure removes the temporary sibling and leaves the destination in its
preflight state (absent stays absent; an empty directory stays empty); a cleanup
failure is reported but never publishes a partial Repo.

Every effect that reaches the world — managed install, pixi provisioning, the
staged Checks, and Git — is a seam with a real default and an injectable
override, so the orchestration (preflight, staging, ordering, rollback, publish)
is exercised without a full Rust toolchain while the command wires the real
effects (ADR-0062; the injection detail ADR-0055–0063 leave to the module).
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import git, pixienv
from .errors import CreationError
from .names import validate_name
from .plan import CreationPlan, build_plan
from .profiles import resolve_profiles

logger = logging.getLogger("shipit.repocreate")

#: The public Checks creation certifies the staged Repo against, in order
#: (``docs/spec/repo-new.md``): the same public pixi tasks a user runs.
CHECKS: tuple[str, ...] = ("lint", "test", "build")

#: pixi's long-runner bound — a cold provision or a first-activation Check
#: re-solve is provisioning-shaped work, so it shares pixi's own budget rather
#: than the Exec runner's 5-minute default. Aliased to the single source of
#: truth (:data:`shipit.pixienv.INSTALL_TIMEOUT`) so the two cannot drift.
_LONG_TIMEOUT: float = pixienv.INSTALL_TIMEOUT

# Seam type aliases — each takes the staged Repo root and performs its effect.
Effect = Callable[[Path], None]


@dataclass(frozen=True)
class CreationResult:
    """What a successful :func:`create_repo` produced (ADR-0030).

    ``destination`` is the published Repo path; ``initial_commit`` the root
    commit's sha; ``stacks`` the selected profile keys. A value returned only
    on full success — a handled failure raises :class:`CreationError` and
    publishes nothing.
    """

    destination: Path
    initial_commit: str
    stacks: tuple[str, ...]


def _preflight(parent: Path, name_value: str) -> tuple[Path, Path]:
    """Validate the parent and derive+validate the destination (ADR-0059).

    The parent must already exist as a writable, traversable directory (a
    symlink resolving to one is accepted); creation never creates missing parent
    structure. The destination is always ``parent/name`` and must be absent or an
    empty directory — a file, a symlink, or a directory containing ANY entry
    (including a hidden one) is refused before anything is written.
    """
    resolved = parent.resolve() if parent.is_symlink() else parent
    if not resolved.is_dir():
        raise CreationError(f"parent {parent} is not an existing directory")
    # Both write AND traverse (execute) are required: creation creates the
    # staging sibling under the parent and stats/renames entries within it, and
    # a non-traversable parent (W_OK without X_OK) would accept preflight only to
    # fail mid-creation.
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise CreationError(f"parent {parent} is not a writable, traversable directory")
    # Anchor the destination to the user-supplied `parent`, NOT its resolved
    # target: when `parent` is a symlink to a directory we accept it, but the
    # created/reported path must stay `parent/name` (a path *through* the symlink)
    # so it matches the docstring and the CLI's `<parent>/<name>` contract rather
    # than surprising the caller with the real path behind the link. Staging still
    # uses the resolved parent so the publish rename stays same-filesystem.
    dest = parent / name_value
    _assert_absent_or_empty(dest)
    return resolved, dest


def _assert_absent_or_empty(dest: Path) -> None:
    """Refuse a destination that is not absent or an empty directory.

    Every probe here (``exists``/``is_symlink``/``is_dir``/``iterdir``) hits the
    filesystem and can raise ``OSError`` — e.g. a destination that exists but is
    not readable/traversable raises ``PermissionError`` (``EACCES`` propagates
    from the stat-based checks too, not just ``iterdir``). Any such error is
    re-raised as :class:`CreationError` so an uninspectable destination stays on
    the verb's ``error: …`` + exit-1 refusal contract rather than escaping as a
    raw traceback.
    """
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
    """Write every consumer-owned file of ``plan`` into ``root``."""
    for owned in plan.files:
        dest = root / owned.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(owned.text, encoding="utf-8")
        if owned.executable:
            dest.chmod(0o755)


def default_installer(root: Path) -> None:
    """Orchestrate the existing install domain in-process (ADR-0055).

    Runs the unchanged gather → reconcile → apply pipeline in ``MODE_TREE``:
    the managed catalog is written into the working tree (no commit — creation
    owns the single root commit) and the installed hooks are activated. The
    toolchain catalog is signal-conditional off the tracked Cargo manifest, so
    the scaffold must already be ``git add``-ed when this runs (creation stages
    before installing) for the managed Rust block to ship.

    ``MODE_TREE`` degrades a failed ``lefthook install`` to a warning
    (``hooks_activated is False``) rather than aborting — right for install's
    own working-tree refresh, wrong for creation, whose contract is that the
    managed baseline INCLUDING active hooks is in place before the initial
    commit (so that commit runs the hooks). So creation fails CLOSED here: a
    false activation raises :class:`CreationError`, the staging sibling is
    cleaned up, and nothing is published.
    """
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

    toolchains = detect_toolchains(root)
    units = load_units(toolchains=toolchains)
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
    """Resolve and lock the staged Repo's pixi environment (writes ``pixi.lock``).

    Runs ``pixi install`` through a SCRUBBED environment (ADR-0062) so an
    inherited ``PIXI_*`` project pointer from the invoking checkout cannot bind
    the child to a different manifest; the ``--manifest-path`` the pixi adapter
    adds pins resolution to the staged Repo regardless.
    """
    scrubbed = pixienv.scrub_env(dict(os.environ))
    pixienv.install(root, env=scrubbed)


def default_verifier(root: Path) -> None:
    """Certify the staged Repo through the user shell seam (ADR-0062).

    Runs ``pixi run lint``, ``pixi run test``, and ``pixi run build`` — the
    public pixi TASKS, exactly as a user would — from a child rooted in the
    staged Repo, with a scrubbed environment and an explicit ``--manifest-path``
    so inherited pixi activation cannot select the invoking checkout. The first
    failing Check raises :class:`CreationError`, which prevents publication.
    """
    scrubbed = pixienv.scrub_env(dict(os.environ))
    for task in CHECKS:
        result = pixienv.run_task(
            task, root, env=scrubbed, check=False, timeout=_LONG_TIMEOUT
        )
        if not result.ok:
            raise CreationError(
                f"staged Check `pixi run {task}` failed (rc={result.rc}); "
                "the Repo was not published"
            )


def default_author(root: Path) -> str:
    """The resolved Git author name for the MIT ``LICENSE`` copyright holder.

    Resolves the author through :func:`git.author_name` (``git var
    GIT_AUTHOR_IDENT``) from ``root`` (after ``git init``), so it uses the SAME
    identity the ``Initial commit`` will — honoring ``GIT_AUTHOR_NAME``/
    ``GIT_AUTHOR_EMAIL`` and the full ``user.*`` config chain, not one config
    key. It ALSO probes the committer identity (:func:`git.committer_name`,
    ``git var GIT_COMMITTER_IDENT``), which git resolves INDEPENDENTLY of the
    author: a setup with only ``GIT_AUTHOR_*`` set resolves an author but no
    committer, and the ``Initial commit`` needs both. Requiring both here means
    an unresolvable identity is a creation PREFLIGHT failure
    (``docs/spec/repo-new.md``: never a template placeholder), raised BEFORE any
    effect rather than as a raw commit-time git error mid-creation.
    """
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
    installer: Effect = default_installer,
    provisioner: Effect = default_provisioner,
    verifier: Effect = default_verifier,
    author_reader: Callable[[Path], str] = default_author,
    year: int | None = None,
) -> CreationResult:
    """Create, verify, and publish a new local Repo — the whole flow.

    Resolves the stacks and validates the name (usage-shaped
    :class:`CreationError`), preflights the parent/destination, then stages the
    complete Repo in a temporary sibling: writes the plan, initializes Git on
    ``main``, stages the scaffold, installs the managed baseline, provisions and
    locks pixi, runs the three public Checks, and creates the ``Initial commit``.
    Only after every step succeeds does one atomic rename publish it at the
    destination. Any handled failure removes the temporary sibling and leaves
    the destination in its preflight state.

    ``year`` defaults to the local creation year; the effect seams default to
    the real implementations and are injected in tests.
    """
    profiles = resolve_profiles(stacks)
    name = validate_name(raw_name)
    resolved_parent, dest = _preflight(parent, name.value)
    creation_year = year if year is not None else datetime.date.today().year

    staging = Path(tempfile.mkdtemp(dir=resolved_parent, prefix=".shipit-repo-new-"))
    # `mkdtemp` hard-codes 0o700, and `os.rename` publishes the directory mode
    # verbatim — so a published Repo would be `rwx------`, breaking shared
    # workspaces and container mounts and diverging from `git init`/`cargo new`,
    # which respect the user's umask. Widen the staging root to the umask-derived
    # mode (typically 0o755) before it is published. Read the umask WITHOUT
    # mutating process-global state: an `os.umask` set/restore probe reads only
    # by writing, imposing its momentary mask on any file a concurrent thread
    # creates in the window — an unacceptable side effect for an orchestration
    # library. Instead create a throwaway 0o777 directory inside the staging
    # root, which the OS masks atomically on creation (`0o777 & ~umask`), and
    # read the umask-derived bits straight back off it. Preserve any high-order
    # bits `mkdtemp` inherited from the parent (e.g. an SGID group-inheritance
    # bit on a shared workspace) by only rewriting the low 9 permission bits.
    # Guard EVERYTHING after the staging root exists — including the umask probe
    # and chmod below — so any filesystem error (permissions, disk) removes the
    # temporary sibling instead of leaking it.
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
        # Stage the scaffold BEFORE install so the tracked Cargo manifest
        # signals the Rust toolchain to the managed catalog.
        git.add_all(cwd=str(staging))
        installer(staging)
        provisioner(staging)
        verifier(staging)
        git.add_all(cwd=str(staging))
        git.commit_all("Initial commit", cwd=str(staging))
        head = git.head_commit(cwd=str(staging))
        if head is None:
            raise CreationError("Initial commit did not produce a resolvable HEAD")
        _publish(staging, dest)
    except BaseException:
        _cleanup(staging)
        raise
    return CreationResult(
        destination=dest,
        initial_commit=head.value,
        stacks=tuple(p.key for p in profiles),
    )


def _publish(staging: Path, dest: Path) -> None:
    """Atomically rename the staged Repo to ``dest`` after a final empty-recheck.

    The destination must still be absent or empty at publish time (ADR-0059);
    ``os.rename`` is the one same-filesystem atomic step — it creates ``dest``
    when absent and replaces an existing empty directory in place.
    """
    _assert_absent_or_empty(dest)
    os.rename(staging, dest)
    logger.info("published new Repo", extra={"destination": str(dest)})


def _cleanup(staging: Path) -> None:
    """Remove the temporary sibling on a handled failure; report if it cannot.

    A cleanup failure never publishes the partial Repo — it is logged and the
    original failure continues to propagate (creation still returns non-zero).
    """
    try:
        shutil.rmtree(staging, ignore_errors=False)
    except OSError:
        logger.exception(
            "failed to remove staging directory after a creation failure",
            extra={"staging": str(staging)},
        )
