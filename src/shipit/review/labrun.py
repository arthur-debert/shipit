"""labrun — resolve one **Cell** onto the sanctioned offline replay driver.

The Review Lab's runner (ADR-0049, RVW03-WS07): ``lab run <cell>`` loads the
declarative cell file (:mod:`shipit.review.cell`), joins it to the Ground-truth
fixture (:mod:`shipit.review.groundtruth`), and executes every
(fixture PR × replicate × sweep) point FOREGROUND on the subscription-billed
CLI backends — each point one call into the sanctioned offline replay driver
(:func:`shipit.review.replay.run_replay` /
:func:`~shipit.review.replay.run_fanout_replay`, RVW03-WS01: resolved onto,
never forked). Results land as normal **Review-round records** tagged with the
cell's full idempotency key (``round.cell``,
:func:`shipit.review.cell.run_key`), in the same store live rounds use.

Idempotency is the load-bearing property (ADR-0049: no result is ever paid for
twice): before each point the pin's round-record store is read and a banked
record carrying the FULL key — (cell, fixture PR, fixture version, variant,
replicate, sweep) — is REUSED, never re-run, unless ``force`` re-executes
explicitly. Extending a K=1 curve to K=2 therefore pays only for sweep 2.

Informed sweeps (``[sweeps] mode = "informed"``) compose the prior sweeps'
POSTED findings into the instructions at THIS layer
(:func:`shipit.review.cell.compose_informed_instructions` → a temp
instructions file handed to the unchanged driver) — deliberately never a
replay-driver change (ADR-0049), so blind and informed arms drive the
identical pipeline and differ only in instructions text.

Checkouts: fixture pins live in OTHER repos, and replay is offline by design —
the operator supplies local clones (``--checkout``), each resolved to its
origin identity and matched to the pins; a pin with no matching checkout fails
LOUD before any model run bills (never a silent skip that would shrink the
curve's denominator).
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .. import identity
from ..agent import backend as agent_backend
from ..harness.eval.store import REVIEW_ROUNDS_KIND, read_records
from ..harness.eval.variant import variant_of
from ..identity import repo_from_slug
from . import replay as replay_mod
from .cell import (
    MAX_PLANNED_POINTS,
    Cell,
    CellError,
    compose_informed_instructions,
    key_tuple,
    record_matches_key,
    run_key,
)
from .groundtruth import Fixture, PinnedRange
from .instructions import load_instructions

logger = logging.getLogger("shipit.review")

__all__ = [
    "PlannedPoint",
    "RunSummary",
    "plan_points",
    "resolve_pins",
    "run_cell",
    "safe_instructions_path",
]


def safe_instructions_path(path: str | None) -> str | None:
    """Resolve a cell's relative instructions ``path`` and refuse one that
    escapes the working-directory root via symlink. BOUNDARY (touches the fs).

    :func:`shipit.review.cell._validate_instructions_path` rejects absolute /
    ``~`` / ``..`` at parse, but an IN-repo symlink (``lab/instructions/x.txt``
    → ``/etc/passwd``) still resolves outside the tree, and
    :func:`shipit.review.instructions.load_instructions` ends in a plain
    ``open`` — so the read is re-checked HERE against the RESOLVED real path: it
    must stay within the working directory (``cwd`` — where the relative path is
    read from; ``lab run`` runs from the repo root), symlinks followed, or the
    cell read is a loud refusal, keeping the in-repo-files-only promise intact
    for symlinks too. Returns the resolved absolute path so the caller reads the
    real target directly. ``None`` (the bundled default) passes straight through.
    A broken or LOOPING symlink (``resolve`` raises ``OSError`` / ``RuntimeError``)
    is also a loud :class:`CellError`, never a raw traceback.
    """
    if path is None:
        return None
    root = Path.cwd().resolve()
    try:
        resolved = (root / path).resolve()
    except (OSError, RuntimeError) as exc:  # broken / looping symlink
        raise CellError(
            f"cell instructions {path!r} cannot be resolved ({exc}) — check for "
            "a broken or looping symlink"
        ) from exc
    if resolved != root and root not in resolved.parents:
        raise CellError(
            f"cell instructions {path!r} resolve to {resolved} — outside the "
            "working directory; a symlink escaping the tree is refused (cells "
            "read in-repo files only)"
        )
    return str(resolved)


@dataclass(frozen=True)
class PlannedPoint:
    """One (fixture PR × replicate × sweep) point of a cell's sweep plan."""

    pin: PinnedRange
    replicate: int
    sweep: int
    key: Mapping[str, Any]


