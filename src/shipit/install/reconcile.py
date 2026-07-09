"""The reconcile decision core — hash compares in, one frozen :class:`Plan` out.

Reconciliation is a HASH COMPARE, not a subsystem. Per managed unit there are
four outcomes and no more — the moment it grows features it has become the
drift engine this design exists to delete (docs/dev/lessons-learned.lex §4):

  - absent in the consumer            -> ADD      (write it; record its hash)
  - present, hash == desired          -> NOOP     (already current; nothing to do)
  - present, hash == stored pristine  -> UPDATE   (overwrite silently; advance pristine)
  - present, hash != stored pristine  -> OVERRIDE (consumer-edited: still propose
                                                    shipit's content on the PR
                                                    branch, but FLAG it with a diff
                                                    so the human decides at merge)

Install also decides a RETIRED-FILES pass (docs/prd/rvw01-sole-requester.md,
ADR-0031): a packaged manifest (``retired-files.toml``) lists paths shipit used
to distribute that must no longer exist, each with every known pristine
content hash. Three outcomes, same safety philosophy — never destroy a local
edit:

  - absent                              -> NOOP   (already gone)
  - present, hash in known pristines    -> DELETE (safe: it is shipit's own content)
  - present, hash matches NO known one  -> KEEP   (locally modified: warn, keep)

Install also runs a LEFTHOOK MERGE-CONFLICT tripwire (#544): lefthook merges a
consumer's committed ``lefthook-local.yml`` over the managed ``lefthook.yml``
and refuses a merged hook where both ``piped`` and ``parallel`` are true —
crashing BEFORE any check runs, so every ``git commit`` in that consumer is
blocked. The managed caller deliberately sets NO hook-level execution-order
option, but a future managed edit (or an old managed copy) can reintroduce the
class, so the reconcile detects it against the DESIRED managed content and the
Plan carries the conflicts: the working-tree mode warns loudly, the committing
modes fail closed (:mod:`shipit.install.apply`) — a managed-config change must
never silently brick a consumer's commits.

Install also runs a PIXI KEY-CONFLICT guard on first block splices (#547
round 1): a pixi block unit is exact bytes anchored under a TOML table the
consumer owns (the node deps block lands in ``[dependencies]``), so a consumer
who already pins one of the block's keys there (their own ``nodejs``, say)
would get a DUPLICATE TOML KEY on the ADD splice — an unparseable pixi.toml
that blocks installs and every hooked commit. Gather detects the clash against
the parsed consumer manifest, and the reconcile SKIPS delivering that block
(the consumer's own pin stays authoritative; the Plan carries the conflict and
every surface warns) — never a broken write, in any mode. Its pixi-run-level
sibling (TOL01-WS01) guards ``[tasks]`` blocks the same way against TASK-NAME
ambiguity: a managed default-env task (``test``) also defined by a consumer
``[feature.*.tasks]`` table would make ``pixi run <task>`` refuse the name,
so the block is skipped and the consumer's own task stays authoritative
(:class:`PixiTaskConflict`).

The seam (ADR-0030): :func:`gather` is the ONE filesystem read boundary
(consumer hashes, the stored pristine map, the policy-seed plan — a frozen
:class:`ConsumerState`); :func:`detect_toolchains` is its small signal-scoped
sibling (#547: one tracked-manifest read through the git adapter, deciding
WHICH catalog :func:`~shipit.install.units.load_units` returns, before gather
hashes it); :func:`reconcile` is pure over those values and aggregates every
managed and retired decision into the frozen :class:`Plan`, inspectable before
any file is written. All writes live in :mod:`shipit.install.apply`.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from .. import config, git
from .errors import InstallError
from .splice import extract_block, extract_settings_hook
from .units import (
    FMT_JSON_HOOK,
    LEFTHOOK_FILE,
    PIXI_FILE,
    TOOLCHAIN_GO,
    TOOLCHAIN_NODE,
    TOOLCHAIN_RUST,
    Unit,
    data_bytes,
)

logger = logging.getLogger("shipit.install")

ADD = "add"
NOOP = "noop"
UPDATE = "update"
OVERRIDE = "override"

# Retired-files outcomes (docs/prd/rvw01-sole-requester.md). NOOP is shared:
# an absent retired file is the same nothing-to-do as a current managed unit.
DELETE = "delete"
KEEP = "keep"

#: The packaged retired-files manifest (data — retiring a file is an entry, not code).
RETIRED_MANIFEST = "retired-files.toml"


# --------------------------------------------------------------------------
# Managed-unit decisions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    unit: Unit
    action: str
    desired_hash: str
    consumer_hash: str | None
    pristine_hash: str | None


def decide(
    *, consumer_hash: str | None, pristine_hash: str | None, desired_hash: str
) -> str:
    """The reconciliation outcome for one unit — the whole algorithm, four cases."""
    if consumer_hash is None:
        return ADD
    if consumer_hash == desired_hash:
        return NOOP
    if pristine_hash is not None and consumer_hash == pristine_hash:
        return UPDATE
    return OVERRIDE


def plan(
    units: Sequence[Unit],
    consumer_hashes: Mapping[str, str | None],
    pristine: Mapping[str, str],
) -> list[Decision]:
    """Decide every unit against the consumer state and the stored pristine map."""
    decisions: list[Decision] = []
    for unit in units:
        consumer_hash = consumer_hashes.get(unit.key)
        pristine_hash = pristine.get(unit.key)
        desired_hash = unit.desired_hash()
        decisions.append(
            Decision(
                unit=unit,
                action=decide(
                    consumer_hash=consumer_hash,
                    pristine_hash=pristine_hash,
                    desired_hash=desired_hash,
                ),
                desired_hash=desired_hash,
                consumer_hash=consumer_hash,
                pristine_hash=pristine_hash,
            )
        )
    return decisions


def activates_hooks(decisions: Sequence[Decision]) -> bool:
    """Whether this install should activate the git hooks.

    The pure half of the decision: ``True`` whenever ``lefthook.yml`` is part of
    the reconciled set, i.e. the lint-check config is (now) in place — so its hooks
    belong live. The actual ``lefthook install`` is the bounded side effect
    :mod:`shipit.install.apply` performs; the plan only records that it WILL
    happen. Because activation is idempotent, it runs on every WRITING install
    that manages the caller (ADD or UPDATE), not only the first ADD. A pure
    no-op re-run never reaches apply, so it never re-touches already-current
    hooks.
    """
    return any(d.unit.key == LEFTHOOK_FILE for d in decisions)


# --------------------------------------------------------------------------
# Retired files (docs/prd/rvw01-sole-requester.md, ADR-0031)
# --------------------------------------------------------------------------
#
# Files shipit used to distribute (or release-sync-era debris) are removed
# portfolio-wide by the same mechanism that installs files — onboarding a repo
# IS the cleanup. The packaged manifest lists each retired path with the set of
# known pristine content hashes; the pure core below maps (actual hash, known
# hashes) to delete / warn-and-keep / no-op; the IO pass in
# :func:`shipit.install.apply.apply` unlinks the decided deletes.


@dataclass(frozen=True)
class RetiredFile:
    """One retired path with every known pristine version's ``sha256:`` hash."""

    path: str  # path relative to the consumer root
    pristine_hashes: tuple[str, ...]


