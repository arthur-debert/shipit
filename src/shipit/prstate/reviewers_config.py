"""Reviewer configuration: the `[reviewers]` table in `.shipit.toml` in,
one validated Roster out. Everything invalid fails loud at load."""

from __future__ import annotations

import tomllib
from pathlib import Path

from ..review.calibrator import CalibratorConfig
from ..review.dimensions import known_dimension_names
from .errors import PrStateError
from .reviewers import REGISTRY, by_name
from .roster import Roster, RosterEntry

# The shipped default required set. The install scaffold renders its
# `[reviewers]` body from this map, so the two can never disagree.
DEFAULT_REVIEWERS: dict[str, bool] = {"copilot": False}


def default_reviewers_scaffold_body() -> str:
    """The `[reviewers]` TOML body the install scaffold seeds."""
    lines = [
        f"{name} = {{ rerun = {'true' if rerun else 'false'} }}"
        for name, rerun in DEFAULT_REVIEWERS.items()
    ]
    return "[reviewers]\n" + "\n".join(lines) + "\n"


OVERRIDE_FILE = ".shipit.toml"
OVERRIDE_KEY = "reviewers"

# Policy values riding the `[reviewers]` table, not reviewer entries.
ROUND_CAP_KEY = "round_cap"
POLL_INTERVAL_KEY = "poll_interval"
NIT_CAP_KEY = "nit_cap"
CALIBRATOR_KEY = "calibrator"
_RESERVED_KEYS = (ROUND_CAP_KEY, POLL_INTERVAL_KEY, NIT_CAP_KEY, CALIBRATOR_KEY)

# The `calibrator` inline-table's accepted keys; unknown ones fail loud.
_CALIBRATOR_OPTIONS = ("backend", "model", "reasoning", "timeout")

# The accepted per-reviewer options; anything else fails loud.
_RUN_STRING_OPTIONS = ("model", "instructions")
_KNOWN_OPTIONS = ("rerun", "timeout", "window", "dimensions", *_RUN_STRING_OPTIONS)


class RequiredReviewersConfigError(PrStateError):
    """The `[reviewers]` config is invalid; the message says how."""


def default_roster() -> Roster:
    """The shipped-default :class:`Roster`, rendered from :data:`DEFAULT_REVIEWERS`."""
    return Roster(
        tuple(
            RosterEntry(name=name, required=True, rerun=rerun)
            for name, rerun in DEFAULT_REVIEWERS.items()
        )
    )


def load_roster(root: str | None = None) -> Roster:
    """Read `.shipit.toml`, searching up from `root`, into one Roster."""
    config = _find_config(root)
    if config is None:
        return default_roster()
    try:
        with config.open("rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RequiredReviewersConfigError(f"malformed {config}: {exc}") from None
    return parse_roster(cfg, config_path=config)


def parse_roster(
    cfg: dict[str, object], *, config_path: str | Path = OVERRIDE_FILE
) -> Roster:
    """Validate a loaded config dict into a Roster; ``config_path`` anchors
    relative instruction paths and names errors."""
    config = Path(config_path).resolve()
    try:
        value = cfg.get(OVERRIDE_KEY)
        if value is None:
            return default_roster()
        entries = _parse_table(value, config_dir=config.parent)
        round_cap = _parse_round_cap(value)
        poll_interval = _parse_poll_interval(value)
        nit_cap = _parse_nit_cap(value)
        calibrator = _parse_calibrator(value)
        _validate(tuple(e.name for e in entries))
    except RequiredReviewersConfigError as exc:
        # Re-anchor nested validator errors onto an explicit custom path.
        if config.name == OVERRIDE_FILE:
            raise
        raise RequiredReviewersConfigError(
            str(exc).replace(OVERRIDE_FILE, str(config))
        ) from exc
    if not entries:
        return Roster(
            default_roster().entries,
            round_cap=round_cap,
            poll_interval=poll_interval,
            nit_cap=nit_cap,
            calibrator=calibrator,
        )
    try:
        return Roster(
            tuple(entries),
            round_cap=round_cap,
            poll_interval=poll_interval,
            nit_cap=nit_cap,
            calibrator=calibrator,
        )
    except ValueError as exc:
        # A tripped Roster invariant must fail loud as a config error.
        raise RequiredReviewersConfigError(f"{config}: {exc}") from exc


def _parse_table(value: object, *, config_dir: Path) -> list[RosterEntry]:
    """Parse the raw `reviewers` table into RosterEntry values, config
    order. A list/array form fails loud, reserved keys are skipped, keys
    canonicalize to adapter names, and `config_dir` anchors `instructions`."""
    if not isinstance(value, dict):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` must be a TABLE of reviewer -> "
            "{{rerun = bool}} (e.g. `[reviewers]` with `copilot = {{rerun = false}}`); "
            "a list/array form is not accepted"
        )
    entries: list[RosterEntry] = []
    seen: list[str] = []
    for name, opts in value.items():
        if name in _RESERVED_KEYS:
            continue
        if not isinstance(name, str):
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` keys must be reviewer names"
            )
        if name.lower() in _RESERVED_KEYS:
            # Reserved keys are exact-lowercase; only reviewer names
            # canonicalize, so a case-variant would silently miss.
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` reserved key must be spelled "
                f"exactly `{name.lower()}`, got {name!r}"
            )
        key = _canonical_name(name)
        if key in seen:
            _reject_duplicate_names([*seen, key])
        seen.append(key)
        entries.append(_parse_entry(name, key, opts, config_dir=config_dir))
    return entries


def _parse_round_cap(table: dict[str, object]) -> int | None:
    """Parse the table-level `round_cap`; absent is None."""
    value = table.get(ROUND_CAP_KEY)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{ROUND_CAP_KEY}` must be a positive "
            f"integer of review rounds (e.g. `round_cap = 6`), got {value!r}"
        )
    return value