@dataclass(frozen=True)
class RunSummary:
    """What one ``lab run`` did: executed vs reused points (by key)."""

    cell_id: str
    executed: tuple[Mapping[str, Any], ...]
    reused: tuple[Mapping[str, Any], ...]


def resolve_pins(
    cell: Cell, fixture: Fixture, *, subset: Sequence[str] = ()
) -> tuple[PinnedRange, ...]:
    """The fixture pins this run replays. PURE; loud on any mismatch.

    The cell's declared subset (empty = every fixture pin) narrowed by the
    session ``subset`` (a ``--pr`` selection; must stay inside the cell's own
    set — running a pin the cell never declared would bank records the cell's
    curve then reads as its own). The fixture VERSION must equal the cell's
    pin: numbers scored against different label sets never compare, so running
    against a drifted fixture would bank incomparable records.
    """
    if fixture.version != cell.fixture_version:
        raise CellError(
            f"cell {cell.id!r} pins fixture v{cell.fixture_version} but the "
            f"fixture file is v{fixture.version} — update the cell (a new "
            "baseline run) or check out the pinned fixture; numbers across "
            "versions never compare"
        )
    by_id = {pin.id: pin for pin in fixture.prs}
    declared = cell.prs if cell.prs else tuple(pin.id for pin in fixture.prs)
    unknown = [pin_id for pin_id in declared if pin_id not in by_id]
    if unknown:
        raise CellError(
            f"cell {cell.id!r} names fixture pin(s) the fixture does not have: "
            f"{', '.join(map(repr, unknown))}"
        )
    if subset:
        subset_ids = set(subset)
        outside = [pin_id for pin_id in subset if pin_id not in declared]
        if outside:
            raise CellError(
                f"--pr pin(s) outside cell {cell.id!r}'s declared subset: "
                f"{', '.join(map(repr, outside))} "
                f"(declared: {', '.join(map(repr, declared))})"
            )
        declared = tuple(pin_id for pin_id in declared if pin_id in subset_ids)
    return tuple(by_id[pin_id] for pin_id in declared)


def plan_points(
    cell: Cell, pins: Sequence[PinnedRange], *, variant_hash: str
) -> tuple[PlannedPoint, ...]:
    """Enumerate the cell's full sweep plan in deterministic run order. PURE.

    Pins in declared order; per pin, replicates 1..R; per replicate, sweeps
    1..K — sweeps INNERMOST so an informed sweep's priors (same pin, same
    replicate, lower sweep) are always banked before it runs.

    Refuses a plan whose total ``pins × replicates × sweeps`` exceeds
    :data:`~shipit.review.cell.MAX_PLANNED_POINTS` BEFORE building the tuple:
    the per-axis :data:`~shipit.review.cell.MAX_SWEEP_COUNT` still lets the
    product reach a million points, and one point is one model launch, so a
    runaway plan must die loudly here rather than exhaust memory or bill a
    million runs. Both the runner and the report route through this guard.
    """
    total = len(pins) * cell.replicates * cell.sweeps
    if total > MAX_PLANNED_POINTS:
        raise CellError(
            f"cell {cell.id!r}: {len(pins)} pin(s) × {cell.replicates} "
            f"replicate(s) × {cell.sweeps} sweep(s) = {total} points exceeds "
            f"the max {MAX_PLANNED_POINTS} — one point is one model launch, so "
            "a plan this large is a mistake, not an experiment (narrow the "
            "pins, replicates, or sweeps)"
        )
    return tuple(
        PlannedPoint(
            pin=pin,
            replicate=replicate,
            sweep=sweep,
            key=run_key(
                cell,
                pr_id=pin.id,
                variant_hash=variant_hash,
                replicate=replicate,
                sweep=sweep,
            ),
        )
        for pin in pins
        for replicate in range(1, cell.replicates + 1)
        for sweep in range(1, cell.sweeps + 1)
    )