@dataclass(frozen=True)
class RetiredDecision:
    retired: RetiredFile
    action: str  # DELETE | KEEP | NOOP
    actual_hash: str | None


def _retired_path(raw: str) -> str:
    """Validate one manifest path: plain relative, inside the consumer root.

    The manifest is packaged data, but every entry names a file a later unlink
    will destroy — so a bad entry (absolute path, drive letter, ``..``
    traversal) fails the load closed rather than reaching the IO pass.
    """
    posix = PurePosixPath(raw)
    win = PureWindowsPath(raw)
    if (
        not raw
        or posix.is_absolute()
        or ".." in posix.parts
        # Windows forms: `drive` rejects both absolute (`C:\x`) and
        # drive-relative (`C:x`) paths, `root` rejects rooted `\x`, and the
        # parts check catches backslash-separated `..` traversal.
        or win.drive
        or win.root
        or ".." in win.parts
    ):
        raise ValueError(f"retired-files manifest: unsafe path {raw!r}")
    return raw


def load_retired() -> list[RetiredFile]:
    """The packaged retired-files manifest, in manifest order."""
    data = tomllib.loads(data_bytes(RETIRED_MANIFEST).decode("utf-8"))
    return [
        RetiredFile(
            path=_retired_path(str(e["path"])),
            pristine_hashes=tuple(e["pristine"]),
        )
        for e in data.get("retired", [])
    ]


def decide_retired(*, actual_hash: str | None, pristine_hashes: tuple[str, ...]) -> str:
    """The retired-files outcome for one path — the whole algorithm, three cases.

    ``actual_hash is None`` means the file is absent (the same encoding
    :func:`decide` uses for ``consumer_hash``). A pristine match — ANY of the
    known historical versions — is safe to delete; content differing from every
    known version is a local edit we never destroy (KEEP, warned); absent is done.
    """
    if actual_hash is None:
        return NOOP
    if actual_hash in pristine_hashes:
        return DELETE
    return KEEP


def plan_retired(
    retired: Sequence[RetiredFile], actual_hashes: Mapping[str, str | None]
) -> list[RetiredDecision]:
    """Decide every retired path against the consumer's actual content hashes."""
    decisions: list[RetiredDecision] = []
    for r in retired:
        actual = actual_hashes.get(r.path)
        decisions.append(
            RetiredDecision(
                retired=r,
                action=decide_retired(
                    actual_hash=actual, pristine_hashes=r.pristine_hashes
                ),
                actual_hash=actual,
            )
        )
    return decisions


def retired_actual_hash(root: Path, retired: RetiredFile) -> str | None:
    """The hash of a retired path's current content, or ``None`` if absent."""
    dest = root / retired.path
    if dest.is_symlink():
        # ``is_file()`` follows symlinks, so a link whose TARGET matches a
        # pristine hash would otherwise decide DELETE. A symlink is never
        # shipit's pristine output; any non-``sha256:`` value can never match
        # a pristine hash, so the link is kept and warned as locally modified.
        return "symlink"
    if not dest.is_file():
        return None
    return config.content_hash(dest.read_bytes())


