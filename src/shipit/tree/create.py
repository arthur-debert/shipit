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
   ``git fetch origin`` and ``git checkout -B <branch> <base>`` (``-B`` so a
   freeform Tree on the repo's default branch works — #845), then
   ``git submodule sync --recursive`` then ``update --init --recursive``
   (:func:`shipit.git.submodule_update_init`, #485/#486) — a dissociated clone leaves
   submodules as empty gitlinks, so a Tree of a submodule-using consumer must populate
   them to match CI's ``submodules: recursive`` (the ``sync`` first keeps a reused
   reviewer clone's submodule URLs in step with an advanced head).
3. apply ``.treeinclude`` — copy the gitignored-but-needed files (``.env``,
   Doppler config, models) from the source checkout into the new Tree
   (:mod:`shipit.tree.include`).
4. provision: the path's ``pixi install`` / the package-manager-aware frozen
   node install (:func:`node_install_argv`, #543) + hook activation,
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

import json
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
#: adapter, a ``package.json`` gets its package manager's frozen install
#: (:func:`node_install_argv`, #543) — each dep step gated on its file
#: existing, so a repo that uses only one toolchain runs only that step.
NODE_MANIFEST = "package.json"

#: Package manager → its FROZEN install argv: install exactly what the lockfile
#: pins and rewrite nothing, matching what CI runs. Frozen is non-negotiable —
#: a plain ``install`` could mutate the lockfile on the just-cut branch, and
#: provisioning mutates nothing (ADR-0033 in spirit). ``npm ci`` and pnpm's
#: ``--frozen-lockfile`` spell "frozen" identically across their whole version
#: range; yarn does NOT — it renamed the flag across the v1→v2 line — so yarn is
#: resolved by version (:func:`_yarn_install_argv`), not this table (#545).
NODE_INSTALL_ARGV: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "ci"),
    "pnpm": ("pnpm", "install", "--frozen-lockfile"),
}

#: The recognized node package managers: the version-stable table above plus yarn
#: (whose frozen argv is version-dependent, so it carries no fixed entry). This is
#: the membership a ``packageManager`` pin is validated against.
NODE_MANAGERS: frozenset[str] = frozenset(NODE_INSTALL_ARGV) | {"yarn"}

#: Yarn v1 ("classic") stamps a ``# yarn lockfile v1`` banner near the top of every
#: ``yarn.lock``; Berry (v2+) writes a YAML ``__metadata:`` map with no such banner.
#: The banner is the stable, documented way to tell a classic lockfile from a Berry
#: one WITHOUT a ``packageManager`` pin — yarn renamed its frozen-install flag across
#: that boundary (``--frozen-lockfile`` → ``--immutable``), so the leg must know which
#: line it faces to pick the flag that will not hard-fail (#545).
_YARN_V1_BANNER = "# yarn lockfile v1"

#: Lockfile → the package manager that owns it — the fallback detection signal
#: when ``package.json`` carries no ``packageManager`` pin (#543).
NODE_LOCKFILES: dict[str, str] = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
}

#: The per-step provisioning timeout, in seconds: 30 minutes. Provisioning is the
#: known long-runner family (ADR-0028 names the cold frozen node install —
#: ``npm ci`` and its pnpm/yarn equivalents — alongside the pixi
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
            # A dissociated clone leaves submodules as EMPTY gitlink dirs (#485): a
            # consumer whose suite reads submodule-backed fixtures (lex's `comms/specs`)
            # would fail in the Tree though it is green in a normal checkout. Populate
            # them recursively — matching CI's `submodules: recursive` — right after the
            # branch is cut and before provisioning, so `pixi run test` sees a complete
            # checkout. A submodule-less repo is a clean no-op; a fetch failure fails
            # loud and rolls the leaf back (never a silently empty submodule dir).
            git.submodule_update_init(cwd=str(dest))
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
    names the cold frozen node install — ``npm ci`` and its pnpm/yarn
    equivalents — as a legitimate long-runner the 5-minute default must
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


def _yarn_install_argv(*, classic: bool) -> list[str]:
    """Yarn's frozen install argv for the classic (v1) vs Berry (v2+) line (#545).

    Yarn renamed the frozen-install flag across the v1→v2 boundary — classic
    honours ``--frozen-lockfile``, Berry only ``--immutable`` — for the SAME
    "install exactly the lockfile, rewrite nothing" intent. The two flags are not
    interchangeable: passing Berry's ``--immutable`` to a v1 yarn (or vice versa)
    HARD-FAILS on an unknown flag, so the leg selects on the detected major version
    rather than assuming one line.
    """
    flag = "--frozen-lockfile" if classic else "--immutable"
    return ["yarn", "install", flag]


def _yarn_pin_is_classic(pin: str, manifest: Path) -> bool:
    """Whether a ``yarn@<version>`` corepack pin names the classic (v1) line (#545).

    ``packageManager`` is an exact ``<name>@<version>`` (corepack rejects a range),
    so the major version is the leading integer of ``<version>`` and ``major <= 1``
    is classic. A yarn pin whose version has no numeric major is malformed for
    corepack — raise rather than guess a frozen flag.
    """
    _, _, version = pin.partition("@")
    major = version.split(".", 1)[0]
    try:
        return int(major) <= 1
    except ValueError as exc:
        raise ValueError(
            f"unparseable yarn version in packageManager {pin!r} in {manifest}: "
            "corepack pins an exact <name>@<version>, so yarn's frozen-install flag "
            "(--frozen-lockfile for v1, --immutable for v2+) cannot be chosen (#545)"
        ) from exc


