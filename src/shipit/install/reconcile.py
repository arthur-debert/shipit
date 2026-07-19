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

A unit the consumer has DECLINED (#600) never enters the four cases at all:
``.shipit.toml [managed.decline].keep`` lists managed-unit keys the repo keeps
as its own — the durable form of hand-declining the same OVERRIDE in every
reconcile PR (the dogfood repo's ``bin/shipit`` source-deferring bootstrap is
the standing case). A declined unit is excluded from the decisions outright
(never written, never re-proposed; its stale ``[managed]`` pristine entry ages
out on the next applying install's re-stamp), recorded on
:attr:`Plan.declined` so every surface — the plan report, the PR body — keeps
the decision visible. A declined key naming NO unit in this catalog rides
:attr:`Plan.decline_unmatched` and warns (a typo must not silently decline
nothing).

Install also decides a RETIRED-FILES pass (docs/legacy-prd/rvw01-sole-requester.md,
ADR-0031): a packaged manifest (``retired-files.toml``) lists paths shipit used
to distribute that must no longer exist, each with every known pristine
content hash. Three outcomes, same safety philosophy — never destroy a local
edit:

  - absent                              -> NOOP   (already gone)
  - present, hash in known pristines    -> DELETE (safe: it is shipit's own content)
  - present, hash matches NO known one  -> KEEP   (locally modified: warn, keep)

Install also decides a RETIRED-HOOKS pass (#619) — the retired-files idea
extended to consumer-local hook ENTRIES inside a hooks file shipit does not
own outright: the same packaged manifest lists ``(file, event, marker)``
triples naming legacy entries (the ADR-0003 ``bin/install-release-core``
resolver hook, the pre-managed ``setup-dev-env.sh`` duplicate), and every
matching entry in that event array is removed. Two outcomes — a matching
entry exists -> DELETE, none -> NOOP; there is deliberately no KEEP case: a
hook entry's whole content is the command it runs, so "invokes the retired
script" IS its identity, and shipit's own managed entries are protected by
their ``shipit hook`` command marker inside the match itself
(:func:`shipit.install.splice.is_retired_hook`).

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
(:class:`PixiTaskConflict`). A third sibling (ARF01-WS04) guards ANCHOR-LESS
blocks against TABLE REDECLARATION: an EOF-appended top-level table whose name
is NOT in shipit's reserved namespace — the private-tier
``[s3-options.<bucket>]``, which the documented manual runbook may have the
consumer declare by hand — would, on a first splice over a pre-existing table,
declare that table twice and make ``pixi.toml`` unparseable, so the block is
skipped and the consumer's own table stays authoritative
(:class:`PixiTableConflict`).

Install also decides a CHANGELOG RE-RENDER (TOL01-WS08 #578): where the
consumer has adopted the fragment convention (``CHANGELOG/``), a renderer
change in shipit (a new generated-file header, section fixes) leaves the
committed ``CHANGELOG.md`` stale against ``shipit changelog check`` fleet-wide,
and the reconcile PR is the sanctioned channel that refreshes it (ADR-0033) —
gather compares the CURRENT renderer's output against the committed file and
the Plan carries the re-render decision (:attr:`Plan.rerender_changelog`);
:mod:`shipit.install.apply` writes the refreshed render. A repo without the
convention has nothing to re-render and is never refused.

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
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from .. import config, git
from ..changelog import CHANGELOG_FILE, sync_diff
from .errors import InstallError
from .splice import (
    count_retired_hooks,
    extract_block,
    extract_env_member,
    extract_settings_hook,
)
from .units import (
    FMT_ENV_MEMBER,
    FMT_JSON_HOOK,
    FMT_MARKERS,
    LEFTHOOK_FILE,
    PIXI_FILE,
    TOOLCHAIN_GO,
    TOOLCHAIN_NODE,
    TOOLCHAIN_PYTHON,
    TOOLCHAIN_RUST,
    Unit,
    data_bytes,
)

logger = logging.getLogger("shipit.install")

ADD = "add"
NOOP = "noop"
UPDATE = "update"
OVERRIDE = "override"

# Retired-files outcomes (docs/legacy-prd/rvw01-sole-requester.md). NOOP is shared:
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
# Retired files (docs/legacy-prd/rvw01-sole-requester.md, ADR-0031)
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
# Retired hook entries (#619)
# --------------------------------------------------------------------------
#
# The retired-files idea extended to consumer-local hook ENTRIES: legacy
# entries in a hooks-event array (the ADR-0003 `bin/install-release-core`
# resolver hook, the pre-managed `setup-dev-env.sh` duplicate) are removed
# fleet-wide by the same reconcile that installs the managed set. An entry is
# identified by the command it runs (`marker`), and shipit's own managed
# entries are protected inside the match itself (splice.is_retired_hook), so
# the pass can never touch the entries install manages.


@dataclass(frozen=True)
class RetiredHook:
    """One retired consumer-local hook entry: every entry in ``file``'s
    ``event`` hooks-array whose command carries ``marker`` must go."""

    file: str  # the hooks file, relative to the consumer root
    event: str  # the hooks-event array the entry lives in
    marker: str  # the command substring identifying the retired entry

    @property
    def key(self) -> str:
        """The entry's unique manifest identity — the gather counts' mapping
        key and every surface's display name."""
        return f"{self.file}#{self.event}[{self.marker}]"


@dataclass(frozen=True)
class RetiredHookDecision:
    retired: RetiredHook
    action: str  # DELETE | NOOP
    count: int  # matching consumer-local entries at gather time


def load_retired_hooks() -> list[RetiredHook]:
    """The packaged retired-hooks manifest entries, in manifest order (#619).

    Same manifest file as the retired FILES (:data:`RETIRED_MANIFEST` —
    retiring the next entry is data, not code), same path validation: every
    ``file`` names a consumer file the IO pass will rewrite.
    """
    data = tomllib.loads(data_bytes(RETIRED_MANIFEST).decode("utf-8"))
    return [
        RetiredHook(
            file=_retired_path(str(e["file"])),
            event=str(e["event"]),
            marker=str(e["marker"]),
        )
        for e in data.get("retired_hooks", [])
    ]


def decide_retired_hook(*, count: int) -> str:
    """The retired-hooks outcome for one entry — the whole algorithm, two cases.

    Deliberately NO KEEP case (unlike :func:`decide_retired`): a hook entry's
    whole content is the command it runs, so "invokes the retired script" IS
    its identity — there is no local-edit body to preserve the way a retired
    FILE can carry one — and shipit's own managed entries are already excluded
    by the match (:func:`shipit.install.splice.is_retired_hook`).
    """
    return DELETE if count else NOOP


def plan_retired_hooks(
    retired_hooks: Sequence[RetiredHook], counts: Mapping[str, int]
) -> list[RetiredHookDecision]:
    """Decide every retired hook entry against the consumer's gathered counts."""
    return [
        RetiredHookDecision(
            retired=rh,
            action=decide_retired_hook(count=counts.get(rh.key, 0)),
            count=counts.get(rh.key, 0),
        )
        for rh in retired_hooks
    ]


def retired_hook_count(root: Path, hook: RetiredHook) -> int:
    """How many consumer-local entries ``hook`` currently matches — 0 when the
    file is absent, unreadable, or malformed.

    Fails OPEN like :func:`_read_lefthook_local`: an ``OSError`` or non-UTF-8
    read degrades to "nothing to remove" with a logged warning, and a malformed
    file counts 0 in lockstep with the write path
    (:func:`shipit.install.splice.remove_retired_hooks` preserves it verbatim)
    — the decision never claims work the write cannot safely do.
    """
    dest = root / hook.file
    if not dest.is_file():
        return 0
    try:
        text = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "ignoring unreadable hooks file in the retired-hooks pass",
            exc_info=True,
            extra={"root": str(root), "file": hook.file},
        )
        return 0
    return count_retired_hooks(text, hook.event, hook.marker)


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
#: ANYWHERE in the tree is the signal, matching `shipit/lint.py`'s per-manifest
#: leg discovery: a tracked ``Cargo.toml`` is exactly what makes the rust lint
#: leg run (which hard-fails 127 without cargo — the gap the rust dep block
#: closes, #526); ``go.mod`` and ``package.json`` are the go/node analogues.
#: ``pyproject.toml`` joined in #801 (TOL02-WS17 open hole 2): the python
#: signal delivers the release-side twine block — the same tracked-manifest
#: read, feeding :data:`shipit.install.units.TOOLCHAIN_UNITS`' python rows.
TOOLCHAIN_MANIFESTS = (
    ("Cargo.toml", TOOLCHAIN_RUST),
    ("go.mod", TOOLCHAIN_GO),
    ("package.json", TOOLCHAIN_NODE),
    ("pyproject.toml", TOOLCHAIN_PYTHON),
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


def _managed_block_keys(
    text: str, units: Sequence[Unit], consumer_hashes: Mapping[str, str | None]
) -> dict[str, frozenset[str]]:
    """Anchor → the keys currently declared inside a PRESENT shipit-managed
    block's marker span in the consumer's ``pixi.toml``.

    These keys are shipit-OWNED, not hand-written consumer pins: this same
    reconcile rewrites (or retires) the block that carries them. A marker-absent
    block whose first-splice key coincides with one of these is a managed-block
    MIGRATION — the key moving from one managed block to another in one pass
    (#1071: ``rattler-build`` moving out of the rust release-deps block into the
    new conda-packager block) — which :func:`_pixi_key_conflicts` must NOT
    mistake for a duplicate over a genuine consumer pin, or it would skip the
    RECEIVING block and, with the same plan removing the key from the DONOR
    block, strand the repo with no packager until a second reconcile.

    Best-effort and fail-open like the caller: only ``FMT_MARKERS`` pixi block
    units whose markers are PRESENT are read, and an unparseable managed span
    contributes nothing.
    """
    by_anchor: dict[str, set[str]] = {}
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor is None:
            continue
        if unit.fmt != FMT_MARKERS:
            continue
        if consumer_hashes.get(unit.key) is None:
            continue  # marker-absent: no present span whose keys to project
        inner = extract_block(text, unit.open_marker, unit.close_marker)
        if inner is None:
            continue
        try:
            block = tomllib.loads(inner)
        except tomllib.TOMLDecodeError:
            continue
        by_anchor.setdefault(unit.anchor, set()).update(block.keys())
    return {anchor: frozenset(keys) for anchor, keys in by_anchor.items()}


def _pixi_key_conflicts(
    root: Path, units: Sequence[Unit], consumer_hashes: Mapping[str, str | None]
) -> tuple[PixiKeyConflict, ...]:
    """Gather's key-conflict read: first-splice duplicates in the pixi manifest.

    Best-effort and fail-open, like the lefthook-local read: no manifest or an
    unparseable one detects nothing (a consumer who already broke their own
    TOML hears it from pixi, not from a guard that only inspects). Only
    marker-absent (ADD-bound) pixi block units are checked — see
    :class:`PixiKeyConflict`.

    A clash is a duplicate over a CONSUMER-owned key, so keys currently living
    inside ANOTHER present managed block's span (:func:`_managed_block_keys`)
    are excluded: the manifest is projected through the pending managed-block
    updates first, so a key migrating between two managed blocks in one pass
    (#1071) does not read as a first-splice conflict and strand the receiving
    block for a whole extra reconcile.
    """
    path = root / PIXI_FILE
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
        manifest = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return ()
    managed_keys = _managed_block_keys(text, units, consumer_hashes)
    conflicts: list[PixiKeyConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor is None:
            continue
        if unit.fmt != FMT_MARKERS:
            # An FMT_ENV_MEMBER unit shares its anchor table (`[environments]`) with
            # the consumer BY DESIGN — it merges its managed feature into the
            # consumer's own env entry (append, never a duplicate key), so its
            # would-be "clash" is the merge, not a conflict to skip (ADR-0066).
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
        owned = managed_keys.get(unit.anchor, frozenset())
        clashes = tuple(sorted(k for k in block_keys if k in table and k not in owned))
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
    ``test`` task lives in the ``test`` feature for the rust toolchain env).
    Detected only when the block's markers are
    absent (an ADD), like :class:`PixiKeyConflict`; the remedy is the
    consumer's call — keep their task (the block stays undelivered) or delete
    it and re-run install to adopt the managed caller. A same-named key in the
    ``[tasks]`` anchor table itself is the OTHER guard's case (a duplicate
    TOML key, :class:`PixiKeyConflict`).
    """

    unit_key: str  # the [managed] table key, e.g. "pixi.toml#shipit-test-task"
    task: str  # the ambiguous task name
    features: tuple[str, ...]  # the features whose tasks tables define it


def _feature_tasks_header(feature: str) -> str:
    """The ``[feature.<name>.tasks]`` header for ``feature``, QUOTING the name
    when it carries a dot (or is otherwise not a bare TOML key) so the rendered
    header is a valid, unambiguous path — an unquoted ``ruamel.yaml`` would read
    as the nested ``ruamel`` → ``yaml`` tables, not the one feature named
    ``ruamel.yaml``. Bare keys (the common case) render unquoted.

    The name is read from the CONSUMER's own ``pixi.toml`` (:func:`_enabled_features`),
    so it is unbounded: a quoted TOML key is a basic string, so any ``\\`` or ``"``
    it carries is escaped before interpolation — otherwise the header would be
    invalid TOML and the conflict message would mis-render the very path it names.
    """
    if re.fullmatch(r"[A-Za-z0-9_-]+", feature):
        return f"[feature.{feature}.tasks]"
    escaped = feature.replace("\\", "\\\\").replace('"', '\\"')
    return f'[feature."{escaped}".tasks]'


def format_pixi_task_conflict(conflict: PixiTaskConflict) -> str:
    """The one actionable message for a task-ambiguity conflict — used verbatim
    by the stderr warning and the durable log line, so the two never drift."""
    tables = " and ".join(_feature_tasks_header(f) for f in conflict.features)
    return (
        f"this repo's pixi.toml already defines a '{conflict.task}' task in "
        f"{tables}, which the managed block '{conflict.unit_key}' also defines "
        f"in [tasks] — splicing it would make `pixi run {conflict.task}` "
        f"ambiguous (pixi refuses a task defined in several environments), so "
        f"the block was NOT delivered and this repo's own task stays "
        f"authoritative. To adopt the managed caller instead, delete this "
        f"repo's own task and re-run `shipit install`."
    )


def _enabled_features(manifest: Mapping[str, object]) -> frozenset[str]:
    """The feature names referenced by any ``[environments]`` entry.

    pixi's ``[environments]`` maps an env name to its features — either a bare
    list (``test = ["test"]``) or a table (``test = { features = ["test"] }``).
    A feature listed by no environment materializes in none, so its tasks
    cannot collide with a default-env managed task (:func:`_pixi_task_conflicts`
    uses this to avoid over-detecting). The always-present ``default`` feature
    is not enumerated here — it is not a consumer ``[feature.*]`` name.
    """
    environments = manifest.get("environments")
    if not isinstance(environments, dict):
        return frozenset()
    enabled: set[str] = set()
    for spec in environments.values():
        feats = spec.get("features") if isinstance(spec, dict) else spec
        if isinstance(feats, list):
            enabled.update(str(f) for f in feats)
    return frozenset(enabled)


def _pixi_task_conflicts(
    root: Path, units: Sequence[Unit], consumer_hashes: Mapping[str, str | None]
) -> tuple[PixiTaskConflict, ...]:
    """Gather's task-ambiguity read: first-splice pixi-task name clashes.

    Best-effort and fail-open like :func:`_pixi_key_conflicts` (whose
    ADD-bound-only rule it shares): no manifest or an unparseable one detects
    nothing. Only ``[tasks]``-anchored pixi block units are checked — a task
    the block would define in the default env clashes with a same-named task
    a consumer ``[feature.*.tasks]`` table defines, but ONLY when that feature
    is ENABLED by some ``[environments]`` entry: a feature no environment
    includes never materializes its tasks in any env, so it cannot make
    ``pixi run <task>`` ambiguous — counting it would over-detect and skip the
    managed block needlessly. Ambiguity is exactly the task landing in the
    default env (the managed block) AND another env (the enabled feature).
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
    enabled = _enabled_features(manifest)
    feature_tasks: dict[str, list[str]] = {}
    for feature, body in features.items():
        if str(feature) not in enabled:
            continue  # unreferenced feature: its tasks reach no environment
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


@dataclass(frozen=True)
class PixiTableConflict:
    """One anchor-less pixi block unit whose FIRST splice would REDECLARE a
    consumer-owned TOML table — the key-conflict guard's EOF-appended-table
    sibling (ARF01-WS04).

    An anchor-less block appends its own top-level table(s) at EOF. The
    projection's reserved ``shipit-artifacts*`` feature tables can never clash
    with a consumer table by design, but the private-tier
    ``[s3-options.<bucket>]`` table (:data:`shipit.install.artifactdeps.S3_OPTIONS_KEY`)
    reuses a NON-reserved name a consumer may already declare by hand (the
    documented manual private-tier runbook). Splicing a second identical table
    header makes ``pixi.toml`` unparseable (TOML forbids a table defined twice),
    blocking installs and every hooked commit — so the block is SKIPPED, the
    consumer's own table stays authoritative, and every surface warns. Detected
    only when the block's markers are absent (an ADD), like
    :class:`PixiKeyConflict`: once spliced, the block's own tables legitimately
    live in the manifest and a re-reconcile reads them as its own.
    """

    unit_key: (
        str  # the [managed] table key, e.g. "pixi.toml#shipit-artifact-deps-s3-options"
    )
    # The clashing table headers the consumer already declares, VERBATIM (the
    # TOML-quoted form, e.g. `s3-options."my.bucket"`) so the warning names the
    # real path — a bare `s3-options.my.bucket` is a different nested table.
    tables: tuple[str, ...]


def format_pixi_table_conflict(conflict: PixiTableConflict) -> str:
    """The one actionable message for a table-redeclaration conflict — used
    verbatim by the stderr warning and the durable log line, so the two surfaces
    never drift."""
    tables = " and ".join(f"[{t}]" for t in conflict.tables)
    return (
        f"this repo's pixi.toml already declares the {tables} table(s), which "
        f"the managed block '{conflict.unit_key}' also declares — splicing it "
        f"would redeclare the table(s) and make pixi.toml unparseable, so the "
        f"block was NOT delivered and this repo's own table stays "
        f"authoritative. To adopt the managed block instead, delete this repo's "
        f"own table(s) and re-run `shipit install`."
    )


def _split_toml_key(key: str) -> tuple[str, ...]:
    """Split a TOML dotted key-path into its segments, honoring quoted parts.

    A dot inside a quoted segment is a literal name char, not a path separator,
    so ``s3-options."my.bucket"`` splits into ``("s3-options", "my.bucket")`` —
    the same quoting :func:`shipit.install.artifactdeps._toml_key` emits when a
    projected name carries a dot. Surrounding whitespace on a bare segment is
    trimmed; a quoted segment's inner text is taken verbatim.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in key:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            segments.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    segments.append("".join(buf).strip())
    return tuple(segments)


def _toml_table_headers(inner: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Every plain ``[table]`` header in a TOML text, as ``(raw, segments)``.

    ``raw`` is the header's inner text VERBATIM (``s3-options."my.bucket"`` —
    the TOML-quoted form the block emitted), so a conflict report names the real
    table path a user would delete; ``segments`` is that path split with quoting
    honored (:func:`_split_toml_key`), so the walk against the parsed consumer
    manifest compares whole names. Reporting ``raw`` rather than
    ``".".join(segments)`` keeps the quoting: a bare ``s3-options.my.bucket`` is
    a DIFFERENT (nested) table in TOML and would misdirect the fix.

    Only plain-table headers matter to the redeclaration guard: array-of-tables
    (``[[...]]``) declarations stack rather than clash, and the projection emits
    none anyway.
    """
    headers: list[tuple[str, tuple[str, ...]]] = []
    for line in inner.splitlines():
        stripped = line.strip()
        if (
            not stripped.startswith("[")
            or stripped.startswith("[[")
            or not stripped.endswith("]")
        ):
            continue
        raw = stripped[1:-1].strip()
        headers.append((raw, _split_toml_key(raw)))
    return tuple(headers)


def _table_declared(manifest: Mapping[str, object], path: tuple[str, ...]) -> bool:
    """Whether the parsed consumer manifest already declares the table at ``path``.

    Walks the manifest along the segments; the table is present when the FULL
    path resolves to a sub-table (dict) — exactly the redeclaration a duplicate
    ``[path]`` header would cause. An empty path (a malformed header) is never a
    table. Only the LEAF path each block header names is checked, so a shared
    super-table (a consumer ``[feature.foo]`` next to the reserved
    ``[feature.shipit-artifacts]``) is not mistaken for a clash.
    """
    if not path:
        return False
    table: object = manifest
    for part in path:
        if not isinstance(table, dict) or part not in table:
            return False
        table = table[part]
    return isinstance(table, dict)


def _pixi_table_conflicts(
    root: Path, units: Sequence[Unit], consumer_hashes: Mapping[str, str | None]
) -> tuple[PixiTableConflict, ...]:
    """Gather's table-redeclaration read: first-splice duplicate top-level tables.

    Best-effort and fail-open like :func:`_pixi_key_conflicts` (whose
    ADD-bound-only rule it shares): no manifest or an unparseable one detects
    nothing. Only ANCHOR-LESS pixi block units are checked — an anchored block's
    duplicate-KEY risk is the key-conflict guard's, and its own header (an
    ``[environments]`` say) is the consumer's own table it merges into. An
    anchor-less block whose EOF-appended header names a table the consumer
    already declares would make pixi.toml unparseable on the ADD splice, so the
    reconcile skips it (:class:`PixiTableConflict`).
    """
    path = root / PIXI_FILE
    if not path.is_file():
        return ()
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return ()
    conflicts: list[PixiTableConflict] = []
    for unit in units:
        if unit.kind != "block" or unit.dest != PIXI_FILE or unit.anchor is not None:
            continue
        if consumer_hashes.get(unit.key) is not None:
            continue  # markers present: the block's tables are already its own
        clashes = tuple(
            raw
            for raw, segments in _toml_table_headers(unit.desired_inner())
            if _table_declared(manifest, segments)
        )
        if clashes:
            conflicts.append(PixiTableConflict(unit_key=unit.key, tables=clashes))
    return tuple(conflicts)


def _changelog_stale(root: Path) -> bool:
    """Whether the consumer's committed ``CHANGELOG.md`` no longer matches the
    CURRENT renderer's output over ``CHANGELOG/`` — gather's changelog read
    (TOL01-WS08 #578).

    A renderer change (WS06's generated-file header, the duplicate-section
    fixes) strands every consumer's committed projection: ``shipit changelog
    check`` fails fleet-wide, and hand-patching per repo is exactly what the
    reconcile channel exists to replace (ADR-0033). ``True`` only where the
    fragment convention EXISTS and the render differs — a repo without
    ``CHANGELOG/`` (or with unrenderable version filenames) is not stale, it
    just has nothing to re-render (:func:`shipit.verbs.changelog.render_current`
    returns ``None`` there). Imported at call time: the verb module wears the
    ``_errors`` CLI shell, whose import chain leads back into this package (the
    same cycle the selfcert lint import breaks lazily).

    Fails OPEN on an unreadable projection, like :func:`_read_lefthook_local`
    and the manifest reads: :func:`gather` runs this advisory read
    unconditionally, so an ``OSError`` (a permission denial, a mid-read unlink)
    or a non-UTF-8 file anywhere in the changelog inspection — the committed
    ``CHANGELOG.md`` OR a ``CHANGELOG/`` fragment ``render_current`` reads —
    degrades to "not stale" with a logged warning rather than crashing ``shipit
    install`` on files it only inspects. The catch lives at THIS advisory
    boundary, not in ``render_current``, so the ``changelog`` verb (for which
    the render is the primary operation, not an aside) still fails loud.
    """
    from ..verbs.changelog import render_current

    try:
        rendered = render_current(root)
        if rendered is None:
            return False
        committed_path = root / CHANGELOG_FILE
        committed = (
            committed_path.read_text(encoding="utf-8")
            if committed_path.is_file()
            else None
        )
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "ignoring unreadable CHANGELOG projection — treating as not stale",
            exc_info=True,
            extra={"root": str(root)},
        )
        return False
    return sync_diff(rendered, committed) is not None


def consumer_inner(root: Path, unit: Unit) -> str | None:
    """A block unit's current inner text in the consumer, or ``None``."""
    dest = root / unit.dest
    if not dest.is_file():
        return None
    text = dest.read_text(encoding="utf-8")
    if unit.fmt == FMT_JSON_HOOK:
        return extract_settings_hook(text, unit.event, unit.marker)
    if unit.fmt == FMT_ENV_MEMBER:
        return extract_env_member(text, unit.env_name or "", unit.required_features)
    return extract_block(text, unit.open_marker, unit.close_marker)


@dataclass(frozen=True)
class SymlinkedDest:
    """A whole-file unit whose destination crosses a consumer-owned symlink.

    ``component`` is the repo-relative path of the SHALLOWEST symlinked path
    element (from the consumer root down to the leaf) — the containment breach
    point the operator must remove before install can write the unit.
    """

    unit_key: str
    dest: str  # the unit's declared dest (repo-relative)
    component: str  # the symlinked path element (repo-relative)


def symlinked_dest_component(root: Path, dest: str) -> str | None:
    """The shallowest symlinked component of ``dest`` under ``root``, or None.

    Walks every path element from the consumer root down to the leaf. A
    whole-file unit's write (:func:`shipit.install.apply.write_unit` ->
    ``dest.write_bytes``) and its hash read (:func:`consumer_hash` ->
    ``dest.read_bytes``) both FOLLOW a symlink in ANY path component, so a
    symlinked element — a linked leaf, or a linked parent dir — would let an
    install write THROUGH the link, over whatever it targets OUTSIDE the repo.
    Returns the offending element's repo-relative path so the caller fails
    closed on it; shipit never writes through a consumer's symlink (ADR-0077).
    """
    current = root
    for part in Path(dest).parts:
        current = current / part
        if current.is_symlink():
            return str(current.relative_to(root))
    return None


def symlinked_dests(root: Path, units: Sequence[Unit]) -> tuple[SymlinkedDest, ...]:
    """Every whole-file unit whose dest crosses a consumer symlink (fail-closed).

    Block units are excluded: they splice into a consumer-owned host file
    (``AGENTS.md``, ``pixi.toml``) whose symlinking is the consumer's own
    business, and the splice reads+rewrites that one file rather than writing a
    fresh path shipit owns. Only whole-file units carry the path-containment
    guarantee this guard protects.
    """
    found: list[SymlinkedDest] = []
    for u in units:
        if u.kind != "file":
            continue
        component = symlinked_dest_component(root, u.dest)
        if component is not None:
            found.append(
                SymlinkedDest(unit_key=u.key, dest=u.dest, component=component)
            )
    return tuple(found)


def format_symlinked_dest(sd: SymlinkedDest) -> str:
    """The one actionable message for a symlinked-dest conflict — used verbatim
    by the working-tree/dry-run stderr warning and the fail-closed error, so the
    two surfaces can never drift."""
    leaf = "" if sd.component == sd.dest else f" (writing {sd.dest} would follow it)"
    return (
        f"{sd.component} is a symlink{leaf} — shipit refuses to write the managed "
        f"unit {sd.unit_key} through it (it would overwrite the link's target, "
        f"outside this repo). Remove the symlink and re-run `shipit install` to "
        f"receive a real copy"
    )


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
    # Retired hook entries (#619): RetiredHook.key -> how many consumer-local
    # entries currently match — read here (the ONE read boundary) so the
    # reconcile's two-case decision stays pure over this state.
    retired_hook_counts: Mapping[str, int] = field(default_factory=dict)
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
    # First-splice table-REDECLARATION clashes (ARF01-WS04): an anchor-less
    # managed block whose EOF-appended top-level table (the private-tier
    # [s3-options.<bucket>]) the consumer already declares by hand — read here
    # for the same purity reason.
    pixi_table_conflicts: tuple[PixiTableConflict, ...] = ()
    # The committed CHANGELOG.md no longer matches the CURRENT renderer's
    # output over CHANGELOG/ (#578) — read here (the ONE read boundary) so the
    # reconcile's re-render decision stays pure over this state. Always False
    # where the fragment convention is absent or unrenderable.
    changelog_stale: bool = False
    # The consumer's declined managed-unit keys (#600) — `.shipit.toml
    # [managed.decline].keep`, read here (the ONE read boundary) so the
    # reconcile's skip decision stays pure over this state. Empties with the
    # pristine map on an unreadable manifest (the degraded-but-continuing
    # path): no readable policy means no decline.
    declines: tuple[str, ...] = ()
    # Whole-file units whose dest crosses a consumer symlink (#1088 review):
    # read here (the ONE read boundary) so the reconcile's fail-closed decision
    # stays pure over this state. A symlinked dest component would make an
    # install write THROUGH the link, outside the repo — the containment breach
    # every mode refuses.
    symlinked_dests: tuple[SymlinkedDest, ...] = ()


def gather(
    root: Path,
    units: Sequence[Unit],
    retired: Sequence[RetiredFile],
    retired_hooks: Sequence[RetiredHook] = (),
) -> ConsumerState:
    """Read the consumer's current state — the install domain's ONE read boundary.

    Filesystem reads only, no git/gh: per-unit content hashes, the stored
    pristine map and the seed-if-absent policy plan from ``.shipit.toml``
    (consumer-owned policy — the App ``[secrets]`` mappings, the ``[reviewers]``
    set, the ``[lint]`` ignore globs, and the manifest-derived ``[toolchains]``
    map (#578) — is planned alongside the manifest but never under the
    pristine-hash reconciliation; architecture.lex §6, issue #25), each retired path's
    actual hash, each retired hook entry's current match count
    (:func:`retired_hook_count`, #619), the consumer's committed
    lefthook-local config (#544, the
    merge-conflict tripwire's input), the pixi manifest's first-splice
    key clashes (:func:`_pixi_key_conflicts`), task-ambiguity clashes
    (:func:`_pixi_task_conflicts`) and table-redeclaration clashes
    (:func:`_pixi_table_conflicts`), whether the committed ``CHANGELOG.md``
    is stale against the current renderer (:func:`_changelog_stale`, #578),
    and the declined managed-unit keys (``[managed.decline].keep``, #600 —
    consumer-owned policy, read alongside the pristine map).
    """
    root = root.resolve()
    if not root.is_dir():
        raise InstallError(f"{root} is not a directory")

    cfg_path = root / config.CONFIG_NAME
    pristine: dict[str, str] = {}
    seeds: list[str] = []
    current_pin: str | None = None
    declines: tuple[str, ...] = ()
    manifest_error: str | None = None
    try:
        if cfg_path.is_file():
            raw = cfg_path.read_text(encoding="utf-8")
            cfg = config.load(cfg_path)
            pristine = config.load_managed(cfg)
            current_pin = config.shipit_version(cfg)  # RAW — compared, not validated
            # `raw` lets load_declines reject a dotted `decline.keep` that the
            # re-stamp would silently strip (#600); the header form is required.
            declines = config.load_declines(cfg, raw)  # [managed.decline].keep
        # The [toolchains] seed entries derive from the consumer's root
        # manifests (#578) — the same signal table the Tool verbs' missing-map
        # error suggests (`config.SIGNAL_MANIFESTS`), so seed and suggestion
        # can never disagree. Seed-when-absent like every policy table.
        seeds = config.plan_policy_seed(
            cfg_path, toolchains=config.derive_toolchains(root)
        )
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
        retired_hook_counts={h.key: retired_hook_count(root, h) for h in retired_hooks},
        current_pin=current_pin,
        target_pin=_target_pin(),
        pixi_manifest_missing=not (root / PIXI_FILE).is_file(),
        manifest_error=manifest_error,
        lefthook_local_path=lefthook_local_path,
        lefthook_local=lefthook_local,
        pixi_key_conflicts=_pixi_key_conflicts(root, units, consumer_hashes),
        pixi_task_conflicts=_pixi_task_conflicts(root, units, consumer_hashes),
        pixi_table_conflicts=_pixi_table_conflicts(root, units, consumer_hashes),
        changelog_stale=_changelog_stale(root),
        declines=declines,
        symlinked_dests=symlinked_dests(root, units),
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
    # Retired hook entries (#619): the two-case decisions over the packaged
    # (file, event, marker) triples — a DELETE removes every matching
    # consumer-local entry from its event array (shipit's own managed entries
    # are protected inside the match; splice.is_retired_hook).
    retired_hooks: tuple[RetiredHookDecision, ...] = ()
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
    # Pixi blocks SKIPPED over a table REDECLARATION (ARF01-WS04): an anchor-less
    # block's EOF-appended top-level table (the private-tier
    # [s3-options.<bucket>]) is one the consumer already declares by hand, so the
    # splice would make pixi.toml unparseable — excluded the same way, every
    # surface warns off this record.
    pixi_table_conflicts: tuple[PixiTableConflict, ...] = ()
    # This plan regenerates CHANGELOG.md from CHANGELOG/ with the CURRENT
    # renderer (#578): the committed projection went stale against a renderer
    # change, and the reconcile PR is the sanctioned channel that refreshes it
    # (ADR-0033). A work axis of its own, like the pin bump — it can be the
    # ONLY change and must still make the plan actionable. The fragments stay
    # authoritative; the rendered file is a projection, never a managed unit.
    rerender_changelog: bool = False
    # Units this plan DECLINED (#600): catalog units the consumer's
    # `.shipit.toml [managed.decline].keep` keeps as its own. Excluded from the
    # decisions outright — never written, never re-proposed as an OVERRIDE, and
    # dropped from the manifest re-stamp (apply stamps `[managed]` from the
    # decisions, so a declined unit's stale pristine entry ages out on the next
    # applying install). Not work: a decline contributes nothing to
    # :attr:`nothing_to_do`. Every surface renders the standing decision off
    # this record.
    declined: tuple[str, ...] = ()
    # Declined keys naming NO unit in this catalog (#600): warned, never
    # silently ignored — usually a typo, occasionally a toolchain-conditional
    # unit whose signal manifest this repo does not track.
    decline_unmatched: tuple[str, ...] = ()
    # Whole-file units whose dest crosses a consumer symlink (#1088 review):
    # their decisions are EXCLUDED (a symlinked read would hash the link's
    # target, not shipit's dest) and EVERY mode fails closed on this record
    # before any write (:func:`shipit.install.apply.reject_symlinked_dests`) —
    # the containment guarantee is mode-independent, unlike the lefthook publish
    # refusal. Every surface warns off this record.
    symlinked_dests: tuple[SymlinkedDest, ...] = ()

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
    def retire_hook_deletes(self) -> tuple[RetiredHookDecision, ...]:
        return tuple(d for d in self.retired_hooks if d.action == DELETE)

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
        """No writes, no seeds, no retired delete, no pin bump, no changelog
        re-render — a clean no-op.

        A seed-only change (managed set current, policy missing) still counts
        as a write, so a re-install picks up policy a consumer never had — but
        stays a no-op once the policy is in place. A pending retired delete —
        file or hook entry (#619) — likewise keeps the run a write, so cleanup
        lands even when the managed
        set is current. A stale pin (:attr:`pin_stale`) is a work axis of its
        own: a code-only shipit change touches no managed file yet must roll the
        pin forward. So is a stale changelog projection
        (:attr:`rerender_changelog`, #578): a renderer change touches no managed
        file yet must refresh the consumer's committed render. A KEPT retired
        file does not: there is nothing shipit will change on its own.
        """
        return (
            not self.writes
            and not self.seeds
            and not self.retire_deletes
            and not self.retire_hook_deletes
            and not self.pin_stale
            and not self.rerender_changelog
        )

    @property
    def activates_hooks(self) -> bool:
        """Whether an applying install will (re)activate the git hooks."""
        return activates_hooks(self.decisions)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """Every path a writing apply touches — the commit set, manifest included.

        Deleted retired paths join it: ``git add`` on a removed path stages the
        deletion, so every commit mode carries the cleanup. So do the hooks
        files a retired-entry removal rewrites (#619), and the
        re-rendered ``CHANGELOG.md`` (#578), so the reconcile PR carries the
        refreshed render.
        """
        return tuple(
            sorted(
                {d.unit.dest for d in self.writes}
                | {config.CONFIG_NAME}
                | {d.retired.path for d in self.retire_deletes}
                | {d.retired.file for d in self.retire_hook_deletes}
                | ({CHANGELOG_FILE} if self.rerender_changelog else set())
            )
        )


def reconcile(
    units: Sequence[Unit],
    retired: Sequence[RetiredFile],
    state: ConsumerState,
    retired_hooks: Sequence[RetiredHook] = (),
) -> Plan:
    """Decide the whole install — pure over the gathered :class:`ConsumerState`.

    Aggregates the four-case managed decisions, the three-case retired-file
    decisions, the two-case retired-hook decisions (#619), and the policy
    seeds into one frozen :class:`Plan`. Logs the
    decided counts (the plan is mechanics, DEBUG) and each kept retired file
    (a locally modified copy shipit refuses to destroy, WARNING) — the durable
    twin (ADR-0029); the terminal report is the renderer's.
    """
    # A conflicted block never reaches the write set: a key conflict's ADD
    # would splice a duplicate TOML key into the consumer's pixi.toml
    # (PixiKeyConflict); a task conflict's would make a pixi task ambiguous
    # (PixiTaskConflict); a table conflict's would redeclare a consumer-owned
    # top-level table (PixiTableConflict, ARF01-WS04). Neither does a DECLINED
    # unit (#600): the consumer's `[managed.decline].keep` keeps it as the repo's
    # own, so it is excluded before the four-case decide ever runs.
    conflicted = (
        {c.unit_key for c in state.pixi_key_conflicts}
        | {c.unit_key for c in state.pixi_task_conflicts}
        | {c.unit_key for c in state.pixi_table_conflicts}
        # A symlinked-dest unit's decision would be read THROUGH the link (its
        # hash is the target's, not the dest's), so exclude it outright — apply
        # fails closed on the record below before any write reaches the link.
        | {sd.unit_key for sd in state.symlinked_dests}
    )
    decline_set = set(state.declines)
    unit_keys = {u.key for u in units}
    # Both surfaces keep the consumer's DECLARATION order (config.load_declines'
    # promise), de-duped — not the catalog's `units` order, which would make the
    # plan report and PR body reorder unpredictably as the shipped catalog grows.
    declined = tuple(dict.fromkeys(k for k in state.declines if k in unit_keys))
    decline_unmatched = tuple(
        dict.fromkeys(k for k in state.declines if k not in unit_keys)
    )
    decisions = tuple(
        d
        for d in plan(units, state.consumer_hashes, state.pristine)
        if d.unit.key not in conflicted and d.unit.key not in decline_set
    )
    result = Plan(
        root=state.root,
        decisions=decisions,
        retired=tuple(plan_retired(retired, state.retired_hashes)),
        seeds=state.seeds,
        retired_hooks=tuple(
            plan_retired_hooks(retired_hooks, state.retired_hook_counts)
        ),
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
        pixi_table_conflicts=state.pixi_table_conflicts,
        rerender_changelog=state.changelog_stale,
        declined=declined,
        decline_unmatched=decline_unmatched,
        symlinked_dests=state.symlinked_dests,
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
            "retire_hook_deletes": len(result.retire_hook_deletes),
            "pin_stale": result.pin_stale,
            "rerender_changelog": result.rerender_changelog,
            "declined": len(result.declined),
        },
    )
    for key in result.declined:
        logger.info(
            "managed unit declined — kept as the consumer's own "
            "([managed.decline].keep)",
            extra={"root": state.root, "unit": key},
        )
    for key in result.decline_unmatched:
        logger.warning(
            "declined key names no managed unit in this catalog",
            extra={"root": state.root, "unit": key},
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
    for bc in result.pixi_table_conflicts:
        logger.warning(
            "pixi table conflict: %s",
            format_pixi_table_conflict(bc),
            extra={"root": state.root, "unit": bc.unit_key, "tables": bc.tables},
        )
    for sd in result.symlinked_dests:
        logger.warning(
            "symlinked dest: %s",
            format_symlinked_dest(sd),
            extra={"root": state.root, "unit": sd.unit_key, "component": sd.component},
        )
    if result.nothing_to_do:
        logger.debug(
            "managed set is current — nothing to do", extra={"root": state.root}
        )
    return result
