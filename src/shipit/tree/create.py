"""``tree/create`` — the effectful orchestrator that materializes a *ready* Tree.

``create(spec, ...) -> Tree`` turns a pure :class:`~shipit.tree.layout.TreePlan`
into a real, independent, **provisioned** checkout on disk and returns the READY
summary (``{path, branch, base}``). The whole pipeline hides behind this one call
(PRD Implementation Decisions):

1. ``git clone --reference <local> --dissociate <github-url> <dir>`` — a tiny,
   instant, yet fully INDEPENDENT clone (ADR-0014); see
   :func:`shipit.git.clone_dissociated`.
2. harden the fresh clone as a future ``--reference`` donor
   (:func:`shipit.git.configure_safe_reference_donor`, #353), then
   ``git fetch origin`` and ``git checkout -b <branch> <base>``.
3. apply ``.treeinclude`` — copy the gitignored-but-needed files (``.env``,
   Doppler config, models) from the source checkout into the new Tree
   (:mod:`shipit.tree.include`).
4. provision: the path's ``pixi install`` / ``npm ci`` + hook activation,
   run with the parent's project-pointer env scrubbed (:func:`provision_env`) —
   NO managed-set mutation (ADR-0033: the TRE03-era ``shipit install --local``
   reconcile is deleted; the Shipit pin keeps Tree and tool coherent by
   construction). The ADR-0015 build env (per-Tree ``target/``,
   ``SCCACHE_BASEDIRS``, ``CARGO_INCREMENTAL=0``) is no longer injected here —
   it lives in pixi ``[activation.env]`` (COR01 / ADR-0022), so pixi sets it on
   every activation and it reaches the agent's own in-Tree ``cargo``.

Materialization stays atomic from the caller's view: if any step fails, the
half-built leaf is removed before the error propagates. Every git call goes
through the :mod:`shipit.gh` boundary and every provisioning command through
:func:`run_provision`, so both are patchable; the integration smoke exercises the
real git path end to end, while the planning/matching truth tables are unit-tested
in ``layout`` / ``include``.
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config, events, execrun, git, logcontext, pixienv
from ..install.apply import HOOK_ACTIVATE_ARGV, LEFTHOOK_BINARY
from ..install.units import LEFTHOOK_FILE, LINT_ENV
from . import include
from .layout import TreeSpec, central_root, plan

#: The Tree axis' shared logger (LOG02 spray, ADR-0029): the creation pipeline
#: narrates its milestones at INFO with durations ("tree created …", per
#: provision step), mechanics at DEBUG, and a failed create at ERROR with the
#: exception attached — under a :func:`shipit.logcontext.scoped` ``tree`` bind
#: so every record of one materialization correlates.
logger = logging.getLogger("shipit.tree")

#: Provisioning REQUIRES a base carrying the Shipit pin (``.shipit.toml``
#: ``[shipit].version`` — :func:`shipit.config.shipit_pin`): a pinless target
#: fails closed (ADR-0033's one surviving guard). Given a pinned repo, the
#: checkout's manifests drive the rest: a ``pixi.toml``
#: (:data:`shipit.pixienv.MANIFEST_NAME`) gets ``pixi install`` through the pixi
#: adapter, a ``package.json`` gets ``npm ci`` — each dep step gated on its file
#: existing, so a repo that uses only one toolchain runs only that step.
NPM_MANIFEST = "package.json"

#: The per-step provisioning timeout, in seconds: 30 minutes. Provisioning is the
#: known long-runner family (ADR-0028 names cold ``npm ci`` alongside the pixi
#: install the pixi adapter now bounds itself — :data:`shipit.pixienv.INSTALL_TIMEOUT`),
#: so the runner's 5-minute default would kill legitimate cold installs — but
#: ``None`` (the pre-WS03 stopgap) let a hung step hang Tree creation forever.
#: 30 minutes is generous enough for a cold solve+download on a slow link, tight
#: enough that a wedged step still dies at a known bound with a durable record.
PROVISION_TIMEOUT: float = 30 * 60.0

#: Bytes of randomness behind an agent hash → 8 hex chars. Enough to keep two
#: concurrent Trees for the same issue from colliding on disk without bloating the
#: dir name.
_HASH_BYTES = 4


@dataclass(frozen=True)
class Tree:
    """A materialized Tree — the READY summary a caller prints/consumes."""

    path: str
    branch: str
    base: str


def new_agent_hash() -> str:
    """A short random hex tag that disambiguates a Tree's directory (never its branch)."""
    return secrets.token_hex(_HASH_BYTES)