def _yarn_lockfile_is_classic(lockfile: Path) -> bool:
    """Whether a lone ``yarn.lock`` (no packageManager pin) is a v1 "classic" file.

    Reads only the head — the :data:`_YARN_V1_BANNER` sits in the first two lines —
    and looks for the banner. Its absence means a Berry (v2+) lockfile, whose frozen
    flag is ``--immutable`` (#545).
    """
    with lockfile.open("r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(len(_YARN_V1_BANNER) + 256)
    return _YARN_V1_BANNER in head


def node_install_argv(dest: Path) -> list[str]:
    """The frozen node-deps install argv for the checkout at ``dest`` (#543).

    Tree provisioning used to hard-code ``npm ci``, which HARD-FAILS on a
    pnpm/yarn repo (no ``package-lock.json``) — and because the svelte prettier
    leg fails open when its plugins are unresolvable (#498/#542), the miss was
    SILENT: dirty ``.svelte`` files passed without a verdict. So the manager is
    detected, and an undecidable manifest fails LOUD:

    1. the ``packageManager`` field in ``package.json`` (the corepack pin,
       ``<name>@<version>``) is AUTHORITATIVE when present — it is the repo's
       own declaration, the one corepack and CI already honour — and it wins
       over any lockfile on disk. The exact ``<name>@<version>`` shape is
       enforced: a bare name with no pinned version is malformed for corepack
       and raises rather than being read as a usable signal;
    2. otherwise the lockfile decides: exactly one of the
       :data:`NODE_LOCKFILES` names its manager;
    3. anything else — an unrecognized or shapeless ``packageManager``, an
       unparseable or non-object ``package.json``, no recognized lockfile, or
       several — raises
       :class:`ValueError`, failing the materialization (rolled back like any
       provisioning failure). A repo that declares node deps but whose manager
       cannot be determined must never be half-provisioned: the cost downstream
       is not an error but a silent fail-open.

    Yarn is the one manager whose frozen argv is not fixed by name: v1 "classic"
    and v2+ "Berry" spell the frozen flag differently (``--frozen-lockfile`` vs
    ``--immutable``, #545), so the yarn major version — from the pin's
    ``<version>``, or from the ``yarn.lock`` banner on the lockfile-only path —
    picks the flag; a yarn pin with no numeric major raises like any other
    undecidable signal.
    """
    manifest = dest / NODE_MANIFEST
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ValueError(
            f"unparseable {NODE_MANIFEST} in {dest}: {exc} — cannot determine "
            "the package manager for the node-deps provisioning step (#543)"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{NODE_MANIFEST} in {dest} is JSON but not an object "
            f"({type(data).__name__}); a package.json is always an object, so no "
            "package manager can be read — failing loud like an unparseable one (#543)"
        )
    pin = data.get("packageManager")
    if pin is not None:
        name, sep, version = str(pin).partition("@")
        if not sep or not version:
            raise ValueError(
                f"malformed packageManager {pin!r} in {manifest}: corepack pins an "
                "exact <name>@<version>, so a bare name (no pinned version) is not a "
                "usable signal — failing loud rather than guessing a frozen install "
                "(#543)"
            )
        if name == "yarn":
            return _yarn_install_argv(classic=_yarn_pin_is_classic(str(pin), manifest))
        if name not in NODE_INSTALL_ARGV:
            raise ValueError(
                f"unsupported packageManager {pin!r} in {manifest}: known "
                f"managers are {sorted(NODE_MANAGERS)} (#543)"
            )
        return list(NODE_INSTALL_ARGV[name])
    found = [lock for lock in NODE_LOCKFILES if (dest / lock).is_file()]
    if len(found) == 1:
        manager = NODE_LOCKFILES[found[0]]
        if manager == "yarn":
            return _yarn_install_argv(
                classic=_yarn_lockfile_is_classic(dest / found[0])
            )
        return list(NODE_INSTALL_ARGV[manager])
    detail = (
        f"multiple lockfiles ({', '.join(found)})"
        if found
        else f"no recognized lockfile ({', '.join(NODE_LOCKFILES)})"
    )
    raise ValueError(
        f"{dest} has a {NODE_MANIFEST} but no packageManager field and {detail}; "
        "refusing to guess a frozen install — a wrong one hard-fails here or "
        "leaves deps unprovisioned that downstream lint legs fail open on (#543)"
    )


def _provision(dest: Path, *, trees_root: Path) -> None:
    """Provision the freshly-checked-out Tree so a write-session starts ready.

    Provisioning mutates NOTHING managed (ADR-0033): it is clone + branch +
    env + hook activation. The TRE03-era ``shipit install
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
    do not clone, so a Tree must arm its own) — / the package-manager-aware
    frozen node install (:func:`node_install_argv`, #543: ``npm ci`` on a pnpm
    or yarn repo hard-fails, and the svelte prettier leg then fails open
    silently), each gated on
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
    if (dest / NODE_MANIFEST).is_file():
        run_provision(node_install_argv(dest), cwd=dest, env=env)


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
