"""cell — the declarative **Cell** file: one review experiment, stated in-repo.

A Cell (ADR-0049; ``docs/spec/review-lab.md``) is a small committed TOML file
declaring everything one review experiment runs: the Ground-truth fixture
version + PR subset it replays, the pipeline shape (single pass or the
dimension fan-out, dedup mode, calibrator on/off), the **Invocation**
(backend/model/timeout, with experiment-only per-dimension overrides), the
instructions **Variant**, and the sweep plan (``count`` × ``replicates``,
blind or informed). Two fields are MANDATORY and validated at load —
``baseline`` (the cell this one is compared against — usually the control; a
composition cell may name a treatment, see :func:`check_fair_pair`) and
``axis`` (the ONE thing changed vs that baseline) — so an unfair comparison
fails at PR review of the cell file, before any token burns.

This module is the PURE domain layer: parse + validate
(:func:`parse_cell` / :func:`load_cell`), the idempotency key
(:func:`run_key` / :func:`record_matches_key` — cell, fixture PR, fixture
version, variant, replicate, sweep, the ADR-0049 banked-reuse key), the
informed-sweep instruction composition (:func:`compose_informed_instructions`
— prior findings enter at the RUNNER layer, never as a replay-driver change),
and the fairness checks — per-edge (:func:`check_fair_pair`) and the
baseline-chain walk to the control (:func:`check_baseline_lineage` /
:func:`load_baseline_lineage`). The I/O runner lives in
:mod:`shipit.review.labrun`; the convergence-curve report in
:mod:`shipit.review.curve`; the thin CLI in :mod:`shipit.verbs.lab`.

Validation posture: LOUD on any defect, including UNKNOWN keys — a misspelled
knob silently ignored would run a different experiment than the reviewed file
declares, which is exactly the mislabeled-arm failure the lab exists to kill.
For the same reason an ``[invocation]`` ``reasoning`` key is REJECTED with its
own message: the codex/claude backends DO carry a reasoning knob now
(#685/#691), but the lab runner does not yet thread a level from the Cell
through the replay driver into the backend, so accepting the field would stamp
arms with a level that never reached a run — the RVW02 failure reproduced in
config.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .calibrator import CalibratorConfig
from .dimensions import (
    DEFAULT_DIMENSION_NAMES,
    fanout_variant_text,
    known_dimension_names,
    resolve_dimensions,
)
from .groundtruth import Fixture

__all__ = [
    "CELL_SCHEMA_VERSION",
    "DEFAULT_CELLS_DIR",
    "Cell",
    "CellError",
    "CellInvocation",
    "check_baseline_lineage",
    "check_fair_pair",
    "compose_informed_instructions",
    "instructions_variant_text",
    "key_tuple",
    "load_baseline_lineage",
    "load_cell",
    "parse_cell",
    "record_matches_key",
    "resolve_cell_path",
    "run_key",
]

#: Bump when the cell FILE FORMAT changes (field set / shapes) — the same
#: convention as :data:`shipit.review.groundtruth.FIXTURE_SCHEMA_VERSION`.
CELL_SCHEMA_VERSION = 1

#: Where cells live, relative to the repo root — in-repo on purpose (ADR-0049):
#: a cell is reviewed like code, and the baseline/axis declaration is exactly
#: what its PR review checks.
DEFAULT_CELLS_DIR = Path("lab") / "cells"

#: The declared pipeline shapes: one monolithic range pass, or the dimension
#: fan-out (the two arms of the sanctioned offline replay driver, RVW03-WS01).
SHAPES = ("single", "fanout")

#: The dedup modes of the fan-out shape: the mechanical union dedup (the
#: shipped default), or the dormant LLM judge opted back on (ADR-0049's late
#: calibrator cell with its entry bar).
DEDUP_MODES = ("mechanical", "calibrated")

#: The sweep modes: ``blind`` sweeps repeat the same instructions; ``informed``
#: sweeps compose the prior sweeps' posted findings into the instructions (an
#: explicit declared mode — never a silent default; ADR-0049).
SWEEP_MODES = ("blind", "informed")

#: The axis value a CONTROL cell declares (a control is its own baseline and
#: changes nothing — every treatment names it and one real axis).
CONTROL_AXIS = "control"


class CellError(ValueError):
    """A cell file that cannot be trusted: parse or validation failure.

    Always LOUD (never a silent skip or a silently-ignored key): a cell that
    runs anything other than what its reviewed file declares is a mislabeled
    experiment arm — the failure mode the Review Lab exists to kill.
    """


@dataclass(frozen=True)
class CellInvocation:
    """The cell's pinned **Invocation**: which backend runs the review, how.

    ``backend`` is a funnel-agent token (``codex`` / ``agy`` — resolved through
    :func:`shipit.agent.backend.by_funnel_agent` at run time); ``model`` and
    ``timeout`` apply to every pass unless a per-dimension override
    (:attr:`Cell.dimension_invocations`) narrows one pass. There is
    deliberately NO ``reasoning`` field: the backends carry the knob (#685/#691)
    but the lab runner does not thread a level into the replay driver yet, so a
    recorded-but-unwired level would mislabel the arm (see module doc).
    """

    backend: str = "codex"
    model: str = "pro"
    timeout: str = "600s"


@dataclass(frozen=True)
class Cell:
    """One validated experiment cell — everything a ``lab run`` resolves.

    ``baseline``/``axis`` are the mandatory fairness declaration (ADR-0049):
    a control cell names ITSELF as baseline and declares ``axis = "control"``;
    a treatment names its control and states the ONE thing it changes.
    ``fixture_version`` pins the label-set version the cell's scores cite;
    ``prs`` the fixture pin subset it replays (empty = every pin).
    ``dimension_invocations`` are the experiment-only per-dimension Invocation
    overrides (``{dimension name: {"model"/"timeout": …}}``) — they live HERE,
    in the lab, never in product Roster configuration (ADR-0049).
    """

    id: str
    baseline: str
    axis: str
    fixture_version: int
    shape: str
    description: str = ""
    prs: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    dedup: str = "mechanical"
    calibrator: CalibratorConfig | None = None
    invocation: CellInvocation = field(default_factory=CellInvocation)
    dimension_invocations: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    instructions_path: str | None = None
    label: str | None = None
    sweeps: int = 1
    sweep_mode: str = "blind"
    replicates: int = 1

    @property
    def is_control(self) -> bool:
        """True when this cell IS its own baseline (the control arm)."""
        return self.baseline == self.id


def _require_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CellError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def _require_cell_name(raw: Mapping[str, Any], key: str, where: str) -> str:
    """A required non-empty string that is ALSO a bare cell name — no path
    separators or ``.``/``..``. ``id`` and ``baseline`` both name a file under
    the cells directory (``<cells>/<name>.toml``), so a value like ``../x`` would
    let baseline lookup traverse OUT of that directory when ``lab run``/``report``
    load the pair; a bare name keeps the lookup inside the cells dir."""
    value = _require_str(raw, key, where)
    if "/" in value or "\\" in value or value in (".", ".."):
        raise CellError(
            f"{where}: {key!r} {value!r} must be a bare cell name (no path "
            "separators) — it names a file under the cells directory, and a "
            "traversal path would escape it"
        )
    return value


def _optional_str(raw: Mapping[str, Any], key: str, where: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CellError(f"{where}: {key!r} must be a non-empty string when present")
    return value.strip()


#: The ceiling on a sweep/replicate count. The runner allocates one point per
#: (pin × replicate × sweep), so an unbounded count is an OOM vector (a typo'd
#: ``count = 1000000000`` would build a billion-tuple before anything runs) —
#: a plan this large is a mistake, never a real experiment.
MAX_SWEEP_COUNT = 1000

#: The ceiling on a cell's TOTAL planned points (``pins × replicates ×
#: sweeps``). :data:`MAX_SWEEP_COUNT` bounds each axis alone, but their product
#: still reaches a million points per pin (``1000 × 1000``) — enough to exhaust
#: memory building the plan tuple and to bill a million model runs from one
#: reviewed cell. :func:`shipit.review.labrun.plan_points` enforces this total
#: so both the runner and the report refuse a runaway plan before it allocates.
#: One point is one model launch; a cell asking for more is a config error.
MAX_PLANNED_POINTS = 10_000


def _positive_int(raw: Mapping[str, Any], key: str, where: str, default: int) -> int:
    value = raw.get(key, default)
    # bool is an int subclass; `count = true` must not parse as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CellError(f"{where}: {key!r} must be a positive integer")
    if value > MAX_SWEEP_COUNT:
        raise CellError(
            f"{where}: {key!r} = {value} exceeds the max {MAX_SWEEP_COUNT} — "
            "the runner allocates one point per pin × replicate × sweep, so a "
            "plan this large is a mistake, not an experiment"
        )
    return value


def _validate_instructions_path(path: str, where: str) -> None:
    """Constrain a cell's ``[instructions].path`` to an in-repo relative file.

    A cell TOML is committed and reviewed, but its ``path`` is still untrusted
    input: an absolute path (``/etc/passwd``), a home-expansion (``~/.ssh/…``),
    or a ``..`` traversal would let a cell read a LOCAL SECRET off the
    maintainer's disk and hand its contents to the model as prompt text when
    ``lab run`` executes — arbitrary-file-read → exfiltration. Cells read
    instructions from in-repo files ONLY, so anything but a repo-relative path
    with no parent-directory hops is a loud refusal here, before any run.
    """
    candidate = Path(path)
    if candidate.is_absolute() or path.startswith("~") or ".." in candidate.parts:
        raise CellError(
            f"{where}: [instructions] 'path' must be a repo-relative path with "
            f"no '..' segments (got {path!r}) — a cell reads its instructions "
            "from in-repo files only; an absolute, '~', or traversal path could "
            "exfiltrate a local secret into the prompt"
        )


def _reject_unknown_keys(
    raw: Mapping[str, Any], known: Sequence[str], where: str
) -> None:
    """The no-silently-ignored-knob guard: an unknown key is a LOUD error —
    a typo'd field must not quietly run a different experiment than the
    reviewed cell file declares."""
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise CellError(
            f"{where}: unknown key(s) {', '.join(map(repr, unknown))} — "
            f"known keys: {', '.join(known)}"
        )


def _parse_invocation(raw: Any, where: str) -> CellInvocation:
    if raw is None:
        return CellInvocation()
    if not isinstance(raw, Mapping):
        raise CellError(f"{where}: [invocation] must be a table")
    if "reasoning" in raw:
        raise CellError(
            f"{where}: [invocation] 'reasoning' is not wireable — the "
            "codex/claude backends carry a reasoning knob (#685/#691), but the "
            "lab runner does not thread a level from the Cell into the replay "
            "driver yet, so a recorded-but-unapplied level would mislabel the "
            "experiment arm. Drop the key."
        )
    _reject_unknown_keys(raw, ["backend", "model", "timeout", "dimensions"], where)
    defaults = CellInvocation()
    return CellInvocation(
        backend=_optional_str(raw, "backend", where) or defaults.backend,
        model=_optional_str(raw, "model", where) or defaults.model,
        timeout=_optional_str(raw, "timeout", where) or defaults.timeout,
    )


def _parse_dimension_invocations(
    raw: Any, *, shape: str, effective_dimensions: Sequence[str], where: str
) -> dict[str, dict[str, str]]:
    """The per-dimension override table (``[invocation.dimensions.<name>]``):
    experiment-only capability, validated against the cell's OWN pass set —
    an override on a pass the cell never runs is a reviewed lie."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CellError(f"{where}: [invocation.dimensions] must be a table of tables")
    if shape != "fanout":
        raise CellError(
            f"{where}: per-dimension invocation overrides apply only to the "
            "fan-out shape — a single-pass cell has no dimension passes"
        )
    overrides: dict[str, dict[str, str]] = {}
    for name, fields in raw.items():
        entry_where = f"{where}: [invocation.dimensions.{name}]"
        if name not in effective_dimensions:
            raise CellError(
                f"{entry_where} names a dimension outside this cell's pass set "
                f"({', '.join(effective_dimensions)})"
            )
        if not isinstance(fields, Mapping):
            raise CellError(f"{entry_where} must be a table")
        if "reasoning" in fields:
            raise CellError(
                f"{entry_where}: 'reasoning' is not wireable — see [invocation]"
            )
        if "backend" in fields:
            raise CellError(
                f"{entry_where}: per-dimension 'backend' is not supported — the "
                "fan-out runs one reviewer backend per round (per-dimension "
                "overrides carry 'model'/'timeout' only)"
            )
        _reject_unknown_keys(fields, ["model", "timeout"], entry_where)
        entry = {
            key: _require_str(fields, key, entry_where)
            for key in ("model", "timeout")
            if key in fields
        }
        if not entry:
            raise CellError(f"{entry_where} is empty — declare 'model' or 'timeout'")
        overrides[name] = entry
    return overrides


def parse_cell(data: Mapping[str, Any], *, where: str = "cell") -> Cell:
    """Parsed TOML → validated :class:`Cell`. PURE; loud on any defect.

    Validates the full contract here — the mandatory ``baseline``/``axis``
    fairness declaration (control cells name themselves and declare
    ``axis = "control"``; treatments name a different baseline and a real
    axis), the fixture version pin, shape/dedup/sweep vocabularies, dimension
    names against the closed registry, calibrator config (constructed, so a
    malformed field fails loud), and per-dimension overrides — so every
    consumer downstream (runner, curve report, tests) can trust a
    :class:`Cell` unconditionally.
    """
    if not isinstance(data, Mapping):
        raise CellError(f"{where}: cell file must be a TOML table")
    _reject_unknown_keys(
        data,
        [
            "schema",
            "id",
            "baseline",
            "axis",
            "description",
            "fixture",
            "pipeline",
            "invocation",
            "instructions",
            "sweeps",
        ],
        where,
    )
    schema = data.get("schema", CELL_SCHEMA_VERSION)
    if schema != CELL_SCHEMA_VERSION:
        raise CellError(
            f"{where}: cell schema {schema!r} != supported {CELL_SCHEMA_VERSION} — "
            "this shipit is too old or the file too new"
        )
    cell_id = _require_cell_name(data, "id", where)
    baseline = _require_cell_name(data, "baseline", where)
    axis = _require_str(data, "axis", where)
    if baseline == cell_id and axis != CONTROL_AXIS:
        raise CellError(
            f"{where}: a cell that is its own baseline is the CONTROL and must "
            f"declare axis = {CONTROL_AXIS!r} (got {axis!r})"
        )
    if baseline != cell_id and axis == CONTROL_AXIS:
        raise CellError(
            f"{where}: a treatment cell (baseline {baseline!r}) must declare its "
            f"ONE changed axis — axis = {CONTROL_AXIS!r} is reserved for the "
            "control (ADR-0049: one axis per cell, declared, or the comparison "
            "is unfair)"
        )
    description = _optional_str(data, "description", where) or ""

    fixture_raw = data.get("fixture")
    if not isinstance(fixture_raw, Mapping):
        raise CellError(
            f"{where}: [fixture] table is required — a cell pins the fixture "
            "version its scores cite (numbers across versions never compare)"
        )
    _reject_unknown_keys(fixture_raw, ["version", "prs"], f"{where}: [fixture]")
    fixture_version = fixture_raw.get("version")
    if (
        isinstance(fixture_version, bool)
        or not isinstance(fixture_version, int)
        or fixture_version < 1
    ):
        raise CellError(f"{where}: [fixture] 'version' must be a positive integer")
    prs_raw = fixture_raw.get("prs", [])
    if not isinstance(prs_raw, Sequence) or isinstance(prs_raw, str):
        raise CellError(f"{where}: [fixture] 'prs' must be an array of pin ids")
    prs = []
    for i, pin in enumerate(prs_raw):
        if not isinstance(pin, str) or not pin.strip():
            raise CellError(f"{where}: [fixture] prs[{i}] must be a non-empty string")
        prs.append(pin.strip())
    if len(set(prs)) != len(prs):
        raise CellError(f"{where}: [fixture] 'prs' has duplicate pin ids")

    pipeline_raw = data.get("pipeline")
    if not isinstance(pipeline_raw, Mapping):
        raise CellError(
            f"{where}: [pipeline] table is required — declare the shape "
            f"({' | '.join(SHAPES)}) explicitly; the pipeline is an axis, "
            "never an implicit default"
        )
    _reject_unknown_keys(
        pipeline_raw,
        ["shape", "dimensions", "dedup", "calibrator"],
        f"{where}: [pipeline]",
    )
    shape = _require_str(pipeline_raw, "shape", f"{where}: [pipeline]")
    if shape not in SHAPES:
        raise CellError(
            f"{where}: [pipeline] 'shape' must be one of: {', '.join(SHAPES)}; "
            f"got {shape!r}"
        )
    dimensions_raw = pipeline_raw.get("dimensions")
    dimensions: tuple[str, ...] = ()
    if dimensions_raw is not None:
        if shape != "fanout":
            raise CellError(
                f"{where}: [pipeline] 'dimensions' applies only to the fan-out shape"
            )
        if not isinstance(dimensions_raw, Sequence) or isinstance(dimensions_raw, str):
            raise CellError(
                f"{where}: [pipeline] 'dimensions' must be an array of dimension names"
            )
        if not dimensions_raw:
            raise CellError(
                f"{where}: [pipeline] 'dimensions' is an empty list — omit the key "
                "for the shipped default set, or list at least one dimension (an "
                "explicit empty list is a config mistake, not the default; the "
                "Roster `dimensions` option rejects it the same way)"
            )
        names = []
        for i, name in enumerate(dimensions_raw):
            if not isinstance(name, str) or not name.strip():
                raise CellError(
                    f"{where}: [pipeline] dimensions[{i}] must be a non-empty string"
                )
            names.append(name.strip())
        try:
            resolve_dimensions(names)
        except KeyError as exc:
            raise CellError(
                f"{where}: [pipeline] unknown dimension {exc.args[0]!r} — known "
                f"dimensions: {', '.join(known_dimension_names())}"
            ) from None
        if len(set(names)) != len(names):
            raise CellError(f"{where}: [pipeline] 'dimensions' has duplicates")
        dimensions = tuple(names)
    dedup = pipeline_raw.get("dedup", "mechanical")
    if dedup not in DEDUP_MODES:
        raise CellError(
            f"{where}: [pipeline] 'dedup' must be one of: {', '.join(DEDUP_MODES)}; "
            f"got {dedup!r}"
        )
    if dedup != "mechanical" and shape != "fanout":
        raise CellError(
            f"{where}: [pipeline] dedup = 'calibrated' applies only to the "
            "fan-out shape — a single pass has no union to calibrate"
        )
    calibrator_raw = pipeline_raw.get("calibrator")
    calibrator: CalibratorConfig | None = None
    if dedup == "calibrated":
        if not isinstance(calibrator_raw, Mapping):
            raise CellError(
                f"{where}: [pipeline.calibrator] table is required when "
                "dedup = 'calibrated' (the judge's Invocation is part of the "
                "reviewed cell, never an ambient default)"
            )
        _reject_unknown_keys(
            calibrator_raw,
            ["backend", "model", "reasoning", "timeout"],
            f"{where}: [pipeline.calibrator]",
        )
        try:
            calibrator = CalibratorConfig(**dict(calibrator_raw))
        except (TypeError, ValueError) as exc:
            raise CellError(f"{where}: [pipeline.calibrator] invalid: {exc}") from exc
    elif calibrator_raw is not None:
        raise CellError(
            f"{where}: [pipeline.calibrator] is set but dedup is {dedup!r} — "
            "opt the judge on explicitly with dedup = 'calibrated', or drop the "
            "table (a half-declared judge is an unlabeled arm)"
        )

    invocation_raw = data.get("invocation")
    invocation = _parse_invocation(invocation_raw, where)
    # Omitted `dimensions` means the SHIPPED default set, not everything the
    # registry knows — the experiment-only severity tiers (ADR-0051) run only
    # when a cell lists them explicitly. `dimensions` is empty here ONLY when
    # the key was omitted (an explicit empty list was rejected loud above), so
    # the fallback never masks a config mistake.
    effective_dimensions = dimensions if dimensions else DEFAULT_DIMENSION_NAMES
    dimension_invocations = _parse_dimension_invocations(
        invocation_raw.get("dimensions")
        if isinstance(invocation_raw, Mapping)
        else None,
        shape=shape,
        effective_dimensions=effective_dimensions,
        where=where,
    )

    instructions_raw = data.get("instructions")
    instructions_path: str | None = None
    label: str | None = None
    if instructions_raw is not None:
        instr_where = f"{where}: [instructions]"
        if not isinstance(instructions_raw, Mapping):
            raise CellError(f"{instr_where} must be a table")
        _reject_unknown_keys(instructions_raw, ["path", "label"], instr_where)
        instructions_path = _optional_str(instructions_raw, "path", instr_where)
        if instructions_path is not None:
            _validate_instructions_path(instructions_path, instr_where)
        label = _optional_str(instructions_raw, "label", instr_where)

    sweeps_raw = data.get("sweeps")
    sweeps, sweep_mode, replicates = 1, "blind", 1
    if sweeps_raw is not None:
        if not isinstance(sweeps_raw, Mapping):
            raise CellError(f"{where}: [sweeps] must be a table")
        _reject_unknown_keys(
            sweeps_raw, ["count", "mode", "replicates"], f"{where}: [sweeps]"
        )
        sweeps = _positive_int(sweeps_raw, "count", f"{where}: [sweeps]", 1)
        sweep_mode = sweeps_raw.get("mode", "blind")
        if sweep_mode not in SWEEP_MODES:
            raise CellError(
                f"{where}: [sweeps] 'mode' must be one of: "
                f"{', '.join(SWEEP_MODES)}; got {sweep_mode!r} (informed vs "
                "blind is an explicit declared mode, ADR-0049)"
            )
        replicates = _positive_int(sweeps_raw, "replicates", f"{where}: [sweeps]", 1)

    return Cell(
        id=cell_id,
        baseline=baseline,
        axis=axis,
        description=description,
        fixture_version=fixture_version,
        prs=tuple(prs),
        shape=shape,
        dimensions=dimensions,
        dedup=dedup,
        calibrator=calibrator,
        invocation=invocation,
        dimension_invocations=dimension_invocations,
        instructions_path=instructions_path,
        label=label,
        sweeps=sweeps,
        sweep_mode=sweep_mode,
        replicates=replicates,
    )


def load_cell(path: Path) -> Cell:
    """Read + validate the cell file at ``path``. The one read boundary.

    Enforces ``id == filename stem`` so ``lab run <id>`` is unambiguous — two
    files cannot claim one cell id, and a copy-edited treatment that forgot to
    change its ``id`` fails loud instead of silently impersonating its control.
    """
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise CellError(f"no cell file at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise CellError(f"cell {path} is not valid TOML: {exc}") from exc
    cell = parse_cell(data, where=str(path))
    if cell.id != path.stem:
        raise CellError(
            f"{path}: cell id {cell.id!r} != filename stem {path.stem!r} — the "
            "file name IS the cell's handle; rename one of them"
        )
    return cell


def resolve_cell_path(ref: str, cells_dir: Path = DEFAULT_CELLS_DIR) -> Path:
    """A CLI cell reference → the file path: an existing path verbatim, else
    ``<cells_dir>/<ref>.toml`` (the in-repo cells directory)."""
    direct = Path(ref)
    if direct.is_file():
        return direct
    return cells_dir / f"{ref}.toml"


def _effective_pins(cell: Cell, fixture: Fixture) -> frozenset[str]:
    """The pin id set a cell actually replays: its declared ``prs``, or — the
    ``prs = []`` "every fixture pin" convention (:func:`resolve_pins` expands
    it) — the fixture's full pin set. The denominator a fair-pair compares."""
    if cell.prs:
        return frozenset(cell.prs)
    return frozenset(pin.id for pin in fixture.prs)


def check_fair_pair(cell: Cell, baseline: Cell, fixture: Fixture) -> None:
    """Loud :class:`CellError` when ``cell`` and its ``baseline`` cannot be
    fairly compared. PURE.

    The machine-checkable half of the ADR-0049 fairness bar: the two cells must
    score against the SAME fixture version and the SAME PR subset (differing
    denominators answer different questions — comparing them is the RVW02
    incomparable-arms failure), and ``cell.baseline`` must actually name
    ``baseline``. The baseline is USUALLY the control, but need not be: a
    COMPOSITION cell layers one new axis onto a treatment that already earned
    its edge (e.g. ``sevtiers-informed`` vs ``fanout-sevtiers``, #717). The
    chain does not hide axes because the WHOLE chain is walked
    (:func:`check_baseline_lineage` — at ``lab run``/``report`` time and in
    the committed-cells test), fair-pair-checking every hop and requiring
    termination at a control, so each link is declared and reviewed — the
    one-axis discipline holds per pair, all the way down to the control.
    The PR subset compares EFFECTIVE pin sets against ``fixture`` —
    ``prs = []`` means "every fixture pin", so a control that omits ``prs``
    and a treatment that lists all of them explicitly are the SAME
    denominator, not an unfair pair. The remaining half — that the axis named
    really is the ONLY difference — is what PR review of the cell pair checks,
    which the mandatory declarations make reviewable.
    """
    if cell.baseline != baseline.id:
        raise CellError(
            f"cell {cell.id!r} declares baseline {cell.baseline!r}, not {baseline.id!r}"
        )
    if cell.fixture_version != baseline.fixture_version:
        raise CellError(
            f"cells {cell.id!r} (fixture v{cell.fixture_version}) and "
            f"{baseline.id!r} (fixture v{baseline.fixture_version}) pin "
            "different fixture versions — their numbers never compare"
        )
    if _effective_pins(cell, fixture) != _effective_pins(baseline, fixture):
        raise CellError(
            f"cells {cell.id!r} and {baseline.id!r} replay different PR subsets "
            "— their recall denominators differ, so the comparison is unfair"
        )


def check_baseline_lineage(
    cell: Cell,
    fixture: Fixture,
    resolve_baseline: Callable[[Cell], Cell],
) -> tuple[Cell, ...]:
    """Walk ``cell``'s declared baseline chain to its control, fair-pair
    checking every hop. Returns the chain ``(cell, …, control)``. PURE.

    :func:`check_fair_pair` proves one EDGE; this proves the LINEAGE (#719):
    starting from ``cell``, repeatedly resolve ``current.baseline`` via
    ``resolve_baseline`` — which is given the referencing cell and must return
    its loaded baseline or raise a :class:`CellError` naming the missing
    ancestor (and where it searched, see :func:`load_baseline_lineage`) — and
    require the walk to terminate at a control (``baseline == id``). A
    repeated id is a loud cycle error: without this walk two treatments could
    name each other, share fixture/pins, pass every per-edge check, and a
    control-less or cyclic lineage would silently read as a fair experiment.
    A control cell is its own one-cell chain (``resolve_baseline`` is never
    called).
    """
    chain = [cell]
    visited = {cell.id}
    current = cell
    while not current.is_control:
        parent = resolve_baseline(current)
        if parent.id in visited:
            raise CellError(
                f"cell {cell.id!r} has a cyclic baseline chain "
                f"({' -> '.join([*(c.id for c in chain), parent.id])}) — a "
                "baseline chain must terminate at a control cell "
                "(baseline == id), so a cell can never be its own ancestor"
            )
        check_fair_pair(current, parent, fixture)
        visited.add(parent.id)
        chain.append(parent)
        current = parent
    return tuple(chain)


def load_baseline_lineage(
    cell: Cell, fixture: Fixture, cells_dir: Path = DEFAULT_CELLS_DIR
) -> tuple[Cell, ...]:
    """:func:`check_baseline_lineage` over the cells DIRECTORY — each ancestor
    loads from ``<cells_dir>/<baseline>.toml`` (the same lookup ``lab run`` /
    ``lab report`` use). Returns the chain ``(cell, …, control)``; a missing
    ancestor is a loud :class:`CellError` naming the missing cell id and the
    cells dir searched, a cycle or an unfair hop the walker's own error.
    """

    def resolve(current: Cell) -> Cell:
        path = cells_dir / f"{current.baseline}.toml"
        if not path.is_file():
            raise CellError(
                f"cell {current.id!r} names baseline {current.baseline!r}, "
                f"but {current.baseline!r} has no cell file in cells dir "
                f"{cells_dir} ({path} does not exist) — every link of the "
                "baseline chain is part of the reviewed lineage; commit the "
                "missing cell first"
            )
        return load_cell(path)

    return check_baseline_lineage(cell, fixture, resolve)


# --- the idempotency key (ADR-0049: banked results are never paid for twice) ---


def instructions_variant_text(cell: Cell, base_text: str) -> str:
    """The text ``cell``'s instructions-variant hash covers. PURE.

    ``base_text`` is the cell's BASE instructions (read once by the runner /
    report). A ``single``-shape cell hashes it verbatim — its one pass embeds
    nothing beyond the instructions, and the unchanged text keeps existing
    banked single-pass points on their recorded keys. A ``fanout`` cell folds
    its resolved dimension set — names, titles, focus texts — and its
    per-dimension Invocation overrides into the hashed text
    (:func:`~shipit.review.dimensions.fanout_variant_text`, #713): the focus
    texts live in code and the cell's ``dimensions`` list selects them, so the
    file alone under-keys the experiment — a focus-text edit (ADR-0051's
    experiment material) must change the variant and re-key, never silently
    reuse points banked under the old prompt. Both run-key derivations
    (:mod:`shipit.review.labrun` and ``lab report``) hash THIS text, so the
    report always selects exactly the records the runs banked.
    """
    if cell.shape != "fanout":
        return base_text
    return fanout_variant_text(base_text, cell.dimensions, cell.dimension_invocations)


def run_key(
    cell: Cell,
    *,
    pr_id: str,
    variant_hash: str,
    replicate: int,
    sweep: int,
) -> dict[str, Any]:
    """The FULL idempotency key of one cell run, as the ``round.cell`` record
    tag. PURE.

    The ADR-0049 key: (cell, fixture PR, fixture version, variant, replicate,
    sweep). ``variant_hash`` is the content hash of the cell's variant text —
    the BASE instructions, folded with a fan-out cell's resolved dimension set
    and per-dimension overrides (:func:`instructions_variant_text`, #713) —
    in the ``sha256:`` scheme of
    :func:`shipit.harness.eval.variant.variant_of`. Editing the prompt — the
    instructions file OR a dimension focus text — changes the key, so banked
    records of the old prompt are never silently reused for the new one. An
    informed sweep's COMPOSED instructions hash differently per sweep (they
    embed prior findings), so the record's own ``round.variant`` cannot key
    reuse — the base hash here can, and does.
    The non-key fields (axis/baseline/sweep_mode/label) ride along for the
    report's benefit; :func:`record_matches_key` compares KEY fields only.
    """
    return {
        "id": cell.id,
        "baseline": cell.baseline,
        "axis": cell.axis,
        "fixture_version": cell.fixture_version,
        "pr": pr_id,
        "variant": variant_hash,
        "replicate": replicate,
        "sweep": sweep,
        "sweep_mode": cell.sweep_mode,
        "label": cell.label,
    }


#: The fields of :func:`run_key` that ARE the idempotency key — the ADR-0049
#: six-tuple. The rest of the ``round.cell`` tag is report decoration.
KEY_FIELDS = ("id", "fixture_version", "pr", "variant", "replicate", "sweep")


#: The value types a KEY_FIELD may legitimately hold — the shapes
#: :func:`run_key` produces (ids/variant strings, versions/replicate/sweep
#: ints). Anything else in a stored ``round.cell`` tag is a corrupt or
#: hand-edited record: it is never a real run's key, and it must not crash the
#: reader (a ``list``/``dict`` there would be an unhashable set element).
_KEY_SCALAR_TYPES = (str, int, type(None))


def key_tuple(tag: Mapping[str, Any]) -> tuple | None:
    """The ADR-0049 idempotency key as a HASHABLE tuple in ``KEY_FIELDS`` order
    — the same fields :func:`record_matches_key` compares one at a time, packed
    for O(1) set membership when many records are matched against many keys.
    PURE. ``tag`` is a ``round.cell`` tag or a :func:`run_key` dict; a missing
    field reads as ``None`` (which never equals a real run's key field).

    Returns ``None`` when any key field holds a non-scalar (a corrupt stored
    record — e.g. ``{"id": []}``), so a malformed banked line is skipped, never
    fed as an unhashable element into a caller's set (the robustness the older
    value-by-value :func:`record_matches_key` comparison had by construction)."""
    values = tuple(tag.get(field) for field in KEY_FIELDS)
    if any(not isinstance(value, _KEY_SCALAR_TYPES) for value in values):
        return None
    return values


def record_matches_key(record: Mapping[str, Any], key: Mapping[str, Any]) -> bool:
    """True when a banked review-round record carries this run's full
    idempotency key (its ``round.cell`` tag matches every KEY field). PURE."""
    tag = record.get("round.cell")
    if not isinstance(tag, Mapping):
        return False
    return all(tag.get(field_name) == key[field_name] for field_name in KEY_FIELDS)


# --- informed-sweep composition (runner-layer, never a replay-driver change) ---

#: Control characters stripped from a prior finding before it enters the next
#: sweep's prompt — a banked finding's fields ultimately derive from untrusted
#: diffs, so a terminal-escape or NUL byte must never ride through the store
#: into the composed instructions (the same CWE-150 guard the curve render uses).
_PRIOR_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: The most prior findings a composed prompt embeds, and the per-field char cap:
#: a prior finding's text is untrusted and unbounded, so a poisoned or huge
#: banked record can neither bloat the prompt without limit nor smuggle an
#: arbitrarily long field into it. Excess is truncated, not silently dropped.
MAX_PRIOR_FINDINGS = 200
_MAX_PRIOR_FIELD_LEN = 500


def _clean_prior_field(value: Any, *, limit: int = _MAX_PRIOR_FIELD_LEN) -> str:
    """One prior-finding field as inert, bounded prompt data: control chars
    neutralized, whitespace flattened to one line, length capped."""
    flattened = " ".join(_PRIOR_CONTROL_CHARS.sub("·", str(value)).split())
    return flattened[:limit]


def compose_informed_instructions(
    base_text: str, prior_findings: Sequence[Mapping[str, Any]]
) -> str:
    """The informed sweep's instructions: base text + the prior sweeps'
    posted findings. PURE.

    ADR-0049's informed mode composes prior findings INTO the instructions at
    the runner layer — the replay driver is untouched, so blind and informed
    runs drive the identical pipeline and differ only in this text. Each prior
    finding is stated as location + claim, and the sweep is told those are
    ALREADY BANKED: its job is what they missed, so repeats are wasted tokens.
    An empty ``prior_findings`` returns the base text unchanged (sweep 1 of an
    informed cell is blind by construction).

    Prior findings are UNTRUSTED data (their fields trace back to diffs), so
    they enter as inert, bounded text — control characters neutralized, each
    field length-capped, and the list truncated at :data:`MAX_PRIOR_FINDINGS` —
    never as free prose a poisoned record could bloat or use to steer the run.
    """
    if not prior_findings:
        return base_text
    lines = []
    for finding in prior_findings[:MAX_PRIOR_FINDINGS]:
        file = _clean_prior_field(finding.get("file") or "?")
        line = finding.get("line")
        loc = f"{file}:{line}" if isinstance(line, int) else file
        severity = _clean_prior_field(finding.get("severity") or "?", limit=32)
        text = _clean_prior_field(finding.get("text") or "")
        lines.append(f"- {loc} ({severity}): {text}")
    if len(prior_findings) > MAX_PRIOR_FINDINGS:
        lines.append(
            f"- (+{len(prior_findings) - MAX_PRIOR_FINDINGS} more banked "
            "finding(s) omitted from this prompt)"
        )
    return (
        f"{base_text.rstrip()}\n\n"
        "## Findings already banked by prior sweeps\n\n"
        "Earlier sweeps of this same range already reported the findings "
        "below. Do NOT re-report them (or trivial rephrasings of them) — they "
        "are already counted. Hunt for what they MISSED: different files, "
        "different defect classes, deeper interactions.\n\n" + "\n".join(lines) + "\n"
    )
