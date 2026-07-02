"""``tree/create`` — the effectful orchestrator that materializes a *ready* Tree.

``create(spec, ...) -> Tree`` turns a pure :class:`~shipit.tree.layout.TreePlan`
into a real, independent, **provisioned** checkout on disk and returns the READY
summary (``{path, branch, base}``). The whole pipeline hides behind this one call
(PRD Implementation Decisions):

1. ``git clone --reference <local> --dissociate <github-url> <dir>`` — a tiny,
   instant, yet fully INDEPENDENT clone (ADR-0014); see
   :func:`shipit.gh.git_clone_dissociated`.
2. ``git fetch origin`` then ``git checkout -b <branch> <base>``.
3. apply ``.treeinclude`` — copy the gitignored-but-needed files (``.env``,
   Doppler config, models) from the source checkout into the new Tree
   (:mod:`shipit.tree.include`).
4. provision: ``shipit install`` then the path's ``pixi install`` / ``npm ci``,
   run with the parent's project-pointer env scrubbed (:func:`provision_env`). The
   ADR-0015 build env (per-Tree ``target/``, ``SCCACHE_BASEDIRS``, ``CARGO_INCREMENTAL=0``)
   is no longer injected here — it lives in pixi ``[activation.env]`` (COR01 / ADR-0022),
   so pixi sets it on every activation and it reaches the agent's own in-Tree ``cargo``.

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

import platformdirs

from .. import config, execrun, gh, logcontext
from . import include, provision
from .layout import TreeSpec, central_root, plan

logger = logging.getLogger("shipit.tree")

#: Provisioning REQUIRES an ALREADY-ONBOARDED ``.shipit.toml`` (one with a
#: ``[shipit]``/``[managed]`` block — :func:`shipit.config.is_onboarded`): a
#: non-onboarded target fails closed (#210). Given an onboarded repo, the checkout's
#: manifests drive the rest: the managed-set reconcile always runs, a ``pixi.toml``
#: gets ``pixi install``, a ``package.json`` gets ``npm ci`` — each dep step gated on
#: its file existing, so a repo that uses only one toolchain runs only that step.
PIXI_MANIFEST = "pixi.toml"
NPM_MANIFEST = "package.json"

#: The per-step provisioning timeout, in seconds: 30 minutes. Provisioning is the
#: known long-runner family (ADR-0028 names cold ``pixi install`` / ``npm ci``),
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
            "tree create: cloning %s -> %s (branch %s, base %s)",
            github_url,
            dest,
            tree_plan.branch,
            tree_plan.base,
        )
        try:
            gh.git_clone_dissociated(github_url, str(dest), reference=source_repo)
            gh.git_fetch(cwd=str(dest))
            gh.git_checkout_new_branch(tree_plan.branch, tree_plan.base, cwd=str(dest))
            copied = include.apply(source_repo, dest)
            logger.debug(
                "tree create: copied %d .treeinclude file(s) into %s", len(copied), dest
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
        logger.info(
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


#: ``PIXI_*`` variables the parent ``pixi run`` injects that bind to the PARENT
#: project/manifest/environment. They MUST NOT leak into a child shipit/pixi
#: operating inside a DIFFERENT clone: a leaked ``PIXI_PROJECT_MANIFEST`` makes the
#: clone's ``pixi run lint`` resolve the parent manifest, where ``lint`` is
#: ambiguous across the ``default``/``lint``/``review`` environments, so the
#: install commit's pre-commit hook dies (#167). This is the same env-leak class as
#: ADR-0019's ``ANTHROPIC_API_KEY`` finding — an inherited var breaking a child
#: rooted elsewhere — and the fix is the same: scrub it. Cache-location vars are
#: user-level (not project-bound), so they are kept (see :func:`is_leaked_env_var`)
#: to preserve cross-Tree package-cache sharing.
PIXI_CACHE_VARS = frozenset({"PIXI_CACHE_DIR", "RATTLER_CACHE_DIR"})

#: The Conda **activation** vars that bind a process to the PARENT env — exactly the
#: ones a ``conda activate`` (and pixi's own activation, which is conda-shaped) set on
#: entry. They MUST be scrubbed for the same reason as the ``PIXI_*`` pointers: a leaked
#: ``CONDA_PREFIX`` / ``CONDA_DEFAULT_ENV`` keeps a child bound to the PARENT env's
#: activation, so ``python`` / tooling resolve there instead of the child's own Tree. The
#: stacked ``CONDA_PREFIX_<n>`` an activation *stack* leaves behind is caught by prefix in
#: :func:`is_leaked_env_var`. **Installation-level** vars (``CONDA_EXE``,
#: ``CONDA_PYTHON_EXE``, ``CONDA_ROOT``, ``_CE_*``) are user-/install-level, NOT project
#: pointers, so they are KEPT — dropping them wholesale could change subprocess behavior
#: (including ``pixi run`` itself in a Conda-managed shell).
CONDA_ACTIVATION_VARS = frozenset(
    {"CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "CONDA_PROMPT_MODIFIER"}
)

#: The ADR-0015 build-env vars that pixi ``[activation.env]`` now OWNS and re-sets to a
#: PER-TREE value (via ``$PIXI_PROJECT_ROOT``) on every activation (COR01 / ADR-0022).
#: These are exactly the three keys declared in ``pixi.toml``'s ``[activation.env]``. Because
#: the build env now comes from pixi ``[activation.env]`` (no longer injected in Python), an
#: inherited PARENT value would
#: SHADOW the Tree's activation value — a leaked ``CARGO_TARGET_DIR`` / ``SCCACHE_BASEDIRS``
#: points the child's ``cargo`` at the PARENT Tree's ``target/`` and keys sccache on the
#: PARENT path, so build artifacts land in — and cache-hit against — the WRONG Tree. They
#: are the same leak class as the ``PIXI_*`` / Conda pointers: strip the inherited value so
#: pixi's per-Tree ``[activation.env]`` value is authoritative. NOT scrubbed (kept, same as
#: the cache/installation carve-outs): ``RUSTC_WRAPPER`` (the install-level sccache binary
#: pointer — dropping it would DISABLE sccache in the child, and it is not per-Tree) and the
#: ``SCCACHE_*`` cache/credential vars (``SCCACHE_DIR`` / ``SCCACHE_GCS_KEY`` — the child
#: NEEDS them to reach the shared cache backend; they are user-/backend-level, not per-Tree
#: paths).
BUILD_ENV_VARS = frozenset(
    {"CARGO_TARGET_DIR", "SCCACHE_BASEDIRS", "CARGO_INCREMENTAL"}
)


def is_leaked_env_var(key: str) -> bool:
    """Whether ``key`` is a parent-project env pointer to scrub from a Tree child.

    The single source of truth for "which inherited vars bind to the PARENT project and
    must not leak into a child rooted in a different clone". Three leak classes:

    - ``PIXI_*`` project pointers (all ``PIXI_*`` except the user-level cache vars in
      :data:`PIXI_CACHE_VARS`).
    - Conda **activation** vars (:data:`CONDA_ACTIVATION_VARS` and the stacked
      ``CONDA_PREFIX_<n>``) — SCOPED to activation-binding vars only; installation-level
      ``CONDA_*`` (``CONDA_EXE`` / ``CONDA_PYTHON_EXE`` / ``CONDA_ROOT`` / ``_CE_*``) is
      KEPT, since scrubbing all ``CONDA_*`` could break ``pixi run`` in a Conda shell.
    - ADR-0015 **build-env** vars (:data:`BUILD_ENV_VARS`) that pixi ``[activation.env]``
      re-sets PER-TREE — SCOPED to the three per-Tree-path keys; install-/backend-level
      ``RUSTC_WRAPPER`` and ``SCCACHE_*`` cache/credential vars are KEPT (dropping them
      would disable sccache or cut the child off from the shared cache).

    Both the provisioning env (:func:`provision_env`) and the launch env
    (:func:`shipit.spawn.launch.scrub_tree_env`) scrub SOLELY on this predicate, so the
    two paths can never drift on any carve-out.
    """
    if key.startswith("PIXI_"):
        return key not in PIXI_CACHE_VARS
    if key in CONDA_ACTIVATION_VARS or key.startswith("CONDA_PREFIX_"):
        return True
    if key in BUILD_ENV_VARS:
        return True
    return False


def provision_env() -> dict[str, str]:
    """The COMPLETE environment for a provisioning command run inside a Tree.

    A copy of the current environment with the parent's leaked ``PIXI_*`` / Conda
    activation / ADR-0015 build-env project pointers removed (:func:`is_leaked_env_var`).
    Returned as the full env — not an overlay — so :func:`run_provision` can hand it to
    :func:`shipit.execrun.run` with ``replace_env=True``: a merge could re-add the very vars
    we are dropping (they live in ``os.environ``), so removal requires replacing the env,
    not merging onto it. With the project pointers gone, the child ``pixi`` / ``shipit``
    re-resolves the project from its own cwd (the Tree), which is the whole point.

    The ADR-0015 build env (per-Tree ``target/`` + ``SCCACHE_BASEDIRS`` +
    ``CARGO_INCREMENTAL=0``) is NO LONGER built here: it lives in pixi ``[activation.env]``
    (COR01 / ADR-0022), where ``$PIXI_PROJECT_ROOT`` expands to the same per-Tree absolute
    paths on EVERY activation — so it now reaches the agent's own in-Tree ``cargo``, not
    just this provisioning subprocess (which never builds Rust anyway). A parent value for
    those same keys is now SCRUBBED (:data:`BUILD_ENV_VARS` in :func:`is_leaked_env_var`)
    so an inherited ``CARGO_TARGET_DIR`` / ``SCCACHE_BASEDIRS`` can never shadow the Tree's
    own per-activation value and mis-route the child's build artifacts.
    """
    return {k: v for k, v in os.environ.items() if not is_leaked_env_var(k)}


def run_provision(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    """Run one provisioning command in the Tree (the patchable provisioning boundary).

    A thin wrapper over :func:`shipit.execrun.run` (no shell) so tests assert *which*
    commands provisioning would run and *with what env* without spawning ``pixi`` /
    ``npm``. ``env`` is the COMPLETE child environment (built by
    :func:`provision_env`) and is used verbatim (``replace_env=True``) so the
    scrubbed ``PIXI_*`` vars cannot creep back in via a merge over ``os.environ``.

    Every step carries the explicit generous :data:`PROVISION_TIMEOUT` (ADR-0028
    names cold ``pixi install`` / ``npm ci`` as the legitimate long-runners the
    5-minute default must not kill; the bound replaces WS01's ``timeout=None``
    stopgap so a wedged step still dies at a known point). The runner gives every
    step a durable record — timing on success (DEBUG), and on failure an ERROR
    record carrying both stream tails, which is exactly where a broken
    ``pixi install`` writes its real diagnostics — closing the documented
    "no provisioning logs" gap. On top of the transport record, the step's timing
    is narrated at INFO on the tree logger, so Tree-birth timing is readable from
    the domain log without dropping to DEBUG.
    """
    result = execrun.run(
        cmd, cwd=str(cwd), env=env, replace_env=True, timeout=PROVISION_TIMEOUT
    )
    logger.info(
        "provision step %s completed in %dms",
        shlex.join(result.argv),
        result.duration_ms,
        extra={"duration_ms": result.duration_ms},
    )


def _provision(dest: Path, *, trees_root: Path) -> None:
    """Provision the freshly-checked-out Tree so a write-session starts ready.

    Runs ``shipit install --local`` (only when the repo is ALREADY ONBOARDED), then
    the path's ``pixi install`` / ``npm ci``, each gated on its manifest existing and
    each run with the scrubbed provisioning env (:func:`provision_env` — parent project
    pointers removed; the ADR-0015 build env is no longer injected here, it comes from
    pixi ``[activation.env]`` on activation). Before ``pixi install`` it checks (and only
    *warns* about — #119) the pixi-cache / Trees-root same-filesystem invariant.

    The install runs in ``--local`` mode (#170): it commits the managed set on the
    Tree's already-checked-out planned branch with NO branch switch, NO push, and NO
    PR. The default consumer-onboarding install would instead switch to
    ``shipit/install``, force-push it, and open a draft PR — polluting origin on
    every Tree creation and leaving HEAD on the wrong branch. Provisioning only
    needs the managed files committed in the Tree, never any origin side effect.

    When that install DOES commit (a managed-set drift window — the repo's committed
    set lags the running shipit), the commit SHA(s) are recorded in
    ``.git/shipit-provision.json`` (:mod:`shipit.tree.provision`) so the ephemeral
    gc ladder can exclude exactly them from its unpushed floor instead of keeping
    the Tree forever over a commit shipit itself made (#232). Recording is
    best-effort/additive (like the liveness pidfile): an unreadable HEAD, an
    unresolvable range, or a failed write degrades to *not recorded*, which the
    ladder reads conservatively as KEEP — never a failed Tree creation.

    Provisioning is gated on :func:`shipit.config.is_onboarded` and FAILS CLOSED: a
    Tree cut from a repo with no ``[shipit]``/``[managed]`` block raises, rather than
    silently skipping the reconcile (#210, revisiting #206). "Operate on a
    non-onboarded repo" is meaningless in steady state — a repo shipit spawns Trees in
    IS onboarded by definition — so a non-onboarded target is a real misconfiguration,
    and shipit's ethos everywhere else is to fail loud (ADR-0017), never to tolerate a
    degraded state silently. Onboarding a repo is a deliberate act (``shipit install``),
    never a Tree-prep side effect: a repo that carries ``.shipit.toml`` for consumer
    policy (``[secrets]`` / ``[reviewers]`` / ``[project]``) but no managed block would
    otherwise be ONBOARDED fresh on every spawn, committing the onboarding artifacts
    into the spawned branch and polluting every work-stream PR (#205). The loud
    :class:`ValueError` is caught by the spawn/tree callers and rendered as a clean
    exit-1 pointing at ``shipit install`` (never an escaping traceback).
    """
    if not config.is_onboarded(dest / config.CONFIG_NAME):
        raise ValueError(
            f"repo {dest} is not onboarded — run `shipit install` first "
            "(a Tree can only be provisioned from a repo shipit manages)"
        )
    env = provision_env()
    head_before = gh.git_head_commit(cwd=str(dest))
    run_provision(["shipit", "install", ".", "--local"], cwd=dest, env=env)
    _record_install_commits(dest, head_before=head_before)
    if (dest / PIXI_MANIFEST).is_file():
        _warn_if_cache_cross_filesystem(trees_root)
        run_provision(["pixi", "install"], cwd=dest, env=env)
    if (dest / NPM_MANIFEST).is_file():
        run_provision(["npm", "ci"], cwd=dest, env=env)


def _record_install_commits(dest: Path, *, head_before: str | None) -> None:
    """Record what the managed-set install just committed, best-effort (#232).

    Compares ``HEAD`` before and after the ``shipit install --local`` step; when it
    moved, the commits in between are the install's reconcile (the only step of the
    provisioning pipeline that commits) and their SHAs go into the Tree's
    ``.git/shipit-provision.json``. Every failure — an unreadable HEAD on either
    side, an unresolvable range, a failed write — degrades to *no record* with a
    WARNING: the gc ladder then simply keeps the Tree (the pre-#232 behavior), so
    the record is purely additive and can never break Tree creation.
    """
    cwd = str(dest)
    head_after = gh.git_head_commit(cwd=cwd)
    if head_before is None or head_after is None or head_after == head_before:
        return
    shas = gh.git_commits_between(head_before, head_after, cwd=cwd)
    if not shas:
        logger.warning(
            "provisioning moved HEAD in %s but the commit range was unreadable; "
            "not recording a provision commit (gc will keep the Tree, #232)",
            dest,
        )
        return
    try:
        provision.write_record(dest, shas)
    except OSError:
        logger.warning(
            "could not record the provisioning commit(s) for %s; gc will keep "
            "the Tree (#232)",
            dest,
            exc_info=True,
        )


# --------------------------------------------------------------------------
# #119 — pixi cache / Trees root same-filesystem check (warn, never fail)
# --------------------------------------------------------------------------


def pixi_cache_dir() -> Path:
    """The directory pixi/rattler caches downloaded packages in.

    Honors ``PIXI_CACHE_DIR`` / ``RATTLER_CACHE_DIR`` overrides, else the platform
    default (``<user-cache>/rattler/cache``).
    """
    override = os.environ.get("PIXI_CACHE_DIR") or os.environ.get("RATTLER_CACHE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir("rattler")) / "cache"


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
    """Emit the #119 cross-filesystem warning (WARNING+ → console) when it applies."""
    message = check_same_filesystem(trees_root, pixi_cache_dir())
    if message:
        logger.warning(message)


def create_from_source(spec: TreeSpec, *, source_repo: str | Path) -> Tree:
    """:func:`create` with ``github_url`` resolved from ``source_repo``'s ``origin``.

    The Tree clones from — and points ``origin`` at — exactly the URL the source
    checkout already uses, so auth and ``gh`` behave identically inside the Tree.
    """
    source = str(source_repo)
    url = gh.git_remote_url(cwd=source)
    return create(spec, source_repo=source, github_url=url)