def _checkout_map(checkouts: Sequence[str]) -> dict[str, str]:
    """Origin ``owner/name`` slug → its supplied checkout path, for every
    supplied checkout. BOUNDARY.

    A path that is not a clone with a resolvable origin identity is a loud
    :class:`CellError` naming it — a mis-supplied checkout must never silently
    replay the wrong repo's ranges.
    """
    mapping: dict[str, str] = {}
    for path in checkouts:
        try:
            repo = identity.resolve_repo(path)
        except Exception as exc:  # ExecError / ValueError — one clean refusal
            raise CellError(
                f"--checkout {path!r} has no resolvable origin owner/name "
                f"identity ({exc}) — pass a clone of the fixture pin's repo"
            ) from exc
        mapping[repo.slug] = path
    return mapping


def _posted_findings(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The record's POSTED findings — ``disposition == post`` AND canonical
    (``duplicate_of is None``), the same read the scorer and every reporter
    use (RVW02-WS04), so an informed sweep is primed with exactly what the
    prior sweeps CONCLUDED, not their routed-out noise."""
    findings = record.get("round.findings")
    if not isinstance(findings, Sequence):
        return []
    return [
        f
        for f in findings
        if isinstance(f, Mapping)
        and f.get("disposition") == "post"
        and f.get("duplicate_of") is None
    ]


def _prior_findings(
    records: Sequence[Mapping[str, Any]], point: PlannedPoint
) -> list[Mapping[str, Any]]:
    """Every posted finding banked by this point's PRIOR sweeps (same cell /
    fixture version / pin / variant / replicate, sweep < this one). PURE — the
    informed sweep's composition input.

    LAST-RECORD-WINS per prior sweep key, the SAME rule the curve scorer applies
    (:func:`shipit.review.curve._dedupe_by_key`): the store is append-only and
    ``--force`` re-runs append a NEW record under the same key, so only the
    newest record for each prior sweep contributes — a superseded run's findings
    never leak (or double) into the next sweep's prompt."""
    priors: list[Mapping[str, Any]] = []
    for sweep in range(1, point.sweep):
        prior_key = {**point.key, "sweep": sweep}
        # Newest match wins: scan from the end and stop at the first hit rather
        # than materializing every match just to keep the last.
        newest = next(
            (r for r in reversed(records) if record_matches_key(r, prior_key)), None
        )
        if newest is not None:
            priors.extend(_posted_findings(newest))
    return priors


