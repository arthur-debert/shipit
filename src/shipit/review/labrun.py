"""Resolve one Cell onto the offline replay driver and run its sweep plan.

Every point is idempotent by its full key, so no result is paid for twice.
See docs/adr/0049-review-lab-cells.md.
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
    instructions_variant_text,
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
    """The cell's instructions path resolved absolute, refusing one that escapes
    the working directory via symlink; ``None`` passes through."""
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
    """The cell's declared pins narrowed by ``subset``, which must stay inside
    them. The fixture version must equal the cell's pin, or the records banked
    would be incomparable."""
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
    """The cell's full sweep plan in run order, sweeps innermost so an informed
    sweep's priors are always banked before it runs. Refuses a plan over
    :data:`~shipit.review.cell.MAX_PLANNED_POINTS` before building the tuple."""
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
    """Origin slug to checkout path; a path with no resolvable origin refuses."""
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
    """The record's POSTED findings: ``post`` AND canonical."""
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
    """Every posted finding banked by this point's prior sweeps, newest record
    winning per key — the store is append-only, so a superseded run's findings
    must not leak into the next sweep's prompt."""
    priors: list[Mapping[str, Any]] = []
    for sweep in range(1, point.sweep):
        prior_key = {**point.key, "sweep": sweep}
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
    """Execute ``cell``'s sweep plan, foreground and sequential, reusing every
    banked point unless ``force``. Preflight is all-or-nothing before any model
    run bills, so a missing clone or unfetched SHA never leaves a half-run curve.
    The first failing point propagates; a re-run continues where it stopped."""
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

    # Read once, up front: the key's variant half hashes this text, and an
    # unreadable file must die before any model run bills.
    try:
        base_text = load_instructions(safe_instructions_path(cell.instructions_path))
    except OSError as exc:
        raise CellError(
            f"cell {cell.id!r}: cannot read instructions "
            f"{cell.instructions_path!r}: {exc}"
        ) from exc
    variant_hash = variant_of(instructions_variant_text(cell, base_text)).content_hash

    # Every pin must resolve to a clone before anything runs, or a silent skip
    # would shrink the curve's denominator. Slugs compare lowercased.
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

    # Likewise every pin's commit range, so an unfetched SHA refuses before any
    # point launches rather than leaving a half-run curve banked.
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

    # One source of truth for both the idempotency check and the informed
    # sweep's priors, refreshed after each write.
    records_by_slug: dict[str, list[dict[str, Any]]] = {}

    def _records(slug: str) -> list[dict[str, Any]]:
        if slug not in records_by_slug:
            records_by_slug[slug] = read_records(
                repo_from_slug(slug), base_dir, kind=REVIEW_ROUNDS_KIND
            )
        return records_by_slug[slug]

    # A set, so the per-point reuse check is O(1) rather than O(points × records).
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
    """One point through the replay driver, launched from a TEMP file holding
    the exact bytes hashed into its ``variant_hash``: the driver re-reads its
    instructions at launch, so passing the cell's own path would let an edit
    between the two run different bytes than the record is banked under."""
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
    fd, instructions_path = tempfile.mkstemp(prefix=f"lab-{cell.id}-", suffix=".txt")
    # Write by path, not through the fd: os.fdopen takes ownership only on
    # success, so it would leak the fd if fdopen itself raised.
    os.close(fd)
    try:
        Path(instructions_path).write_text(launch_text, encoding="utf-8")
        if cell.shape == "fanout":
            return replay_mod.run_fanout_replay(
                backend,
                view,
                model=cell.invocation.model,
                timeout=cell.invocation.timeout,
                instructions_path=instructions_path,
                dimensions=cell.dimensions or None,
                calibrator=cell.calibrator,
                semantic_dedup=cell.dedup == "semantic",
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