# --------------------------------------------------------------------------
# The lefthook merge-conflict tripwire (#544)
# --------------------------------------------------------------------------
#
# lefthook layers a consumer's committed lefthook-local config over the managed
# lefthook.yml (per-hook map merge, the local scalar winning) and REFUSES a
# merged hook where both `piped: true` and `parallel: true` hold — the run
# crashes before executing any command, so the gate neither runs nor can be
# satisfied and every `git commit` in the consumer is blocked (the phos-editor
# incident). The detection below is scoped to the conflict class INSTALL can
# cause: it only flags a hook where the DESIRED managed content itself sets one
# of the exclusive options (a lefthook-local.yml arguing with itself is the
# consumer's own file, outside the managed set's blast radius — lefthook
# reports it on the consumer's next hook run either way).

#: The hook-level execution-order options lefthook refuses to combine.
EXCLUSIVE_HOOK_OPTIONS = ("piped", "parallel")

#: The consumer-owned local-config names lefthook merges over the managed
#: `lefthook.yml` (the managed caller fixes the naming style, so only the
#: `lefthook-local` YAML spellings at the consumer root apply). First hit wins,
#: matching lefthook's own single-local-config resolution.
LEFTHOOK_LOCAL_FILES = ("lefthook-local.yml", "lefthook-local.yaml")


@dataclass(frozen=True)
class LefthookConflict:
    """One hook whose merged managed+local config lefthook would refuse."""

    hook: str  # the hook name, e.g. "pre-commit"
    local_path: str  # the consumer's local-config filename
    managed_options: tuple[str, ...]  # exclusive options the managed caller sets
    local_options: tuple[str, ...]  # exclusive options the local config sets


def format_lefthook_conflict(conflict: LefthookConflict) -> str:
    """The one actionable message for a conflict — used verbatim by the
    working-tree stderr warning and the committing modes' fail-closed error,
    so the two surfaces can never drift.

    Both branches below are reachable only once a FUTURE managed edit
    reintroduces a hook-level option (today's caller sets none, per the #544
    tripwire). The usual shape is the managed side setting one option and the
    consumer's local config the other, so the fix is to drop the local one. If
    the managed side sets BOTH, ``local_options`` is empty: the conflict is
    entirely managed-side, so the guidance points at regenerating the managed
    config, never at removing an option the consumer never set."""

    def named(options: tuple[str, ...]) -> str:
        return " and ".join(f"'{o}: true'" for o in options)

    head = (
        f"the managed {LEFTHOOK_FILE} sets {named(conflict.managed_options)} on "
        f"the '{conflict.hook}' hook"
    )
    if conflict.local_options:
        head += f" and this repo's {conflict.local_path} sets {named(conflict.local_options)}"
    tail = (
        f" — lefthook refuses a merged hook with both 'piped' and 'parallel' "
        f"true and crashes BEFORE running any check, blocking every git "
        f"operation that fires '{conflict.hook}'. "
    )
    if conflict.local_options:
        fix = (
            f"Remove the option from {conflict.local_path} (the managed "
            f"{LEFTHOOK_FILE} is regenerated by `shipit install` — never edit "
            f"it), then re-run."
        )
    else:
        fix = (
            f"This is a managed-config defect — re-run `shipit install` to "
            f"regenerate {LEFTHOOK_FILE} (never edit it by hand)."
        )
    return head + tail + fix


def detect_lefthook_conflicts(
    managed_text: str, local_text: str, local_path: str
) -> tuple[LefthookConflict, ...]:
    """The piped/parallel conflicts lefthook would refuse in the MERGED config.

    Pure over the two texts: parse both YAML, and per managed hook compute the
    merged exclusive options the way lefthook layers them (the local value wins
    when set — so a local ``piped: false`` DEFUSES a managed ``piped: true``).
    A hook conflicts when both exclusive options are true in the merge, the
    managed side contributes at least one of them, and the local side is NOT
    already a both-true self-conflict (which install neither causes nor can fix
    — see the section comment for the scoping rationale). An unparseable or
    non-mapping YAML yields no conflicts: that is a different failure class the
    consumer owns and lefthook itself reports; this tripwire never turns it into
    an install refusal.
    """
    try:
        managed = yaml.safe_load(managed_text)
        local = yaml.safe_load(local_text)
    except yaml.YAMLError:
        return ()
    if not isinstance(managed, dict) or not isinstance(local, dict):
        return ()
    conflicts: list[LefthookConflict] = []
    for hook, managed_hook in managed.items():
        local_hook = local.get(hook)
        if not isinstance(managed_hook, dict) or not isinstance(local_hook, dict):
            continue
        local_set = tuple(
            o for o in EXCLUSIVE_HOOK_OPTIONS if local_hook.get(o) is True
        )
        if len(local_set) == len(EXCLUSIVE_HOOK_OPTIONS):
            # The consumer's own lefthook-local.yml already sets BOTH exclusive
            # options true: the merged hook is refused whatever the managed side
            # sets, so install neither causes it nor can fix it (removing a
            # managed option leaves the local self-conflict intact). Outside the
            # managed blast radius — lefthook reports it on the consumer's next
            # hook run; the tripwire stays scoped to conflicts install creates.
            continue
        managed_set = tuple(
            o for o in EXCLUSIVE_HOOK_OPTIONS if managed_hook.get(o) is True
        )
        if not managed_set:
            continue
        merged = {
            o: local_hook.get(o, managed_hook.get(o)) for o in EXCLUSIVE_HOOK_OPTIONS
        }
        if all(merged[o] is True for o in EXCLUSIVE_HOOK_OPTIONS):
            conflicts.append(
                LefthookConflict(
                    hook=str(hook),
                    local_path=local_path,
                    managed_options=managed_set,
                    local_options=local_set,
                )
            )
    return tuple(conflicts)


