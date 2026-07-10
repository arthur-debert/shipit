"""The ONE boundary loader for reviewer configuration — config in, Roster out.

Which reviewers BLOCK Ready, and how each behaves (re-review on push, wait
window, local-run options), is policy that changes with reviewer pricing and
availability, so it must be a one-line config edit with no code change
(release#622). This module is the single place that resolves that config:

  * `DEFAULT_REVIEWERS` — the declarative default shipped for every consumer:
    Copilot only, and EXPLICITLY review-once (rerun=False) — Copilot is a
    full-diff app reviewer on a metered plan, exactly the ADR-0043 opt-out case,
    so it keeps review-once even though the CODE default flipped to head-strict.
    CodeRabbit is a registered,
    requestable adapter being PILOTED on the phos-org repos (where the GitHub
    App is installed) — a pilot repo opts in via the override below; requiring
    it by default would block every other repo on an app that is not installed
    there (the request edge silently drops, #613-style, and the PR parks at
    REVIEWS_PENDING forever).
  * a per-repo OVERRIDE — the optional `[reviewers]` table in the consumer's
    `.shipit.toml` (the same policy file that already carries `[secrets]`). No
    NEW tracked consumer file.

`load_roster()` is the boundary (CLI01-WS04): it reads the config ONCE and
returns a validated :class:`~.roster.Roster` — every configured reviewer's
settings as one frozen value. Verbs call it once per invocation and pass the
Roster down (onto the `ReadinessView` for the engine, into the request path for
run options). There is deliberately NO module-global cache and NO per-setting
dict resolver anymore: settings travel together on the value, so they cannot
disagree, and tests construct Rosters directly instead of resetting a cache
(discharging ADR-0021 rule 4 for its named example).

The `[reviewers]` value is a MAP from reviewer name to an options inline-table;
the map KEYS are the required reviewers (all must be DONE to flip Ready). The
options:

  * `rerun` (bool, default **True** — head-strict) — whether the reviewer
    re-reviews every new head (consumed by the engine). ADR-0043 flipped the
    code default to head-strict now that a round after the first reviews only
    the cheap fix range (RVW02-WS06): every push re-stales the reviewer and the
    engine re-requests it, so the commits addressing a review are themselves
    reviewed. Review-once (`rerun = false`) is an explicit per-reviewer opt-out
    for reviewers whose re-runs stay expensive — full-diff app reviewers on
    metered plans (Copilot is exactly this, and the shipped default set keeps it
    review-once via `DEFAULT_REVIEWERS`).
  * `window` (duration, default **20m**) — the per-reviewer readiness wait
    window (OBS04-WS03): how long the engine waits for an in-flight review to
    ARRIVE before ageing it to *timed-out* → settled.
  * `timeout` (duration) — the agent-execution cap on a local review's model
    RUN; distinct from `window` (arrival deadline vs run cap). Consumed by the
    local-agent review path, not the engine.
  * `model` / `instructions` — free-form strings consumed by the local-agent
    review RUN path; they do not affect the engine verdict.
  * `dimensions` (array of dimension names, RVW02-WS04) — the local-agent
    reviewer's **Dimension pass** set, riding the same seam as
    `model`/`instructions`. Unset means the shipped default set; names
    validate against the closed registry
    (:func:`shipit.review.dimensions.known_dimension_names`) — an unknown
    dimension fails LOUD with the known set, roster prior art.

Four table-level keys are RESERVED (policy riding the same table, NOT reviewer
entries):

  * `round_cap` — the review-round budget the stopping rule enforces (how
    many review rounds may happen before the engine stops opening another
    round). Unset means the shipped default (`breakers.ROUND_CAP`, 6); a
    non-int or < 1 value fails loud at parse. It lands on `Roster.round_cap`,
    so it travels with the reviewer configuration as part of the ONE boundary
    value.
  * `poll_interval` — the fixed cadence `shipit pr wait` re-polls the
    evaluator at (ADR-0034): a duration (`90s`) or a positive integer of
    seconds. Tool-owned config, deliberately NOT a per-call flag. Unset means
    the shipped default (`wait.POLL_INTERVAL_SECONDS`, 60s). It lands on
    `Roster.poll_interval`.
  * `nit_cap` (RVW02-WS04) — the round-1 nit budget the fan-out routing
    enforces on the POSTED review: a non-negative int (`0` = floor at minor —
    no nit posts). Unset means uncapped. Lands on `Roster.nit_cap`.
  * `calibrator` (RVW02-WS04) — the ONE fixed judge config every local
    reviewer's fan-out shares (ADR-0045: table-level on purpose — a
    per-reviewer calibrator would fork the common severity ruler): an
    inline-table of `backend` / `model` / `reasoning` / `timeout`, e.g.
    `calibrator = { backend = "claude", reasoning = "high" }`. The calibrator
    is a DORMANT stage, OFF by default (RVW02-WS08, #669: the WS05/F2 baseline
    found the LLM judge net-negative on round-1 major recall) — UNSET means the
    round-1 default of the mechanically-deduped union; SETTING this inline-table
    opts the judge back on (its own default is `claude` at high ReasoningLevel).
    Unknown keys and invalid values fail LOUD. Lands on `Roster.calibrator` as a
    validated :class:`~shipit.review.calibrator.CalibratorConfig` (or `None` when
    unset).

The `[reviewers]` value is TABLE-ONLY: a list/array form (`reviewers =
["copilot", "codex"]`) is REJECTED loud, not silently accepted. The required
set + per-reviewer options must be expressed as the table so every required
reviewer carries its options in one place.

Names map to adapters in the registry (#558); an unknown / non-requestable name
fails LOUD (`RequiredReviewersConfigError`) rather than silently dropping a
required reviewer. Unknown options and wrong-typed values fail loud too — the
whole config surface is rejected at LOAD, so a Roster in hand is always valid.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ..review.calibrator import CalibratorConfig
from ..review.dimensions import known_dimension_names
from .errors import PrStateError
from .reviewers import REGISTRY, by_name
from .roster import Roster, RosterEntry

# The shipped default: Copilot required, review-once. This is the SINGLE
# required-reviewer default (ADR-0025 / COR01-WS02): the install scaffold that seeds
# a consumer's `[reviewers]` table (`shipit.config`) RENDERS its body from this map
# (see `default_reviewers_scaffold_body`), so the code-default and the install-scaffold
# can never disagree — a consumer with no `.shipit.toml` and a consumer that just ran
# `shipit install` both require exactly Copilot. codex/agy are deliberately NOT in the
# default set: their review GitHub Apps are not installed on an arbitrary consumer repo,
# so requiring them by default would silently park every PR at REVIEWS_PENDING. A repo
# that HAS the Apps opts them in via its own `.shipit.toml` `[reviewers]` (shipit's own
# repo does exactly this). Changing the required set (or a rerun flag) for ALL consumers
# is editing this one literal.
DEFAULT_REVIEWERS: dict[str, bool] = {"copilot": False}


def default_reviewers_scaffold_body() -> str:
    """The `[reviewers]` TOML table body the install scaffold seeds when a consumer
    has none — rendered FAITHFULLY from :data:`DEFAULT_REVIEWERS`, the SINGLE source of
    the required-reviewer default (ADR-0025). Each default reviewer becomes an EXPLICIT
    `name = { rerun = <true|false> }` entry — the rerun flag is ALWAYS written, never
    left to an empty `name = {}`. This matters since ADR-0043 flipped the code default
    to head-strict (RVW02-WS06): an omitted rerun now parses as `true`, so a `{}` entry
    could no longer faithfully carry a review-once default (Copilot's) — the seeded
    config would silently diverge from `DEFAULT_REVIEWERS`. Writing the flag verbatim
    keeps the scaffold single-sourced against the map VALUES regardless of what the code
    default is, so a freshly-installed repo requires exactly what a repo with no config
    does — the code-default vs install-scaffold disagreement stays gone."""
    lines = [
        f"{name} = {{ rerun = {'true' if rerun else 'false'} }}"
        for name, rerun in DEFAULT_REVIEWERS.items()
    ]
    return "[reviewers]\n" + "\n".join(lines) + "\n"


# The override key + the file that carries it (the `[reviewers]` table in the
# consumer's `.shipit.toml`). Named here so the doc and the loader agree.
OVERRIDE_FILE = ".shipit.toml"
OVERRIDE_KEY = "reviewers"

# Reserved TABLE-LEVEL keys in `[reviewers]`: policy values riding the table,
# not reviewer entries. The entry parser skips them; each has its own parser.
ROUND_CAP_KEY = "round_cap"
POLL_INTERVAL_KEY = "poll_interval"
NIT_CAP_KEY = "nit_cap"
CALIBRATOR_KEY = "calibrator"
_RESERVED_KEYS = (ROUND_CAP_KEY, POLL_INTERVAL_KEY, NIT_CAP_KEY, CALIBRATOR_KEY)

# The `calibrator` inline-table's accepted keys — the CalibratorConfig fields
# (RVW02-WS04). An unknown key fails loud, exactly like a per-reviewer option.
_CALIBRATOR_OPTIONS = ("backend", "model", "reasoning", "timeout")

# The per-reviewer options that are accepted (see the module docstring for what
# each means). An option not listed here fails loud.
_RUN_STRING_OPTIONS = ("model", "instructions")
_KNOWN_OPTIONS = ("rerun", "timeout", "window", "dimensions", *_RUN_STRING_OPTIONS)


class RequiredReviewersConfigError(PrStateError):
    """The `[reviewers]` config is invalid — any of: an unknown name, a
    non-requestable reviewer in the required set, a duplicate name, a wrong-typed
    value, or an unknown per-reviewer option. One error type for the whole config
    surface; the message says which.

    A :class:`~.errors.PrStateError`: a bad `.shipit.toml` is a user-renderable
    engine failure, so the `pr` verbs that catch `(ExecError, PrStateError)`
    report it as a clean `error: …` line instead of a traceback — the config
    surface fails loud the same way a bad `gh`/GraphQL payload does."""


def default_roster() -> Roster:
    """The shipped-default :class:`Roster`, rendered from :data:`DEFAULT_REVIEWERS`
    — what a consumer with no `.shipit.toml` (or no/empty `[reviewers]` table)
    gets: exactly the default required set, all other settings at their defaults."""
    return Roster(
        tuple(
            RosterEntry(name=name, required=True, rerun=rerun)
            for name, rerun in DEFAULT_REVIEWERS.items()
        )
    )


def load_roster(root: str | None = None) -> Roster:
    """Read the `[reviewers]` table from the consumer's `.shipit.toml` into ONE
    validated :class:`Roster` — the boundary read (CLI01-WS04).

    The ONE filesystem seam in this module (an in-process `tomllib` read); a verb
    calls this ONCE per invocation and passes the Roster down as a value, so no
    call path resolves reviewer settings twice. `root` is the directory to start
    searching from (default cwd, walking up to the repo root).

    A missing config, a missing `[reviewers]` table, or an empty table → the
    shipped :func:`default_roster` — a consumer cannot accidentally disable ALL
    review enforcement by writing `reviewers = {}` (removing review enforcement
    entirely is not a config the loop offers). The reserved table-level
    `round_cap` key (the review-round budget) is parsed HERE too and lands on
    :attr:`Roster.round_cap` — it applies even when no reviewer entry is set.
    Everything invalid — malformed TOML, a non-table value, an unknown
    reviewer/option, a wrong-typed or non-positive duration or `round_cap`, a
    duplicate (case-colliding) name — raises
    :class:`RequiredReviewersConfigError` HERE, at load: a Roster in hand is
    always valid.
    """
    config = _find_config(root)
    if config is None:
        return default_roster()
    try:
        with config.open("rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RequiredReviewersConfigError(f"malformed {config}: {exc}") from None
    value = cfg.get(OVERRIDE_KEY)
    if value is None:
        return default_roster()
    entries = _parse_table(value, config_dir=config.parent)
    # `_parse_table` has already established `value` is a table; the reserved
    # table-level policy keys ride that same table (they are NOT reviewer entries).
    round_cap = _parse_round_cap(value)
    poll_interval = _parse_poll_interval(value)
    nit_cap = _parse_nit_cap(value)
    calibrator = _parse_calibrator(value)
    if not entries:
        # No reviewer entries → the shipped default required set. The table-level
        # policy keys still apply: policy can be set without opting out of the
        # default reviewers.
        return Roster(
            default_roster().entries,
            round_cap=round_cap,
            poll_interval=poll_interval,
            nit_cap=nit_cap,
            calibrator=calibrator,
        )
    _validate(tuple(e.name for e in entries))
    try:
        return Roster(
            tuple(entries),
            round_cap=round_cap,
            poll_interval=poll_interval,
            nit_cap=nit_cap,
            calibrator=calibrator,
        )
    except ValueError as exc:
        # Belt-and-suspenders: `_validate` already rejects duplicate names, but
        # any Roster invariant that still trips must fail loud AS a config error
        # (with the file path), not a raw ValueError escaping into a traceback.
        raise RequiredReviewersConfigError(f"{config}: {exc}") from exc


def _parse_table(value: object, *, config_dir: Path) -> list[RosterEntry]:
    """Parse the raw `reviewers` value into RosterEntry values, config order.

    TABLE-ONLY: the value MUST be a MAP `{copilot = {rerun = false}, codex =
    {rerun = true}}` — keys are the required reviewers, each value an options
    inline-table. A list/array form (or any other non-table) fails LOUD; the
    list shorthand (a ported release behavior once accepted) is gone. Reserved
    table-level keys (`_RESERVED_KEYS`, e.g. `round_cap`) are SKIPPED here —
    they are policy values, not reviewer entries, parsed by their own parsers.

    Wrong-typed values / unknown options fail LOUD. Reviewer-name keys are
    normalized to their canonical adapter name (lowercase) so the resulting
    entries are keyed the SAME way the adapters read them off the context; a
    `Copilot` key therefore APPLIES its settings instead of silently degrading
    to the defaults (release#852). Two differently-cased keys that canonicalize
    to the same adapter (`Copilot` + `copilot`) collide → a loud duplicate
    error. `config_dir` anchors a relative `instructions` path (see
    `_parse_entry`)."""
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
            # Table-level POLICY keys (e.g. `round_cap`) ride the same table but
            # are not reviewer entries — each has its own parser in `load_roster`.
            continue
        if not isinstance(name, str):
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` keys must be reviewer names"
            )
        if name.lower() in _RESERVED_KEYS:
            # A case-variant of a reserved policy key (`Round_Cap`) is never a
            # reviewer name. Reserved keys are exact-lowercase like every other
            # config key (`rerun`, `window`); only reviewer NAMES canonicalize
            # case-insensitively (they resolve to adapter names). Rejecting the
            # variant HERE fails loud with the right diagnosis instead of the
            # misleading unknown-reviewer error — and guarantees the reserved-key
            # parsers (exact-key lookups, e.g. `_parse_round_cap`) can never
            # silently MISS a policy value the user meant to set.
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` reserved key must be spelled "
                f"exactly `{name.lower()}`, got {name!r}"
            )
        key = _canonical_name(name)
        # The duplicate guard catches differently-cased keys that collide to
        # one adapter (e.g. `Copilot` + `copilot`); fail on first overwrite so
        # per-reviewer options are never silently clobbered.
        if key in seen:
            _reject_duplicate_names([*seen, key])
        seen.append(key)
        entries.append(_parse_entry(name, key, opts, config_dir=config_dir))
    return entries


def _parse_round_cap(table: dict[str, object]) -> int | None:
    """Parse the reserved table-level `round_cap` key — the review-round budget.

    Absent → ``None`` (the engine's shipped default, ``breakers.ROUND_CAP``).
    A bool, a non-int, or anything < 1 fails LOUD at parse — a bad budget is a
    config error, never a silent default. ``bool`` is an ``int`` subclass, so it
    is rejected explicitly (a ``round_cap = true`` is never "1 round"). The
    lookup is EXACT-key on purpose: a case-variant spelling (`Round_Cap`) has
    already been rejected loud by `_parse_table` (which always runs first), so
    an absent exact key here really means "unset", never a missed variant."""
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
    """Parse the reserved table-level `nit_cap` key — the round-1 nit budget
    (RVW02-WS04).

    Absent → ``None`` (uncapped, the shipped default). ``0`` is LEGAL and
    meaningful (floor the posted review at minor — no nit posts); a bool, a
    non-int, or a negative value fails LOUD at parse. Exact-key lookup on
    purpose, same reasoning as `_parse_round_cap` (a case-variant spelling has
    already been rejected loud by `_parse_table`)."""
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
    """Parse the reserved table-level `calibrator` key — the ONE fixed judge
    config (RVW02-WS04, ADR-0045).

    Absent → ``None`` (the run path's shipped default: `claude` at high
    ReasoningLevel). Present, it must be an inline-table whose keys are a
    subset of `_CALIBRATOR_OPTIONS`, each a non-empty string (`timeout` also
    accepts a bare positive integer of seconds, normalized to the canonical
    `<N>s` like the per-reviewer durations). Unknown keys and any value
    :class:`~shipit.review.calibrator.CalibratorConfig` rejects
    (an unregistered backend, a bad reasoning level) fail LOUD as config
    errors — the whole surface is rejected at LOAD, so a Roster in hand always
    carries a valid calibrator."""
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
    """Parse the reserved table-level `poll_interval` key — `pr wait`'s cadence
    (ADR-0034).

    Absent → ``None`` (the waiter's shipped default,
    ``wait.POLL_INTERVAL_SECONDS``). Accepts the same duration shape as the
    per-reviewer `window`/`timeout` options (`90s` or a positive integer of
    seconds); anything else fails LOUD at parse — a bad cadence is a config
    error, never a silent default. Exact-key lookup on purpose, same reasoning
    as `_parse_round_cap` (a case-variant spelling has already been rejected
    loud by `_parse_table`)."""
    value = table.get(POLL_INTERVAL_KEY)
    if value is None:
        return None
    return _duration_value(f"{OVERRIDE_KEY}.{POLL_INTERVAL_KEY}", value)


def _parse_entry(name: str, key: str, opts: object, *, config_dir: Path) -> RosterEntry:
    """Parse one reviewer's options table into its :class:`RosterEntry`.

    A `null`/empty value (`copilot = {}` with nothing under it) means defaults —
    required, review-once, shipped window, no run options. Anything other than a
    table, an unknown option, a non-bool `rerun`, a non-string `model` /
    `instructions`, or a malformed `timeout` / `window` duration fails loud —
    at config-parse time, not on the run/readiness path.

    A relative `instructions` path is resolved against `config_dir` — the
    directory CONTAINING `.shipit.toml` (and `~` is expanded), not the caller's
    cwd: the config is discovered by walking UP from cwd, so a caller in a
    nested subdir would otherwise resolve a repo-relative `instructions` path
    against the wrong directory and fail to open it. The entry carries the
    absolute path."""
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
            # Reject empty/whitespace HERE, before `instructions` path expansion:
            # an empty string is a non-empty PATH once resolved against config_dir
            # (`Path("").expanduser()` → `.` → the config directory itself), so it
            # would slip past RosterEntry's non-empty guard and only blow up later
            # as an IsADirectoryError on the run path. An empty `model` likewise
            # must fail loud as a config error, not a raw RosterEntry ValueError.
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
        # Anchor a relative instructions path to the config's own directory (and
        # expand ~), so it opens regardless of the caller's cwd.
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
    """Validate one reviewer's `dimensions` option (RVW02-WS04) → the canonical
    tuple, or ``None`` when unset (the shipped default set).

    An array of dimension names from the CLOSED registry
    (:func:`shipit.review.dimensions.known_dimension_names`); an unknown name,
    a non-array shape, an empty array, a non-string element, or a duplicate
    fails LOUD with the known set — the same posture as an unknown reviewer
    name (roster prior art)."""
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
    """Construct a :class:`RosterEntry`, translating its construction-is-validation
    ``ValueError`` into a :class:`RequiredReviewersConfigError`.

    The loader validates every field before it gets here, so this is the
    defense-in-depth boundary: if a value still trips a RosterEntry invariant
    (`__post_init__`), the failure surfaces as a config error naming the
    offending reviewer — never a raw ``ValueError`` escaping the domain-error
    boundary into an unhandled CLI traceback."""
    try:
        return RosterEntry(**kwargs)  # type: ignore[arg-type]
    except ValueError as exc:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{config_name}`: {exc}"
        ) from exc


def _validate(names: tuple[str, ...]) -> None:
    """A required set is valid only if every name is a REQUESTABLE adapter and
    no name repeats.

    Requestable is load-bearing: a reviewer with no request mechanism (Gemini)
    can never satisfy a required reviewer — the engine would forever advise
    "request gemini" while `pr review request` only no-ops. Rejecting it here,
    at parse time, turns that silent dead-end into a loud config error. A
    duplicate name is also rejected — a repeated requirement is always a typo, never
    intent."""
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
    """The canonical adapter name for `name` (the `--reviewer` selector / entry key).

    Resolves through the SAME registry lookup the required-set validation uses
    (`by_name`, which lowercases), so an entry is keyed by `adapter.name`
    (lowercase) — exactly what the adapters read off the context. An unknown
    name has no adapter to canonicalize to; it is passed through lowercased so
    `_validate` raises the precise unknown-reviewer error (with the known set)
    rather than this seam swallowing it."""
    adapter = by_name(name)
    return adapter.name if adapter is not None else name.lower()


def _reject_duplicate_names(names: list[str]) -> None:
    """Fail LOUD on any reviewer name that appears more than once (post-canon).

    Run on canonicalized table keys: the map can collide two differently-cased
    keys onto one adapter (`Copilot` + `copilot`). TOML's own duplicate-key
    rejection only covers byte-identical map keys, so the collision case slips
    past it — this is the check that catches it."""
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise RequiredReviewersConfigError(
            f"duplicate required reviewer(s) {duplicates} in {OVERRIDE_FILE} "
            f"`{OVERRIDE_KEY}` — list each reviewer once "
            "(names are matched case-insensitively, so `Copilot` and `copilot` collide)"
        )


def _duration_seconds(name: str, field: str, value: object) -> int:
    """Validate a per-reviewer duration option (`timeout` / `window`) → whole
    seconds, via the shared duration core with the `reviewers.<name>.<field>`
    label naming the offending option."""
    return _duration_value(f"{OVERRIDE_KEY}.{name}.{field}", value)


def _duration_value(label: str, value: object) -> int:
    """The ONE duration-validation core for the `[reviewers]` config surface →
    whole seconds.

    Accepts a positive integer (seconds) or a string of digits optionally suffixed
    with `s` (e.g. `600` or `600s`). A bool, a non-positive value, or any other
    shape fails LOUD — a bad duration is a config error, never a silent default.
    `bool` is an `int` subclass, so it is rejected explicitly (a `window = true` is
    never "1 second"). `label` names the offending key in the error so the same
    core serves the per-reviewer options (`timeout` / `window`) and the
    table-level `poll_interval`."""
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
    """Search up from `start` (default cwd) for the repo-root `.shipit.toml`.

    Returns the first `.shipit.toml` found walking parent directories, or None
    if none exists up to the filesystem root — the same upward-search shape the
    rest of shipit's policy reads use."""
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for d in (here, *here.parents):
        candidate = d / OVERRIDE_FILE
        if candidate.is_file():
            return candidate
    return None
