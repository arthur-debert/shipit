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

The seam (ADR-0030): :func:`gather` is the ONE read boundary (consumer hashes,
the stored pristine map, the policy-seed plan — a frozen
:class:`ConsumerState`); :func:`reconcile` is pure over those values and
aggregates every managed and retired decision into the frozen :class:`Plan`,
inspectable before any file is written. All writes live in
:mod:`shipit.install.apply`.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .. import config
from .errors import InstallError
from .splice import extract_block, extract_settings_hook
from .units import FMT_JSON_HOOK, LEFTHOOK_FILE, PIXI_FILE, Unit, data_bytes

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
# The read boundary — the consumer's current state, as one frozen value
# --------------------------------------------------------------------------


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


def gather(
    root: Path, units: Sequence[Unit], retired: Sequence[RetiredFile]
) -> ConsumerState:
    """Read the consumer's current state — the install domain's ONE read boundary.

    Filesystem reads only, no git/gh: per-unit content hashes, the stored
    pristine map and the seed-if-absent policy plan from ``.shipit.toml``
    (consumer-owned policy — the App ``[secrets]`` mappings + the ``[reviewers]``
    set — is planned alongside the manifest but never under the pristine-hash
    reconciliation; architecture.lex §6, issue #25), and each retired path's
    actual hash.
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

    return ConsumerState(
        root=str(root),
        consumer_hashes={u.key: consumer_hash(root, u) for u in units},
        pristine=pristine,
        retired_hashes={r.path: retired_actual_hash(root, r) for r in retired},
        seeds=tuple(seeds),
        current_pin=current_pin,
        target_pin=_target_pin(),
        pixi_manifest_missing=not (root / PIXI_FILE).is_file(),
        manifest_error=manifest_error,
    )


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
    decisions = tuple(plan(units, state.consumer_hashes, state.pristine))
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
    if result.nothing_to_do:
        logger.debug(
            "managed set is current — nothing to do", extra={"root": state.root}
        )
    return result