def run_cell(
    cell: Cell,
    fixture: Fixture,
    *,
    checkouts: Sequence[str] = (),
    pr_subset: Sequence[str] = (),
    force: bool = False,
    base_dir: Path | None = None,
    launcher=None,
    out: TextIO | None = None,
) -> RunSummary:
    """Execute ``cell``'s full sweep plan over the replay driver. BOUNDARY.

    Foreground and sequential by design (ADR-0049: small subscription-billed
    sessions, one cell each — the parallelism lives inside each fan-out round,
    not across rounds). Each point is idempotent by its FULL key: a banked
    record is reused (printed as such), never re-run, unless ``force``.
    Preflight is all-or-nothing BEFORE any model run bills: the fixture
    version, pin set, backend token, instructions file, every needed checkout,
    AND every pinned commit range are resolved first — a missing clone or an
    unfetched pinned SHA is a loud refusal naming what to fix, never a silent
    skip and never a half-run curve (pin 1 launched, pin 2 dead on its SHA).

    ``checkouts`` are local clone paths (the current directory is always a
    candidate); ``pr_subset`` narrows the session to named pins; ``base_dir``
    overrides the store family root and ``launcher`` injects the launch seam
    (tests, exactly as on the replay driver). Returns the :class:`RunSummary`
    of executed vs reused points. Failure posture: the first failing point
    PROPAGATES (its own banked points stay banked — a re-run reuses them and
    continues where it stopped).
    """
    stream = out if out is not None else sys.stdout

    def say(line: str) -> None:
        print(line, file=stream)

    pins = resolve_pins(cell, fixture, subset=pr_subset)
    if not pins:
        raise CellError(f"cell {cell.id!r} resolves to zero fixture pins")

    try:
        backend = agent_backend.by_funnel_agent(cell.invocation.backend)
    except KeyError:
        known = ", ".join(b.funnel_agent or "" for b in agent_backend.funnel_backends())
        raise CellError(
            f"cell {cell.id!r}: unknown invocation backend "
            f"{cell.invocation.backend!r} (known: {known})"
        ) from None

    # The cell's BASE instructions: read once, up front — the variant half of
    # the idempotency key hashes this text, and an unreadable file must die
    # before any model run bills.
    try:
        base_text = load_instructions(safe_instructions_path(cell.instructions_path))
    except OSError as exc:
        raise CellError(
            f"cell {cell.id!r}: cannot read instructions "
            f"{cell.instructions_path!r}: {exc}"
        ) from exc
    variant_hash = variant_of(base_text).content_hash

    # Checkout preflight: every pin must resolve to a supplied clone (cwd is
    # a best-effort candidate too) BEFORE anything runs — all-or-nothing,
    # never a silent skip that would shrink the curve's denominator. Slugs
    # compare lowercased (Repo identity is canonical-lowercase; fixture pins
    # may vary in case).
    slug_to_checkout = _checkout_map(checkouts)
    try:
        cwd_repo = identity.resolve_repo(".")
    except Exception:  # not a clone / no origin — cwd just isn't a candidate
        cwd_repo = None
    else:
        slug_to_checkout.setdefault(cwd_repo.slug, ".")
    missing = sorted({pin.repo.lower() for pin in pins} - set(slug_to_checkout))
    if missing:
        raise CellError(
            f"no checkout supplied for fixture repo(s): "
            f"{', '.join(map(repr, missing))} — clone them locally (with the "
            "pinned commits fetched) and pass each clone via --checkout"
        )

    # Range preflight: resolve EVERY pin's commit range up front, so an
    # unfetched or unknown pinned SHA refuses BEFORE any point launches —
    # all-or-nothing (never launch pin 1's point, then die on pin 2's missing
    # SHA and leave a half-run curve banked). Slugs stay lowercased (Repo
    # identity is canonical-lowercase; fixture pins may vary in case).
    views_by_pin: dict[str, Any] = {}
    for pin in pins:
        workdir = slug_to_checkout[pin.repo.lower()]
        try:
            view = replay_mod.resolve_range(
                f"{pin.base_sha}..{pin.head_sha}", workdir=workdir
            )
        except Exception as exc:  # git resolution failure — one loud refusal
            raise CellError(
                f"cell {cell.id!r}: pin {pin.id!r} range "
                f"{pin.base_sha[:12]}..{pin.head_sha[:12]} does not resolve in "
                f"checkout {workdir!r} ({exc}) — fetch the pinned commits "
                "before running (offline replay never fetches)"
            ) from exc
        if view.repo.slug != pin.repo.lower():
            raise CellError(
                f"checkout {workdir!r} resolves to {view.repo.slug!r}, not the "
                f"pin's repo {pin.repo!r} (pin {pin.id!r})"
            )
        views_by_pin[pin.id] = view

    points = plan_points(cell, pins, variant_hash=variant_hash)
    say(
        f"cell {cell.id!r} (axis: {cell.axis!r}; baseline: {cell.baseline!r}) — "
        f"{len(points)} point(s): {len(pins)} pin(s) × "
        f"{cell.replicates} replicate(s) × {cell.sweeps} sweep(s), "
        f"{cell.sweep_mode} sweeps"
    )

    # Per-repo record cache, refreshed after each write: ONE source of truth
    # for both the idempotency check and the informed sweep's priors (banked
    # and just-run points read identically).
    records_by_slug: dict[str, list[dict[str, Any]]] = {}

    def _records(slug: str) -> list[dict[str, Any]]:
        if slug not in records_by_slug:
            records_by_slug[slug] = read_records(
                repo_from_slug(slug), base_dir, kind=REVIEW_ROUNDS_KIND
            )
        return records_by_slug[slug]

    # The banked idempotency keys of a slug's store as a SET, so the per-point
    # reuse check is O(1) — the store holds every review round of the repo, so
    # a per-point `any(record_matches_key ...)` scan would be O(points ×
    # records). Cached alongside `records_by_slug` and dropped with it on write.
    banked_keys_by_slug: dict[str, set[tuple]] = {}

    def _banked_keys(slug: str) -> set[tuple]:
        if slug not in banked_keys_by_slug:
            banked_keys_by_slug[slug] = {
                kt
                for record in _records(slug)
                if isinstance(tag := record.get("round.cell"), Mapping)
                and (kt := key_tuple(tag)) is not None  # skip a corrupt key
            }
        return banked_keys_by_slug[slug]

    executed: list[Mapping[str, Any]] = []
    reused: list[Mapping[str, Any]] = []
    for point in points:
        slug = point.pin.repo.lower()
        where = f"{point.pin.id!r} replicate {point.replicate} sweep {point.sweep}"
        banked = key_tuple(point.key) in _banked_keys(slug)
        if banked and not force:
            say(f"  {where}: banked — reused (pass --force to re-run)")
            reused.append(point.key)
            continue
        view = views_by_pin[point.pin.id]  # resolved in the range preflight above
        say(f"  {where}: running ({cell.shape}, {cell.invocation.backend!r})…")
        result = _run_point(
            cell,
            backend,
            view,
            point,
            base_text=base_text,
            records=_records(slug),
            launcher=launcher,
            base_dir=base_dir,
        )
        say(f"  {where}: record at {result['record_path']}")
        executed.append(point.key)
        records_by_slug.pop(slug, None)  # refresh: the store grew
        banked_keys_by_slug.pop(slug, None)  # and its derived key set
    say(
        f"cell {cell.id!r}: {len(executed)} executed, {len(reused)} reused "
        f"(idempotent by key)"
    )
    return RunSummary(cell_id=cell.id, executed=tuple(executed), reused=tuple(reused))