def _parse_nit_cap(table: dict[str, object]) -> int | None:
    """Parse the table-level `nit_cap`; absent is None, ``0`` is legal."""
    value = table.get(NIT_CAP_KEY)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{NIT_CAP_KEY}` must be a "
            f"non-negative integer of round-1 nits (0 = floor at minor), "
            f"got {value!r}"
        )
    return value


def _parse_calibrator(table: dict[str, object]) -> CalibratorConfig | None:
    """Parse the table-level `calibrator` table; absent is None."""
    value = table.get(CALIBRATOR_KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{CALIBRATOR_KEY}` must be an "
            'options table (e.g. {backend = "claude", reasoning = "high"}), '
            f"got {value!r}"
        )
    unknown = sorted(k for k in value if k not in _CALIBRATOR_OPTIONS)
    if unknown:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{CALIBRATOR_KEY}` has unknown "
            f"option(s) {unknown} — supported options are "
            f"{sorted(_CALIBRATOR_OPTIONS)}"
        )
    kwargs: dict[str, str] = {}
    for field in ("backend", "model", "reasoning"):
        if field in value:
            raw = value[field]
            if not isinstance(raw, str) or not raw.strip():
                raise RequiredReviewersConfigError(
                    f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{CALIBRATOR_KEY}.{field}` "
                    "must be a non-empty string"
                )
            kwargs[field] = raw.strip()
    if "timeout" in value:
        seconds = _duration_value(
            f"{OVERRIDE_KEY}.{CALIBRATOR_KEY}.timeout", value["timeout"]
        )
        kwargs["timeout"] = f"{seconds}s"
    try:
        return CalibratorConfig(**kwargs)
    except ValueError as exc:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{CALIBRATOR_KEY}`: {exc}"
        ) from exc


def _parse_poll_interval(table: dict[str, object]) -> int | None:
    """Parse the table-level `poll_interval`; absent is None."""
    value = table.get(POLL_INTERVAL_KEY)
    if value is None:
        return None
    return _duration_value(f"{OVERRIDE_KEY}.{POLL_INTERVAL_KEY}", value)


def _parse_entry(name: str, key: str, opts: object, *, config_dir: Path) -> RosterEntry:
    """Parse one reviewer's options table into its :class:`RosterEntry`; a
    relative `instructions` path resolves against `config_dir`."""
    if opts is None:
        return _build_entry(name, name=key, required=True)
    if not isinstance(opts, dict):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}` must be an options table "
            "(e.g. {{rerun = true}}) or empty for defaults"
        )
    unknown = sorted(k for k in opts if k not in _KNOWN_OPTIONS)
    if unknown:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}` has unknown option(s) "
            f"{unknown} — supported options are {sorted(_KNOWN_OPTIONS)} "
            "(`rerun` controls re-review; `window` is the readiness wait window; "
            "`model`/`instructions`/`timeout`/`dimensions` are read by the "
            "local-agent run path)"
        )
    for field in _RUN_STRING_OPTIONS:
        if field in opts and (
            not isinstance(opts[field], str) or not opts[field].strip()
        ):
            # Rejected before path expansion: an empty string resolves to
            # the config directory itself and blows up only on the run path.
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be a "
                "non-empty string"
            )
    rerun = opts.get("rerun", True)
    if not isinstance(rerun, bool):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.rerun` must be a boolean"
        )
    dimensions = _parse_dimensions(name, opts.get("dimensions"))
    instructions = opts.get("instructions")
    if instructions is not None:
        # Anchored to the config's own directory, so it opens from any cwd.
        expanded = Path(instructions).expanduser()
        if not expanded.is_absolute():
            expanded = config_dir / expanded
        instructions = str(expanded)
    return _build_entry(
        name,
        name=key,
        required=True,
        rerun=rerun,
        window_seconds=(
            _duration_seconds(name, "window", opts["window"])
            if "window" in opts
            else None
        ),
        model=opts.get("model"),
        instructions=instructions,
        timeout=(
            f"{_duration_seconds(name, 'timeout', opts['timeout'])}s"
            if "timeout" in opts
            else None
        ),
        dimensions=dimensions,
    )