def _plan_lefthook_conflicts(
    units: Sequence[Unit], state: ConsumerState
) -> tuple[LefthookConflict, ...]:
    """The Plan's conflict facts: the DESIRED managed caller vs the gathered
    local config. Desired, not on-disk: install writes shipit's content on
    every ADD/UPDATE/OVERRIDE, so the merge lefthook will actually see is
    desired + local. No lefthook unit (a test subset) or no local config means
    nothing to detect."""
    if state.lefthook_local is None or state.lefthook_local_path is None:
        return ()
    unit = next((u for u in units if u.key == LEFTHOOK_FILE), None)
    if unit is None:
        return ()
    return detect_lefthook_conflicts(
        unit.content.decode("utf-8"), state.lefthook_local, state.lefthook_local_path
    )


# --------------------------------------------------------------------------
# The read boundary — the consumer's current state, as one frozen value
# --------------------------------------------------------------------------

#: manifest basename -> toolchain signal (#547 Layer 1). A tracked manifest
#: ANYWHERE in the tree is the signal, matching `verbs/lint.py`'s per-manifest
#: leg discovery: a tracked ``Cargo.toml`` is exactly what makes the rust lint
#: leg run (which hard-fails 127 without cargo — the gap the rust dep block
#: closes, #526); ``go.mod`` and ``package.json`` are the go/node analogues.
TOOLCHAIN_MANIFESTS = (
    ("Cargo.toml", TOOLCHAIN_RUST),
    ("go.mod", TOOLCHAIN_GO),
    ("package.json", TOOLCHAIN_NODE),
)


def detect_toolchains(root: Path) -> frozenset[str]:
    """The consumer's toolchain signals, off its tracked manifests (#547 Layer 1).

    A small read boundary of its own, SEPARATE from :func:`gather` (which stays
    filesystem-only): one ``git ls-files`` over the toolchain manifest names
    through the git adapter (ADR-0028) — tracked-only, like the lint scope, so a
    vendored/ignored ``package.json`` deep in ``node_modules`` can never summon
    a toolchain. On a non-git root the read degrades to root-level manifest
    existence checks. The result feeds
    :func:`shipit.install.units.load_units`'s ``toolchains`` parameter.
    """
    pathspecs = [
        spec
        for name, _ in TOOLCHAIN_MANIFESTS
        for spec in (name, f"*/{name}")  # the root manifest and any nested one
    ]
    tracked = git.ls_files_matching(pathspecs, cwd=str(root))
    if tracked is not None:
        names = {PurePosixPath(p).name for p in tracked}
    else:
        names = {name for name, _ in TOOLCHAIN_MANIFESTS if (root / name).is_file()}
    detected = frozenset(tc for name, tc in TOOLCHAIN_MANIFESTS if name in names)
    if detected:
        logger.debug(
            "toolchain signals detected",
            extra={"root": str(root), "toolchains": ", ".join(sorted(detected))},
        )
    return detected


@dataclass(frozen=True)
class PixiKeyConflict:
    """One pixi block unit whose FIRST splice would duplicate consumer-owned keys.

    Detected only when the block's markers are absent (an ADD): once the block
    is spliced, its own keys legitimately live in the anchor table. The remedy
    is the consumer's call — keep their pin (the block stays undelivered) or
    delete it and re-run install to adopt the managed one.
    """

    unit_key: str  # the [managed] table key, e.g. "pixi.toml#shipit-node-deps"
    anchor: str  # the TOML table header the block anchors under
    keys: tuple[str, ...]  # the block keys the consumer already declares there


def format_pixi_key_conflict(conflict: PixiKeyConflict) -> str:
    """The one actionable message for a key conflict — used verbatim by the
    stderr warning and the durable log line, so the two surfaces never drift."""
    keys = " and ".join(f"'{k}'" for k in conflict.keys)
    return (
        f"this repo's pixi.toml already declares {keys} in {conflict.anchor}, "
        f"which the managed block '{conflict.unit_key}' also pins — splicing it "
        f"would duplicate the key(s) and make pixi.toml unparseable, so the "
        f"block was NOT delivered and this repo's own pin stays authoritative. "
        f"To adopt the managed pin instead, delete this repo's own entry and "
        f"re-run `shipit install`."
    )


