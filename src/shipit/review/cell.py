"""The declarative Cell file — one review experiment, parsed and validated.

See docs/adr/0049-review-lab-cells.md.
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

CELL_SCHEMA_VERSION = 1

DEFAULT_CELLS_DIR = Path("lab") / "cells"

SHAPES = ("single", "fanout")

DEDUP_MODES = ("mechanical", "semantic", "calibrated")

SWEEP_MODES = ("blind", "informed")

CONTROL_AXIS = "control"


class CellError(ValueError):
    """A cell file that cannot be trusted."""


@dataclass(frozen=True)
class CellInvocation:
    backend: str = "codex"
    model: str = "pro"
    timeout: str = "600s"


@dataclass(frozen=True)
class Cell:
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
        return self.baseline == self.id


def _require_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CellError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def _require_cell_name(raw: Mapping[str, Any], key: str, where: str) -> str:
    """Bare, so the ``<cells>/<name>.toml`` lookup cannot traverse out."""
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


#: One point per (pin × replicate × sweep), so unbounded is an OOM vector.
MAX_SWEEP_COUNT = 1000

#: A total ceiling too: the per-axis bound still permits a million launches.
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
    """In-repo relative only: another path could feed a local secret to the model."""
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
                "for the fan-out's default set (the ADR-0045 concern four), or "
                "list at least one dimension (an explicit empty list is a config "
                "mistake, not the default; the Roster `dimensions` option rejects "
                "it the same way)"
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
            f"{where}: [pipeline] dedup = {dedup!r} applies only to the "
            "fan-out shape — a single pass has no union to dedup"
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
    direct = Path(ref)
    if direct.is_file():
        return direct
    return cells_dir / f"{ref}.toml"


def _effective_pins(cell: Cell, fixture: Fixture) -> frozenset[str]:
    if cell.prs:
        return frozenset(cell.prs)
    return frozenset(pin.id for pin in fixture.prs)


def check_fair_pair(cell: Cell, baseline: Cell, fixture: Fixture) -> None:
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
    chain = [cell]
    visited = {cell.id}
    current = cell
    while not current.is_control:
        parent = resolve_baseline(current)
        if parent.id in visited:
            trail = " -> ".join(
                repr(cid) for cid in (*(c.id for c in chain), parent.id)
            )
            raise CellError(
                f"cell {cell.id!r} has a cyclic baseline chain "
                f"({trail}) — a "
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

    def resolve(current: Cell) -> Cell:
        path = cells_dir / f"{current.baseline}.toml"
        if not path.is_file():
            raise CellError(
                f"cell {current.id!r} names baseline {current.baseline!r}, "
                f"but {current.baseline!r} has no cell file in cells dir "
                f"{cells_dir} ({str(path)!r} does not exist) — every link of the "
                "baseline chain is part of the reviewed lineage; commit the "
                "missing cell first"
            )
        return load_cell(path)

    return check_baseline_lineage(cell, fixture, resolve)


def instructions_variant_text(cell: Cell, base_text: str) -> str:
    """A fan-out cell folds in its dimension set, which lives in code and would
    otherwise under-key the experiment."""
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


#: The fields of :func:`run_key` that ARE the key; the rest decorates.
KEY_FIELDS = ("id", "fixture_version", "pr", "variant", "replicate", "sweep")


_KEY_SCALAR_TYPES = (str, int, type(None))


def key_tuple(tag: Mapping[str, Any]) -> tuple | None:
    values = tuple(tag.get(field) for field in KEY_FIELDS)
    if any(not isinstance(value, _KEY_SCALAR_TYPES) for value in values):
        return None
    return values


def record_matches_key(record: Mapping[str, Any], key: Mapping[str, Any]) -> bool:
    tag = record.get("round.cell")
    if not isinstance(tag, Mapping):
        return False
    return all(tag.get(field_name) == key[field_name] for field_name in KEY_FIELDS)


#: Stripped: a prior finding's fields derive from untrusted diffs.
_PRIOR_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

MAX_PRIOR_FINDINGS = 200
_MAX_PRIOR_FIELD_LEN = 500


def _clean_prior_field(value: Any, *, limit: int = _MAX_PRIOR_FIELD_LEN) -> str:
    flattened = " ".join(_PRIOR_CONTROL_CHARS.sub("·", str(value)).split())
    return flattened[:limit]


def compose_informed_instructions(
    base_text: str, prior_findings: Sequence[Mapping[str, Any]]
) -> str:
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