def _parse_dimensions(name: str, value: object) -> tuple[str, ...] | None:
    """Validate `dimensions` against the closed registry; None when unset."""
    if value is None:
        return None
    known = known_dimension_names()
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(d, str) or not d.strip() for d in value)
    ):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.dimensions` must be a "
            f"non-empty array of dimension names (known: {sorted(known)}), "
            f"got {value!r}"
        )
    names = tuple(d.strip() for d in value)
    unknown = sorted(d for d in names if d not in known)
    if unknown:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.dimensions` has unknown "
            f"dimension(s) {unknown} — known dimensions: {sorted(known)}"
        )
    duplicates = sorted({d for d in names if names.count(d) > 1})
    if duplicates:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.dimensions` lists "
            f"{duplicates} more than once — list each dimension once"
        )
    return names


def _build_entry(config_name: str, **kwargs: object) -> RosterEntry:
    """Construct a :class:`RosterEntry`, translating its ``ValueError``."""
    try:
        return RosterEntry(**kwargs)  # type: ignore[arg-type]
    except ValueError as exc:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{config_name}`: {exc}"
        ) from exc


def _validate(names: tuple[str, ...]) -> None:
    """Every name must be a requestable adapter, unrepeated: a reviewer with
    no request mechanism could never satisfy the requirement."""
    requestable = {r.name for r in REGISTRY if r.requestable}
    known = {r.name for r in REGISTRY}
    lowered = [n.lower() for n in names]

    unknown = [n for n in names if n.lower() not in known]
    if unknown:
        raise RequiredReviewersConfigError(
            f"unknown required reviewer(s) {unknown} in {OVERRIDE_FILE} "
            f"`{OVERRIDE_KEY}` — known adapters: {sorted(known)}"
        )
    not_requestable = [n for n in names if n.lower() not in requestable]
    if not_requestable:
        raise RequiredReviewersConfigError(
            f"non-requestable reviewer(s) {not_requestable} cannot be required "
            f"in {OVERRIDE_FILE} `{OVERRIDE_KEY}`: a reviewer with no request "
            f"mechanism can never satisfy the requirement — requestable adapters: "
            f"{sorted(requestable)}"
        )
    duplicates = sorted({n for n in lowered if lowered.count(n) > 1})
    if duplicates:
        _reject_duplicate_names(lowered)


def _canonical_name(name: str) -> str:
    """The canonical adapter name; an unknown name passes through lowercased."""
    adapter = by_name(name)
    return adapter.name if adapter is not None else name.lower()


def _reject_duplicate_names(names: list[str]) -> None:
    """Fail loud on a repeated canonicalized name; TOML rejects only
    byte-identical keys, so a `Copilot` + `copilot` collision slips past."""
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise RequiredReviewersConfigError(
            f"duplicate required reviewer(s) {duplicates} in {OVERRIDE_FILE} "
            f"`{OVERRIDE_KEY}` — list each reviewer once "
            "(names are matched case-insensitively, so `Copilot` and `copilot` collide)"
        )


def _duration_seconds(name: str, field: str, value: object) -> int:
    """Validate a per-reviewer `timeout` / `window` into whole seconds."""
    return _duration_value(f"{OVERRIDE_KEY}.{name}.{field}", value)


def _duration_value(label: str, value: object) -> int:
    """Validate a duration into whole seconds; `label` names the bad key."""
    if isinstance(value, bool):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{label}` must be a duration "
            f"like `600s` or a positive integer of seconds, not a boolean"
        )
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        text = value.strip()
        core = text[:-1] if text.endswith("s") else text
        if not core.isdigit():
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{label}` must be a duration "
                f"like `600s` or a positive integer of seconds, got {value!r}"
            )
        seconds = int(core)
    else:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{label}` must be a duration "
            f"like `600s` or a positive integer of seconds, got {value!r}"
        )
    if seconds <= 0:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{label}` must be positive, got {value!r}"
        )
    return seconds


def _find_config(start: str | None = None) -> Path | None:
    """Search up from `start` (default cwd) for the repo-root `.shipit.toml`."""
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for d in (here, *here.parents):
        candidate = d / OVERRIDE_FILE
        if candidate.is_file():
            return candidate
    return None