def _pixi_key_conflicts(
    root: Path, units: Sequence[Unit], consumer_hashes: Mapping[str, str | None]
) -> tuple[PixiKeyConflict, ...]:
    """Gather's key-conflict read: first-splice duplicates in the pixi manifest.

    Best-effort and fail-open, like the lefthook-local read: no manifest or an
    unparseable one detects nothing (a consumer who already broke their own
    TOML hears it from pixi, not from a guard that only inspects). Only
    marker-absent (ADD-bound) pixi block units are checked — see
    :class:`PixiKeyConflict`.
    """
    path = root / PIXI_FILE
    if not path.is_file():
        return ()
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return ()
    conflicts: list[PixiKeyConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor is None:
            continue
        if consumer_hashes.get(unit.key) is not None:
            continue  # markers present: the table's keys include the block's own
        try:
            block_keys = tomllib.loads(unit.desired_inner())
        except tomllib.TOMLDecodeError:  # pragma: no cover — packaged data
            continue
        table: object = manifest
        for part in unit.anchor.strip().strip("[]").split("."):
            table = table.get(part) if isinstance(table, dict) else None
        if not isinstance(table, dict):
            continue  # the anchor table does not exist yet — nothing to clash
        clashes = tuple(sorted(k for k in block_keys if k in table))
        if clashes:
            conflicts.append(
                PixiKeyConflict(unit_key=unit.key, anchor=unit.anchor, keys=clashes)
            )
    return tuple(conflicts)


@dataclass(frozen=True)
class PixiTaskConflict:
    """One pixi ``[tasks]`` block unit whose FIRST splice would make a pixi
    task AMBIGUOUS — the key-conflict guard's pixi-run-level sibling
    (TOL01-WS01).

    pixi refuses a bare ``pixi run <task>`` when a task of that name is
    defined in several environments, so splicing a managed default-env task
    (``test = "./bin/shipit test"``) into a manifest whose own
    ``[feature.*.tasks]`` already defines the name would break the consumer's
    working command — shipit's own repo is the standing case (its full-gate
    ``test`` task lives in the ``test`` feature for the rust toolchain env and
    inline lexd provisioning). Detected only when the block's markers are
    absent (an ADD), like :class:`PixiKeyConflict`; the remedy is the
    consumer's call — keep their task (the block stays undelivered) or delete
    it and re-run install to adopt the managed caller. A same-named key in the
    ``[tasks]`` anchor table itself is the OTHER guard's case (a duplicate
    TOML key, :class:`PixiKeyConflict`).
    """

    unit_key: str  # the [managed] table key, e.g. "pixi.toml#shipit-test-task"
    task: str  # the ambiguous task name
    features: tuple[str, ...]  # the features whose tasks tables define it


def format_pixi_task_conflict(conflict: PixiTaskConflict) -> str:
    """The one actionable message for a task-ambiguity conflict — used verbatim
    by the stderr warning and the durable log line, so the two never drift."""
    tables = " and ".join(f"[feature.{f}.tasks]" for f in conflict.features)
    return (
        f"this repo's pixi.toml already defines a '{conflict.task}' task in "
        f"{tables}, which the managed block '{conflict.unit_key}' also defines "
        f"in [tasks] — splicing it would make `pixi run {conflict.task}` "
        f"ambiguous (pixi refuses a task defined in several environments), so "
        f"the block was NOT delivered and this repo's own task stays "
        f"authoritative. To adopt the managed caller instead, delete this "
        f"repo's own task and re-run `shipit install`."
    )


def _pixi_task_conflicts(
    root: Path, units: Sequence[Unit], consumer_hashes: Mapping[str, str | None]
) -> tuple[PixiTaskConflict, ...]:
    """Gather's task-ambiguity read: first-splice pixi-task name clashes.

    Best-effort and fail-open like :func:`_pixi_key_conflicts` (whose
    ADD-bound-only rule it shares): no manifest or an unparseable one detects
    nothing. Only ``[tasks]``-anchored pixi block units are checked — a task
    the block would define in the default env clashes with a same-named task
    any ``[feature.*.tasks]`` table defines (they land in different
    environments, which is exactly what makes ``pixi run`` refuse the name).
    """
    path = root / PIXI_FILE
    if not path.is_file():
        return ()
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return ()
    features = manifest.get("feature")
    if not isinstance(features, dict):
        return ()
    feature_tasks: dict[str, list[str]] = {}
    for feature, body in features.items():
        tasks = body.get("tasks") if isinstance(body, dict) else None
        if isinstance(tasks, dict):
            for task in tasks:
                feature_tasks.setdefault(str(task), []).append(str(feature))
    if not feature_tasks:
        return ()
    conflicts: list[PixiTaskConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor != "[tasks]":
            continue
        if consumer_hashes.get(unit.key) is not None:
            continue  # markers present: the task is already the managed one
        try:
            block_tasks = tomllib.loads(unit.desired_inner())
        except tomllib.TOMLDecodeError:  # pragma: no cover — packaged data
            continue
        for task in block_tasks:
            if task in feature_tasks:
                conflicts.append(
                    PixiTaskConflict(
                        unit_key=unit.key,
                        task=str(task),
                        features=tuple(sorted(feature_tasks[task])),
                    )
                )
    return tuple(conflicts)


def consumer_inner(root: Path, unit: Unit) -> str | None:
    """A block unit's current inner text in the consumer, or ``None``."""
    dest = root / unit.dest
    if not dest.is_file():
        return None
    text = dest.read_text(encoding="utf-8")
    if unit.fmt == FMT_JSON_HOOK:
        return extract_settings_hook(text, unit.event, unit.marker)
    return extract_block(text, unit.open_marker, unit.close_marker)


def consumer_hash(root: Path, unit: Unit) -> str | None:
    """The hash of a unit's current content in the consumer, or ``None`` if absent."""
    if unit.kind == "block":
        inner = consumer_inner(root, unit)
        return None if inner is None else config.content_hash(inner.encode("utf-8"))
    dest = root / unit.dest
    if not dest.is_file():
        return None
    return config.content_hash(dest.read_bytes())


@dataclass(frozen=True)
class ConsumerState:
    """What :func:`gather` read off the consumer — the reconcile's only input.

    ``manifest_error`` is the degraded-but-continuing case: an unreadable
    ``.shipit.toml`` empties the pristine map (consumer edits will surface as
    OVERRIDEs) and carries the reason so the renderer can warn.
    """

    root: str
    consumer_hashes: Mapping[str, str | None]  # unit key -> current hash (or absent)
    pristine: Mapping[str, str]  # unit key -> stored pristine hash
    retired_hashes: Mapping[str, str | None]  # retired path -> current hash
    seeds: tuple[str, ...]  # policy entries the seed pass would add
    # The consumer's current Shipit pin (`.shipit.toml [shipit].version`, RAW —
    # whatever is stored, sha or not) and the pin an applying install WOULD
    # stamp (ADR-0033: its own build sha, resolved through the SAME seam apply
    # stamps with so the two can never disagree). Their mismatch is a plan-level
    # work axis: a code-only shipit change bumps the build sha without touching a
    # managed file, and the reconcile must still roll that pin forward — see
    # :attr:`Plan.pin_stale`. Compared raw (not sha-validated): a non-sha or
    # absent stored pin simply differs from the target and gets re-stamped.
    # ``target_pin`` is None when no build identity resolves (apply fails closed
    # there anyway, so forcing a bump would only raise).
    current_pin: str | None = None
    target_pin: str | None = None
    # No pixi.toml at all (#432) — distinct from "present without the managed
    # blocks", which the per-unit hashes already encode as ADDs: pixi requires a
    # [workspace]/[project]/[package] table, so an applying install must seed a
    # minimal valid manifest before the block splices land.
    pixi_manifest_missing: bool = False
    manifest_error: str | None = None
    # The consumer's committed lefthook-local config (#544) — the filename found
    # (first of :data:`LEFTHOOK_LOCAL_FILES`) and its raw text, or None/None when
    # absent. Read here (the ONE read boundary) so the reconcile's merge-conflict
    # tripwire stays pure over this state.
    lefthook_local_path: str | None = None
    lefthook_local: str | None = None
    # First-splice duplicate-key clashes in the consumer's pixi.toml (#547
    # round 1) — read here (the ONE read boundary) so the reconcile's skip
    # decision stays pure over this state.
    pixi_key_conflicts: tuple[PixiKeyConflict, ...] = ()
    # First-splice pixi-task AMBIGUITY clashes (TOL01-WS01): a managed [tasks]
    # block task also defined by a consumer [feature.*.tasks] table — read
    # here for the same purity reason.
    pixi_task_conflicts: tuple[PixiTaskConflict, ...] = ()


def gather(
    root: Path, units: Sequence[Unit], retired: Sequence[RetiredFile]
) -> ConsumerState:
    """Read the consumer's current state — the install domain's ONE read boundary.

    Filesystem reads only, no git/gh: per-unit content hashes, the stored
    pristine map and the seed-if-absent policy plan from ``.shipit.toml``
    (consumer-owned policy — the App ``[secrets]`` mappings + the ``[reviewers]``
    set — is planned alongside the manifest but never under the pristine-hash
    reconciliation; architecture.lex §6, issue #25), each retired path's
    actual hash, the consumer's committed lefthook-local config (#544, the
    merge-conflict tripwire's input), and the pixi manifest's first-splice
    key clashes (:func:`_pixi_key_conflicts`) and task-ambiguity clashes
    (:func:`_pixi_task_conflicts`).
    """
    root = root.resolve()
    if not root.is_dir():
        raise InstallError(f"{root} is not a directory")

    cfg_path = root / config.CONFIG_NAME
    pristine: dict[str, str] = {}
    seeds: list[str] = []
    current_pin: str | None = None
    manifest_error: str | None = None
    try:
        if cfg_path.is_file():
            cfg = config.load(cfg_path)
            pristine = config.load_managed(cfg)
            current_pin = config.shipit_version(cfg)  # RAW — compared, not validated
        seeds = config.plan_policy_seed(cfg_path)
    except config.ConfigError as exc:
        # Degraded-but-continuing: the reconcile proceeds against an empty
        # pristine map, so consumer edits will surface as OVERRIDEs.
        manifest_error = str(exc)
        logger.warning(
            "ignoring unreadable manifest",
            exc_info=True,
            extra={"root": str(root), "manifest": str(cfg_path)},
        )

    lefthook_local_path, lefthook_local = _read_lefthook_local(root)
    consumer_hashes = {u.key: consumer_hash(root, u) for u in units}
    return ConsumerState(
        root=str(root),
        consumer_hashes=consumer_hashes,
        pristine=pristine,
        retired_hashes={r.path: retired_actual_hash(root, r) for r in retired},
        seeds=tuple(seeds),
        current_pin=current_pin,
        target_pin=_target_pin(),
        pixi_manifest_missing=not (root / PIXI_FILE).is_file(),
        manifest_error=manifest_error,
        lefthook_local_path=lefthook_local_path,
        lefthook_local=lefthook_local,
        pixi_key_conflicts=_pixi_key_conflicts(root, units, consumer_hashes),
        pixi_task_conflicts=_pixi_task_conflicts(root, units, consumer_hashes),
    )


def _read_lefthook_local(root: Path) -> tuple[str | None, str | None]:
    """The consumer's lefthook-local config: ``(filename, text)``, or None/None.

    First existing name of :data:`LEFTHOOK_LOCAL_FILES` wins — the same
    single-local-config resolution lefthook applies over the managed caller.

    Fails OPEN, like the unreadable-manifest path above: an ``OSError`` reading
    a consumer-owned file (a permission denial, a mid-read unlink) degrades to
    None/None with a logged warning rather than crashing ``shipit install`` —
    the merge-conflict tripwire is best-effort, and a working-tree refresh must
    never abort on a file it only inspects.
    """
    for name in LEFTHOOK_LOCAL_FILES:
        dest = root / name
        if not dest.is_file():
            continue
        try:
            return name, dest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning(
                "ignoring unreadable lefthook-local config",
                exc_info=True,
                extra={"root": str(root), "local": name},
            )
            return None, None
    return None, None


def _target_pin() -> str | None:
    """The pin an applying install WOULD stamp — ``None`` if none resolves.

    Resolved through the very seam :func:`shipit.install.apply.apply` stamps
    with (its ``_shipit_version``), so the plan's ``target_pin`` and the pin
    apply writes can never disagree — a code-only build's sha, the same value
    on both sides. Imported at call time to avoid the ``apply -> reconcile``
    module cycle (apply imports the :class:`Plan`). Apply's fail-closed refusal
    (no build identity) becomes ``None`` here: a plan cannot force a bump it
    could not stamp anyway.
    """
    from .apply import _shipit_version

    try:
        return _shipit_version()
    except InstallError:
        return None


# --------------------------------------------------------------------------
# The Plan — every decision, one frozen aggregate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What install WOULD do — the frozen aggregate :func:`reconcile` returns.

    Managed-unit decisions, retired-file decisions, and the policy seeds ride
    together, so "what does install do" is one inspectable value: the dry-run
    renders it, :func:`shipit.install.apply.apply` executes it.
    """

    root: str
    decisions: tuple[Decision, ...]
    retired: tuple[RetiredDecision, ...]
    seeds: tuple[str, ...]
    # The consumer has no pixi.toml, and this plan writes pixi block units into
    # one (#432): apply seeds the minimal valid [workspace] manifest first, so
    # pixi parses the file from the very first commit. One-time scaffold —
    # never hashed into [managed], consumer-owned after the seed.
    seed_pixi_manifest: bool = False
    manifest_error: str | None = None
    # ADR-0033: the Shipit pin travels IN the reconcile payload. The consumer's
    # current pin and the running build's sha ride the plan so a code-only
    # shipit change — new build sha, every managed file byte-identical — is
    # still work to do (:attr:`pin_stale`), rolling the pin forward via the same
    # `.shipit.toml` write apply already performs. Without this the no-op check
    # would strand consumers on a stale build forever (the install reconcile PR
    # is the ONLY bump vehicle; nothing else stamps the pin).
    current_pin: str | None = None
    target_pin: str | None = None
    # The lefthook merge-conflict tripwire's findings (#544): hooks where the
    # DESIRED managed lefthook.yml and the consumer's lefthook-local.yml merge
    # into a config lefthook refuses (both `piped` and `parallel` true — every
    # commit in the consumer would be blocked before any check runs). The
    # working-tree mode warns loudly; the committing modes fail closed in apply.
    lefthook_conflicts: tuple[LefthookConflict, ...] = ()
    # Pixi blocks this plan SKIPPED (#547 round 1): a first splice would have
    # duplicated a consumer-owned key in the anchor table, breaking pixi.toml —
    # so their decisions are excluded outright (never a broken write, in any
    # mode) and every surface warns off this record.
    pixi_key_conflicts: tuple[PixiKeyConflict, ...] = ()
    # Pixi blocks SKIPPED over a task-name AMBIGUITY (TOL01-WS01): the splice
    # would define a default-env task a consumer feature also defines, making
    # `pixi run <task>` refuse the name — excluded the same way, every surface
    # warns off this record.
    pixi_task_conflicts: tuple[PixiTaskConflict, ...] = ()

    @property
    def writes(self) -> tuple[Decision, ...]:
        """ADD/UPDATE/OVERRIDE all write shipit's content; only NOOP writes nothing."""
        return tuple(d for d in self.decisions if d.action in (ADD, UPDATE, OVERRIDE))

    @property
    def overrides(self) -> tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.action == OVERRIDE)

    @property
    def retire_deletes(self) -> tuple[RetiredDecision, ...]:
        return tuple(d for d in self.retired if d.action == DELETE)

    @property
    def retire_keeps(self) -> tuple[RetiredDecision, ...]:
        return tuple(d for d in self.retired if d.action == KEEP)

    @property
    def pin_stale(self) -> bool:
        """The consumer's pin differs from the running build's sha — a pin bump.

        The pin-only work axis (ADR-0033): a code-only shipit change leaves
        every managed file byte-identical yet advances the build sha, and the
        reconcile must still stamp the new pin so the fix reaches the repo.
        Only when ``target_pin`` resolved (else apply cannot stamp anyway and
        would fail closed) and it differs from what the consumer carries — a
        pinless consumer (``current_pin is None``) with a resolved build sha is
        stale too, so a first stamp is never skipped.
        """
        return self.target_pin is not None and self.current_pin != self.target_pin

    @property
    def nothing_to_do(self) -> bool:
        """No writes, no seeds, no retired delete, no pin bump — a clean no-op.

        A seed-only change (managed set current, policy missing) still counts
        as a write, so a re-install picks up policy a consumer never had — but
        stays a no-op once the policy is in place. A pending retired delete
        likewise keeps the run a write, so cleanup lands even when the managed
        set is current. A stale pin (:attr:`pin_stale`) is a work axis of its
        own: a code-only shipit change touches no managed file yet must roll the
        pin forward. A KEPT retired file does not: there is nothing shipit will
        change on its own.
        """
        return (
            not self.writes
            and not self.seeds
            and not self.retire_deletes
            and not self.pin_stale
        )

    @property
    def activates_hooks(self) -> bool:
        """Whether an applying install will (re)activate the git hooks."""
        return activates_hooks(self.decisions)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """Every path a writing apply touches — the commit set, manifest included.

        Deleted retired paths join it: ``git add`` on a removed path stages the
        deletion, so every commit mode carries the cleanup.
        """
        return tuple(
            sorted(
                {d.unit.dest for d in self.writes}
                | {config.CONFIG_NAME}
                | {d.retired.path for d in self.retire_deletes}
            )
        )


def reconcile(
    units: Sequence[Unit],
    retired: Sequence[RetiredFile],
    state: ConsumerState,
) -> Plan:
    """Decide the whole install — pure over the gathered :class:`ConsumerState`.

    Aggregates the four-case managed decisions, the three-case retired-file
    decisions, and the policy seeds into one frozen :class:`Plan`. Logs the
    decided counts (the plan is mechanics, DEBUG) and each kept retired file
    (a locally modified copy shipit refuses to destroy, WARNING) — the durable
    twin (ADR-0029); the terminal report is the renderer's.
    """
    # A conflicted block never reaches the write set: a key conflict's ADD
    # would splice a duplicate TOML key into the consumer's pixi.toml
    # (PixiKeyConflict); a task conflict's would make a pixi task ambiguous
    # (PixiTaskConflict).
    conflicted = {c.unit_key for c in state.pixi_key_conflicts} | {
        c.unit_key for c in state.pixi_task_conflicts
    }
    decisions = tuple(
        d
        for d in plan(units, state.consumer_hashes, state.pristine)
        if d.unit.key not in conflicted
    )
    result = Plan(
        root=state.root,
        decisions=decisions,
        retired=tuple(plan_retired(retired, state.retired_hashes)),
        seeds=state.seeds,
        # Seed only when a write will actually create pixi.toml: no manifest on
        # the consumer AND a pixi block unit in this plan's write set (with the
        # file absent every pixi unit decides ADD, so this is one condition,
        # stated fully).
        seed_pixi_manifest=state.pixi_manifest_missing
        and any(
            d.unit.dest == PIXI_FILE for d in decisions if d.action in (ADD, UPDATE)
        ),
        manifest_error=state.manifest_error,
        current_pin=state.current_pin,
        target_pin=state.target_pin,
        lefthook_conflicts=_plan_lefthook_conflicts(units, state),
        pixi_key_conflicts=state.pixi_key_conflicts,
        pixi_task_conflicts=state.pixi_task_conflicts,
    )
    logger.debug(
        "reconcile plan decided",
        extra={
            "root": state.root,
            "adds": sum(1 for d in result.decisions if d.action == ADD),
            "updates": sum(1 for d in result.decisions if d.action == UPDATE),
            "overrides": len(result.overrides),
            "noops": sum(1 for d in result.decisions if d.action == NOOP),
            "seeds": len(result.seeds),
            "pixi_seed": result.seed_pixi_manifest,
            "retire_deletes": len(result.retire_deletes),
            "retire_keeps": len(result.retire_keeps),
            "pin_stale": result.pin_stale,
        },
    )
    for d in result.retire_keeps:
        logger.warning(
            "retired file kept — locally modified",
            extra={"root": state.root, "path": d.retired.path},
        )
    for c in result.lefthook_conflicts:
        logger.warning(
            "lefthook merge conflict: %s",
            format_lefthook_conflict(c),
            extra={"root": state.root, "hook": c.hook, "local": c.local_path},
        )
    for kc in result.pixi_key_conflicts:
        logger.warning(
            "pixi key conflict: %s",
            format_pixi_key_conflict(kc),
            extra={"root": state.root, "unit": kc.unit_key, "anchor": kc.anchor},
        )
    for tc in result.pixi_task_conflicts:
        logger.warning(
            "pixi task conflict: %s",
            format_pixi_task_conflict(tc),
            extra={"root": state.root, "unit": tc.unit_key, "task": tc.task},
        )
    if result.nothing_to_do:
        logger.debug(
            "managed set is current — nothing to do", extra={"root": state.root}
        )
    return result
