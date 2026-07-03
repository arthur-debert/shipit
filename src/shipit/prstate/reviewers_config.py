"""The ONE boundary loader for reviewer configuration — config in, Roster out.

Which reviewers BLOCK Ready, and how each behaves (re-review on push, wait
window, local-run options), is policy that changes with reviewer pricing and
availability, so it must be a one-line config edit with no code change
(release#622). This module is the single place that resolves that config:

  * `DEFAULT_REVIEWERS` — the declarative default shipped for every consumer:
    Copilot only, review-once (rerun=False). CodeRabbit is a registered,
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

  * `rerun` (bool, default **False**) — whether the reviewer re-reviews every
    new head (consumed by the engine). All reviewers are token-billed (and
    local agents cost a real model run each time), so re-reviewing each push is
    explicit opt-in, not the default.
  * `window` (duration, default **20m**) — the per-reviewer readiness wait
    window (OBS04-WS03): how long the engine waits for an in-flight review to
    ARRIVE before ageing it to *timed-out* → settled.
  * `timeout` (duration) — the agent-execution cap on a local review's model
    RUN; distinct from `window` (arrival deadline vs run cap). Consumed by the
    local-agent review path, not the engine.
  * `model` / `instructions` — free-form strings consumed by the local-agent
    review RUN path; they do not affect the engine verdict.

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
    the required-reviewer default (ADR-0025). Each default reviewer becomes a
    `name = { rerun = <bool> }` entry when it sets rerun, else the empty-options
    `name = {}` (rerun defaults off). Rendering the map VALUES (not just its keys) keeps
    the scaffold truly single-sourced: if a future default flips a reviewer to
    `rerun = true`, the seeded `.shipit.toml` tracks it instead of silently diverging
    from the engine default (which reads the same map). Because both the engine default
    and the seeded config come from this one map, a freshly-installed repo requires
    exactly what a repo with no config does — the code-default vs install-scaffold
    disagreement is gone."""
    lines = [
        f"{name} = {{ rerun = true }}" if rerun else f"{name} = {{}}"
        for name, rerun in DEFAULT_REVIEWERS.items()
    ]
    return "[reviewers]\n" + "\n".join(lines) + "\n"


# The override key + the file that carries it (the `[reviewers]` table in the
# consumer's `.shipit.toml`). Named here so the doc and the loader agree.
OVERRIDE_FILE = ".shipit.toml"
OVERRIDE_KEY = "reviewers"

# The per-reviewer options that are accepted (see the module docstring for what
# each means). An option not listed here fails loud.
_RUN_STRING_OPTIONS = ("model", "instructions")
_KNOWN_OPTIONS = ("rerun", "timeout", "window", *_RUN_STRING_OPTIONS)


class RequiredReviewersConfigError(RuntimeError):
    """The `[reviewers]` config is invalid — any of: an unknown name, a
    non-requestable reviewer in the required set, a duplicate name, a wrong-typed
    value, or an unknown per-reviewer option. One error type for the whole config
    surface; the message says which."""


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
    entirely is not a config the loop offers). Everything invalid — malformed
    TOML, a non-table value, an unknown reviewer/option, a wrong-typed or
    non-positive duration, a duplicate (case-colliding) name — raises
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
    if not entries:
        return default_roster()
    _validate(tuple(e.name for e in entries))
    return Roster(tuple(entries))


def _parse_table(value: object, *, config_dir: Path) -> list[RosterEntry]:
    """Parse the raw `reviewers` value into RosterEntry values, config order.

    TABLE-ONLY: the value MUST be a MAP `{copilot = {rerun = false}, codex =
    {rerun = true}}` — keys are the required reviewers, each value an options
    inline-table. A list/array form (or any other non-table) fails LOUD; the
    list shorthand (a ported release behavior once accepted) is gone.

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
        if not isinstance(name, str):
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` keys must be reviewer names"
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
        return RosterEntry(name=key, required=True)
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
            "`model`/`instructions`/`timeout` are read by the local-agent run path)"
        )
    for field in _RUN_STRING_OPTIONS:
        if field in opts and not isinstance(opts[field], str):
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be a string"
            )
    rerun = opts.get("rerun", False)
    if not isinstance(rerun, bool):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.rerun` must be a boolean"
        )
    instructions = opts.get("instructions")
    if instructions is not None:
        # Anchor a relative instructions path to the config's own directory (and
        # expand ~), so it opens regardless of the caller's cwd.
        expanded = Path(instructions).expanduser()
        if not expanded.is_absolute():
            expanded = config_dir / expanded
        instructions = str(expanded)
    return RosterEntry(
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
    )


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
    """Validate a per-reviewer duration option (`timeout` / `window`) → whole seconds.

    Accepts a positive integer (seconds) or a string of digits optionally suffixed
    with `s` (e.g. `600` or `600s`). A bool, a non-positive value, or any other
    shape fails LOUD — a bad duration is a config error, never a silent default.
    `bool` is an `int` subclass, so it is rejected explicitly (a `window = true` is
    never "1 second"). `field` names the offending option in the error so the same
    core serves both `timeout` and `window`."""
    if isinstance(value, bool):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be a duration "
            f"like `600s` or a positive integer of seconds, not a boolean"
        )
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        text = value.strip()
        core = text[:-1] if text.endswith("s") else text
        if not core.isdigit():
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be a duration "
                f"like `600s` or a positive integer of seconds, got {value!r}"
            )
        seconds = int(core)
    else:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be a duration "
            f"like `600s` or a positive integer of seconds, got {value!r}"
        )
    if seconds <= 0:
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be positive, "
            f"got {value!r}"
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