def _run_point(
    cell: Cell,
    backend,
    view,
    point: PlannedPoint,
    *,
    base_text: str,
    records: Sequence[Mapping[str, Any]],
    launcher,
    base_dir: Path | None,
) -> dict:
    """One point through the unchanged replay driver. BOUNDARY.

    EVERY point launches from a TEMP instructions file written here from the
    up-front-read ``base_text`` — the exact bytes hashed into the point's
    ``variant_hash`` (see :func:`run_cell`). The replay driver re-reads its
    instructions path at launch, so handing it the original
    ``cell.instructions_path`` would let an edit or symlink swap between the
    up-front read and this launch run DIFFERENT bytes than the record is banked
    under — a corrupt point mislabeled under a hash it never ran. Materializing
    the immutable bytes closes that window for both arms, and the driver call is
    byte-identical between them except for the instructions path it is handed:

    - informed sweeps ≥ 2 launch ``base_text`` composed with the prior sweeps'
      posted findings (the runner layer, ADR-0049);
    - blind / sweep-1 launch ``base_text`` verbatim.

    The temp file is removed after the run (the per-run artifact bundle already
    banked the exact prompt).
    """
    if cell.sweep_mode == "informed" and point.sweep > 1:
        priors = _prior_findings(records, point)
        launch_text = compose_informed_instructions(base_text, priors)
        logger.info(
            "lab: informed sweep %d of %s primed with %d prior finding(s)",
            point.sweep,
            point.pin.id,
            len(priors),
            extra={"cell": cell.id, "sweep": point.sweep},
        )
    else:
        launch_text = base_text
    fd, instructions_path = tempfile.mkstemp(
        prefix=f"lab-{cell.id}-", suffix=".txt", text=True
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(launch_text)
    try:
        if cell.shape == "fanout":
            return replay_mod.run_fanout_replay(
                backend,
                view,
                model=cell.invocation.model,
                timeout=cell.invocation.timeout,
                instructions_path=instructions_path,
                dimensions=cell.dimensions or None,
                calibrator=cell.calibrator,
                invocation_overrides=cell.dimension_invocations or None,
                cell=point.key,
                launcher=launcher,
                base_dir=base_dir,
            )
        return replay_mod.run_replay(
            backend,
            view,
            model=cell.invocation.model,
            timeout=cell.invocation.timeout,
            instructions_path=instructions_path,
            cell=point.key,
            launcher=launcher,
            base_dir=base_dir,
        )
    finally:
        try:
            os.unlink(instructions_path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