def create(spec: TreeSpec, *, source_repo: str, github_url: str) -> Tree:
    """Materialize the Tree described by ``spec`` and return its READY summary.

    ``source_repo`` is the local checkout whose object store seeds the clone
    (``--reference``); ``github_url`` is the remote the new Tree's ``origin`` points
    at. The leaf directory's parents are created first (``git clone`` makes only
    the leaf), then the clone is dissociated, fetched, and put on the planned
    branch cut from the planned base.

    Materialization is atomic from the caller's view: if any step after the clone
    fails, the half-built leaf is removed before the error propagates, so a failed
    ``create`` never leaves a partial Tree on disk for the next run to trip over.

    The rollback ``rmtree`` only ever removes a directory THIS call created: a
    pre-existing ``dest`` (a deterministic/colliding agent hash, or a rerun for the
    same issue) is refused up front with :class:`FileExistsError`, so a failed clone
    can never clobber a Tree — or any other directory — that was already on disk.
    """
    tree_plan = plan(spec)
    dest = tree_plan.dir
    trees_root = spec.root if spec.root is not None else central_root()
    # The Tree-birth seam binds its domain keys (ADR-0029), but SCOPED to the
    # creation pipeline: every record of the birth — including the Exec runner's
    # own transport records for the git/provisioning subprocesses — carries the
    # Tree it belongs to, and the binding is unwound when `create` returns so a
    # later in-process record (an embedded caller that then lists/gcs/removes a
    # DIFFERENT Tree) never inherits this Tree/session and corrupts the durable
    # correlation. The session identity is bound when the shape carries one: the
    # ephemeral leaf IS the per-launch session id (ADR-0027), and the issue shape
    # names its branch-leaf session (`issues/<id>/<session>`); `scoped` drops the
    # `None` the other shapes yield (present-when-bound, absent-not-null).
    session = spec.ephemeral or (spec.session if spec.issue is not None else None)
    with logcontext.scoped(tree=str(dest), session=session):
        if dest.exists():
            raise FileExistsError(
                f"tree dir already exists: {dest}; refusing to clone so a failed "
                "create never deletes a pre-existing checkout (rerun, or hash collision)."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        logger.debug(
            "tree cloning %s -> %s (branch %s, base %s)",
            github_url,
            dest,
            tree_plan.branch,
            tree_plan.base,
        )
        try:
            git.clone_dissociated(github_url, str(dest), reference=source_repo)
            # BEFORE the first fetch: a minted Tree must never grow the split
            # commit-graph chain that poisons it as a `--reference` donor for
            # its own children's clones (#353) — and `fetch.writeCommitGraph`
            # is exactly a fetch-time writer.
            git.configure_safe_reference_donor(cwd=str(dest))
            git.fetch(cwd=str(dest))
            git.checkout_new_branch(tree_plan.branch, tree_plan.base, cwd=str(dest))
            copied = include.apply(source_repo, dest)
            logger.debug(
                "tree copied %d .treeinclude file(s) into %s", len(copied), dest
            )
            _provision(dest, trees_root=Path(trees_root))
        except BaseException:
            # The propagating failure's ERROR record (spray convention): the whole
            # birth story — how far it got, how long it took, the exception — plus
            # the rollback the atomicity contract performs, in one durable record.
            logger.error(
                "tree create failed after %dms; removing half-built leaf %s",
                _elapsed_ms(started),
                dest,
                exc_info=True,
            )
            shutil.rmtree(dest, ignore_errors=True)
            raise

        duration_ms = _elapsed_ms(started)
        # The birth milestone IS the `tree.created` dev-cycle event (ADR-0032,
        # verb-witnessed): the same record as before, tagged — the scoped
        # `tree`/`session` keys (and any spawn-seam epic/ws/agent/role bound by
        # the caller) ride in via the pipeline's context-merge.
        events.emit(
            logger,
            "tree.created",
            "tree created at %s (branch %s, base %s) in %dms",
            dest,
            tree_plan.branch,
            tree_plan.base,
            duration_ms,
            extra={"duration_ms": duration_ms},
        )
        return Tree(path=str(dest), branch=tree_plan.branch, base=tree_plan.base)


def _elapsed_ms(started: float) -> int:
    """Milliseconds elapsed since the ``time.monotonic()`` timestamp ``started``."""
    return int((time.monotonic() - started) * 1000)


def provision_env() -> dict[str, str]:
    """The COMPLETE environment for a provisioning command run inside a Tree.

    A copy of the current environment with the parent's leaked ``PIXI_*`` / Conda
    activation / ADR-0015 build-env project pointers removed — the scrub rules are
    pixi domain knowledge and live in the pixi adapter
    (:func:`shipit.pixienv.scrub_env` over the one predicate
    :func:`shipit.pixienv.is_leaked_env_var`, PROC02-WS02), which the launch path
    (:func:`shipit.spawn.launch.scrub_tree_env`) shares so the two can never drift.
    Returned as the full env — not an overlay — so :func:`run_provision` /
    :func:`shipit.pixienv.install` can hand it to :func:`shipit.execrun.run` with
    ``replace_env=True``: a merge could re-add the very vars we are dropping (they
    live in ``os.environ``), so removal requires replacing the env, not merging onto
    it. With the project pointers gone, the child ``pixi`` / ``shipit`` re-resolves
    the project from its own cwd (the Tree), which is the whole point.

    The ADR-0015 build env (per-Tree ``target/`` + ``SCCACHE_BASEDIRS`` +
    ``CARGO_INCREMENTAL=0``) is NO LONGER built here: it lives in pixi ``[activation.env]``
    (COR01 / ADR-0022), where ``$PIXI_PROJECT_ROOT`` expands to the same per-Tree absolute
    paths on EVERY activation — so it now reaches the agent's own in-Tree ``cargo``, not
    just this provisioning subprocess (which never builds Rust anyway). A parent value for
    those same keys is SCRUBBED (:data:`shipit.pixienv.BUILD_ENV_VARS`) so an inherited
    ``CARGO_TARGET_DIR`` / ``SCCACHE_BASEDIRS`` can never shadow the Tree's own
    per-activation value and mis-route the child's build artifacts.
    """
    return pixienv.scrub_env(os.environ)


def run_provision(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run one provisioning command in the Tree (the patchable provisioning boundary).

    A thin wrapper over :func:`shipit.execrun.run` (no shell) so tests assert *which*
    commands provisioning would run and *with what env* without spawning ``pixi`` /
    ``npm``. ``env`` is the COMPLETE child environment (built by
    :func:`provision_env`) and is used verbatim (``replace_env=True``) so the
    scrubbed ``PIXI_*`` vars cannot creep back in via a merge over ``os.environ``.

    Every step carries the explicit generous :data:`PROVISION_TIMEOUT` (ADR-0028
    names cold ``npm ci`` as a legitimate long-runner the 5-minute default must
    not kill; the bound replaces WS01's ``timeout=None`` stopgap so a wedged step
    still dies at a known point — the pixi step carries the pixi adapter's own
    bound instead). The runner gives every step a durable record — timing on
    success (DEBUG), and on failure an ERROR record carrying both stream tails,
    which is exactly where a broken install writes its real diagnostics — closing
    the documented "no provisioning logs" gap. On top of the transport record, the
    step's timing is narrated at INFO on the tree logger (:func:`_narrate_step`),
    so Tree-birth timing is readable from the domain log without dropping to DEBUG.
    """
    result = execrun.run(
        cmd, cwd=str(cwd), env=env, replace_env=True, timeout=PROVISION_TIMEOUT
    )
    _narrate_step(result)


def _narrate_step(result: execrun.ExecResult) -> None:
    """Narrate one completed provisioning step at INFO on the tree logger.

    Shared by :func:`run_provision` and the pixi-adapter install step so every
    provisioning Exec — whichever seam ran it — lands in the domain log with its
    argv and duration.
    """
    logger.info(
        "provision step %s completed in %dms",
        shlex.join(result.argv),
        result.duration_ms,
        extra={"duration_ms": result.duration_ms},
    )


def _provision(dest: Path, *, trees_root: Path) -> None:
    """Provision the freshly-checked-out Tree so a write-session starts ready.

    Provisioning mutates NOTHING managed (ADR-0033): it is clone + branch +
    env + hook activation + provenance record. The TRE03-era ``shipit install
    --local`` step — and the reconcile commit it fail-closed into on the
    just-cut branch during every tool/managed-set drift window — is DELETED:
    the Shipit pin makes Tree and tool coherent by construction (a Tree cut
    from base X runs the shipit pinned at X, via the managed ``bin/shipit``
    launcher), so the incoherence that step papered over no longer exists, and
    its ``chore(shipit)`` commits no longer pollute feature PRs. A newer
    shipit changes a consumer only via a reconcile PR.

    What runs: the path's ``pixi install`` (through the pixi adapter,
    :func:`shipit.pixienv.install` — the pixi argv and its long-runner bound
    are pixi knowledge, PROC02-WS02) — followed, when the clone carries a
    ``lefthook.yml``, by hook activation (:func:`_activate_hooks`, #443: hooks
    do not clone, so a Tree must arm its own) — / ``npm ci``, each gated on
    its manifest existing and each run with the scrubbed provisioning env
    (:func:`provision_env` — parent project pointers removed; the ADR-0015
    build env is no longer injected here, it comes from pixi
    ``[activation.env]`` on activation). Before ``pixi install`` it checks
    (and only *warns* about — #119) the pixi-cache / Trees-root
    same-filesystem invariant.

    Provisioning is gated on the base carrying a Shipit pin
    (:func:`shipit.config.shipit_pin`) and FAILS CLOSED — ADR-0033's one
    surviving guard: a Tree cut from a PINLESS base has no build for its
    ``bin/shipit`` to exec, so every in-Tree verb (hooks, lint, ``pr next``)
    would fail 127 after the expensive clone. Bootstrapping a repo is a
    deliberate act (the bootstrap ``shipit install --pr``, which stamps the
    pin), never a Tree-prep side effect (#205/#210 unchanged in spirit). The
    loud :class:`ValueError` is caught by the spawn/tree callers and rendered
    as a clean exit-1 pointing at the bootstrap (never an escaping traceback).
    """
    if config.shipit_pin(dest / config.CONFIG_NAME) is None:
        raise ValueError(
            f"repo {dest} has no [shipit].version pin — run the bootstrap "
            "`shipit install --pr` first (ADR-0033: a Tree rides its base's "
            "pinned shipit; a pinless base has nothing for bin/shipit to exec)"
        )
    env = provision_env()
    if (dest / pixienv.MANIFEST_NAME).is_file():
        _warn_if_cache_cross_filesystem(trees_root)
        _narrate_step(pixienv.install(dest, env=env))
        if (dest / LEFTHOOK_FILE).is_file():
            _activate_hooks(dest, env=env)
    if (dest / NPM_MANIFEST).is_file():
        run_provision(["npm", "ci"], cwd=dest, env=env)


def _activate_hooks(dest: Path, *, env: dict[str, str]) -> None:
    """Activate the Tree's git hooks — a fresh clone comes up ARMED (#443).

    Git hooks do not clone: a dissociated Tree cut from a repo that already
    carries the managed ``lefthook.yml`` has only ``*.sample`` hooks, the
    managed-set reconcile above is a NOOP in steady state (so apply's own
    opportunistic activation never fires), and nothing else ever ran
    ``lefthook install`` in the Tree — every spawned agent would commit with no
    lint gate and no dev-cycle commit events. So activation is a first-class
    provisioning step: the SAME one activation definition apply uses
    (:data:`~shipit.install.apply.LEFTHOOK_BINARY` +
    :data:`~shipit.install.apply.HOOK_ACTIVATE_ARGV`), run through the Tree's
    OWN pixi lint env (:data:`~shipit.install.units.LINT_ENV`, where the
    managed blocks pin ``lefthook`` — nothing host-global is assumed), with the
    scrubbed provisioning env. Gated on the manifest pair like every dep step:
    inside the pixi branch (the lint env IS a pixi env) and on
    ``lefthook.yml`` existing. Unlike apply's opportunistic activation this
    step is CHECKED: a Tree that cannot arm its hooks is a failed
    materialization (fail loud, ADR-0017), rolled back like any other
    provisioning failure. Worst case is a first ``pixi run -e lint`` solving
    the lint env — provisioning-shaped work the adapter's own long-runner
    bound covers.
    """
    result = pixienv.run_in_env(
        [LEFTHOOK_BINARY, *HOOK_ACTIVATE_ARGV],
        dest,
        environment=LINT_ENV,
        env=env,
    )
    _narrate_step(result)


# --------------------------------------------------------------------------
# #119 — pixi cache / Trees root same-filesystem check (warn, never fail)
# --------------------------------------------------------------------------


def _st_dev(path: Path) -> int:
    """The filesystem device id backing ``path`` (a patchable seam for the FS check).

    Raises ``OSError`` when ``path`` does not exist; callers that must tolerate an
    absent leaf go through :func:`_nearest_dev`.
    """
    return os.stat(path).st_dev


def _nearest_dev(path: Path) -> int | None:
    """Device id of ``path``, or of its nearest existing ancestor when it is absent.

    A device id is a property of the *filesystem*, so the closest existing ancestor
    of a not-yet-created path sits on the same filesystem that path will be created
    on. Probing upward is what lets the #119 check work on a **first** run — exactly
    when it matters — before the Trees root or the pixi cache directory exists.
    Returns ``None`` only when nothing up the chain can be stat'd.
    """
    for candidate in (path, *path.parents):
        try:
            return _st_dev(candidate)
        except OSError:
            continue
    return None


def check_same_filesystem(trees_root: Path, cache_dir: Path) -> str | None:
    """A warning when ``trees_root`` and ``cache_dir`` are on DIFFERENT filesystems.

    pixi links packages out of its cache into each Tree's environment; when the
    cache and the Trees root share a filesystem that linking is near-free, but
    across filesystems it silently falls back to **full copies** (#119). Returns the
    warning string in that case, else ``None``. Either path may not exist yet (the
    cache dir is created by the first ``pixi install``); we compare the device of
    the nearest existing ancestor, so the warning still fires on a first run. Only
    when neither path nor any ancestor can be stat'd do we give up → ``None`` (the
    check warns, never fails).
    """
    trees_dev = _nearest_dev(trees_root)
    cache_dev = _nearest_dev(cache_dir)
    if trees_dev is None or cache_dev is None:
        return None
    if trees_dev != cache_dev:
        return (
            f"pixi cache ({cache_dir}) and Trees root ({trees_root}) are on "
            "different filesystems; package linking falls back to full copies, "
            "so Tree provisioning will be slower and use more disk (#119)."
        )
    return None


def _warn_if_cache_cross_filesystem(trees_root: Path) -> None:
    """Emit the #119 cross-filesystem warning (WARNING+ → console) when it applies.

    Where the cache lives is pixi knowledge (:func:`shipit.pixienv.cache_dir`);
    comparing it to the Trees root is Tree policy, so the check stays here.
    """
    message = check_same_filesystem(trees_root, pixienv.cache_dir())
    if message:
        logger.warning(message)


def create_from_source(spec: TreeSpec, *, source_repo: str | Path) -> Tree:
    """:func:`create` with ``github_url`` resolved from ``source_repo``'s ``origin``.

    The Tree clones from — and points ``origin`` at — exactly the URL the source
    checkout already uses, so auth and ``gh`` behave identically inside the Tree.
    """
    source = str(source_repo)
    url = git.remote_url(cwd=source)
    return create(spec, source_repo=source, github_url=url)
