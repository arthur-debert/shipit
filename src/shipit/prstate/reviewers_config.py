"""The required-reviewer SET + per-reviewer rerun policy — config, not code.

Which reviewers GATE Ready, and whether each re-reviews every push, is policy
that changes with reviewer pricing and availability, so it must be a one-line
config edit with no code change (release#622). This module is the single place
that resolves that config:

  * `DEFAULT_REVIEWERS` — the declarative default shipped for every consumer:
    Copilot only, review-once (rerun=False). CodeRabbit is a registered,
    requestable adapter being PILOTED on the phos-org repos (where the GitHub
    App is installed) — a pilot repo opts in via the override below; requiring
    it by default would gate every other repo on an app that is not installed
    there (the request edge silently drops, #613-style, and the PR parks at
    REVIEWS_PENDING forever).
  * a per-repo OVERRIDE — the optional `[reviewers]` table in the consumer's
    `.shipit.toml` (the same policy file that already carries `[secrets]`). No
    NEW tracked consumer file.

The `[reviewers]` value is a MAP from reviewer name to an options inline-table;
the map KEYS are the required reviewers (all must be DONE to flip Ready). The
options:

  * `rerun` (bool, default **False**) — whether the reviewer re-reviews every
    new head (consumed now by the engine).
  * `model` / `instructions` — parsed + validated now but RESERVED for the
    deferred local-agent review step; they do not affect this epic's behaviour.

The `[reviewers]` value is TABLE-ONLY: a list/array form (`reviewers =
["copilot", "codex"]`) is REJECTED loud, not silently accepted. The required
set + per-reviewer options must be expressed as the table so every gate carries
its options in one place.

NEW POLICY: re-run-on-push is per-reviewer and defaults OFF for EVERYONE. All
reviewers are token-billed now (and local agents cost a real model run each
time), so re-reviewing each push is explicit opt-in, not the default.

Names map to adapters in the registry (#558); an unknown / non-requestable name
fails LOUD (`RequiredReviewersConfigError`) rather than silently dropping a
required gate.

`resolve_reviewers` takes the override as data (already parsed), keeping THIS
module pure and unit-testable; the thin `load_override` seam is the only thing
that touches the filesystem, mirroring `ghapi`/`secretsrc` boundaries. It reads
the `[reviewers]` table from `.shipit.toml` in-process via `tomllib` (no `yq`
subprocess, so no process-lifetime config cache is needed here — the engine's
own `_REQUIRED_CACHE` in `reviewers.py` still exists for the `evaluate` path).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .reviewers import REGISTRY, ReviewerAdapter, by_name

# The shipped default: Copilot required, review-once. Changing the required set
# (or a rerun flag) for ALL consumers is editing this one literal; a single
# consumer overrides it in its own `.shipit.toml` (the phos pilot repos add
# coderabbit there — see module docstring).
DEFAULT_REVIEWERS: dict[str, bool] = {"copilot": False}

# The override key + the file that carries it (the `[reviewers]` table in the
# consumer's `.shipit.toml`). Named here so the doc and the loader agree.
OVERRIDE_FILE = ".shipit.toml"
OVERRIDE_KEY = "reviewers"

# The per-reviewer options that are accepted. `rerun` is consumed now; `model`
# and `instructions` are parsed + validated but RESERVED for the deferred
# local-agent review step (an option not listed here fails loud).
_RESERVED_OPTIONS = ("model", "instructions")
_KNOWN_OPTIONS = ("rerun", *_RESERVED_OPTIONS)


class RequiredReviewersConfigError(RuntimeError):
    """The `[reviewers]` config is invalid — any of: an unknown name, a
    non-requestable reviewer in the required set, a duplicate name, a wrong-typed
    value, or an unknown per-reviewer option. One error type for the whole config
    surface; the message says which."""


def resolve_reviewers(override: dict[str, bool] | None = None) -> dict[str, bool]:
    """The required reviewers + their rerun flags: the override if given+non-empty,
    else the default.

    Pure: the caller passes the already-parsed override map (or None). An empty
    map is treated as "unset" — a consumer cannot accidentally disable ALL
    review gating by writing `reviewers = {}`; that falls back to the default.
    (Removing review gating entirely is not a config the loop offers.)
    """
    resolved = dict(override) if override else dict(DEFAULT_REVIEWERS)
    _validate(tuple(resolved))
    return resolved


def resolve_required_names(override: dict[str, bool] | None = None) -> tuple[str, ...]:
    """The required-reviewer names (map keys), in config order."""
    return tuple(resolve_reviewers(override))


def reviewer_rerun(override: dict[str, bool] | None = None) -> dict[str, bool]:
    """The per-reviewer rerun flags (name -> bool), defaulting False for any
    required reviewer that doesn't specify one."""
    return dict(resolve_reviewers(override))


def _validate(names: tuple[str, ...]) -> None:
    """A required set is valid only if every name is a REQUESTABLE adapter and
    no name repeats.

    Requestable is load-bearing: a reviewer with no request mechanism (Gemini)
    can never satisfy a required gate — the engine would forever advise
    "request gemini" while `pr review request` only no-ops. Rejecting it here,
    at parse time, turns that silent dead-end into a loud config error. A
    duplicate name is also rejected — a repeated gate is always a typo, never
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
            f"mechanism can never satisfy the gate — requestable adapters: "
            f"{sorted(requestable)}"
        )
    duplicates = sorted({n for n in lowered if lowered.count(n) > 1})
    if duplicates:
        raise RequiredReviewersConfigError(
            f"duplicate required reviewer(s) {duplicates} in {OVERRIDE_FILE} "
            f"`{OVERRIDE_KEY}` — list each reviewer once"
        )


def _parse_override_value(value: object) -> dict[str, bool]:
    """Parse the raw `reviewers` value into a name -> rerun map.

    TABLE-ONLY: the value MUST be a MAP `{copilot = {rerun = false}, codex =
    {rerun = true}}` — keys are the required reviewers, each value an options
    inline-table. A list/array form (or any other non-table) fails LOUD; the
    list shorthand a ported release behavior once accepted is gone.

    Wrong-typed values / unknown options fail LOUD. Reviewer-name keys are
    normalized to their canonical adapter name (lowercase) so the resulting map
    is keyed the SAME way the adapters read it (`ctx.reviewer_rerun.get(adapter
    .name, ...)`); a `Copilot` key therefore APPLIES its rerun flag instead of
    silently degrading to review-once (release#852). Two differently-cased keys
    that canonicalize to the same adapter (`Copilot` + `copilot`) collide → a
    loud duplicate error."""
    if not isinstance(value, dict):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` must be a TABLE of reviewer -> "
            "{{rerun = bool}} (e.g. `[reviewers]` with `copilot = {{rerun = false}}`); "
            "a list/array form is not accepted"
        )

    out: dict[str, bool] = {}
    for name, opts in value.items():
        if not isinstance(name, str):
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}` keys must be reviewer names"
            )
        key = _canonical_name(name)
        # The duplicate guard catches differently-cased keys that collide to
        # one adapter (e.g. `Copilot` + `copilot`); here we fail on first
        # overwrite so the per-reviewer options are never silently clobbered.
        if key in out:
            _reject_duplicate_names([*out, key])
        out[key] = _parse_options(name, opts)
    return out


def _canonical_name(name: str) -> str:
    """The canonical adapter name for `name` (the `--reviewer` selector / map key).

    Resolves through the SAME registry lookup the required-set validation uses
    (`by_name`, which lowercases), so a key keys the rerun map by `adapter.name`
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


def _parse_options(name: str, opts: object) -> bool:
    """Parse one reviewer's options table into its rerun flag (default False).

    A `null`/empty value (`copilot = {}` with nothing under it) means defaults —
    rerun=False. `rerun` (bool) is consumed now; `model` / `instructions`
    (strings) are validated but RESERVED for the deferred local-agent step.
    Anything other than a table, an unknown option, a non-bool `rerun`, or a
    non-string reserved field fails loud."""
    if opts is None:
        return False
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
            "(`rerun` is consumed now; `model`/`instructions` are reserved)"
        )
    # Reserved fields are parsed + validated now but not consumed in this epic.
    for field in _RESERVED_OPTIONS:
        if field in opts and not isinstance(opts[field], str):
            raise RequiredReviewersConfigError(
                f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.{field}` must be a string"
            )
    rerun = opts.get("rerun", False)
    if not isinstance(rerun, bool):
        raise RequiredReviewersConfigError(
            f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{name}.rerun` must be a boolean"
        )
    return rerun


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


def load_override(root: str | None = None) -> dict[str, bool] | None:
    """Read the `[reviewers]` table from the consumer's `.shipit.toml`.

    Returns the parsed name -> rerun map, or None when the file/table is absent
    or empty. The ONE filesystem seam in this module (an in-process `tomllib`
    read — no `yq` subprocess); everything else is pure data. `root` is the
    directory to start searching from (default cwd, walking up to the repo
    root); a missing config or a missing/empty `[reviewers]` table → None, so
    the shipped default applies."""
    config = _find_config(root)
    if config is None:
        return None
    try:
        with config.open("rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RequiredReviewersConfigError(f"malformed {config}: {exc}") from None
    value = cfg.get(OVERRIDE_KEY)
    if value is None:
        return None
    parsed = _parse_override_value(value)
    return parsed or None


def reviewer_run_options(name: str, root: str | None = None) -> dict[str, str]:
    """The per-reviewer `model` / `instructions` for `name` from `.shipit.toml`.

    Consumed by the local-agent review RUN path (PRF01-WS07): a reviewer's
    `[reviewers]` entry MAY carry `model` (the backend model alias) and
    `instructions` (a path to a custom review-instructions file). Returns a dict
    with only the keys that are set (e.g. `{"model": "flash"}`); an absent
    config, an absent reviewer entry, or a non-table `reviewers` value → `{}`
    (the run path then uses its own defaults).

    A relative `instructions` path is resolved against the directory CONTAINING
    `.shipit.toml` (and `~` is expanded), not the caller's cwd: the config is
    discovered by walking UP from cwd, so a caller in a nested subdir would
    otherwise resolve a repo-relative `instructions` path against the wrong
    directory and fail to open it. The returned path is absolute.

    Reading `model`/`instructions` is NOT gating: a reviewer requested manually
    via `--reviewer codex-local` (force scope) reads its options here WITHOUT
    being in the required set — so a consumer can tune a local reviewer's model
    without making it a required gate (see the PRD's reviewer-policy note).
    """
    config = _find_config(root)
    if config is None:
        return {}
    try:
        with config.open("rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RequiredReviewersConfigError(f"malformed {config}: {exc}") from None
    value = cfg.get(OVERRIDE_KEY)
    if not isinstance(value, dict):
        # A non-table `reviewers` value (or absent) carries no per-reviewer
        # options. The gating path rejects it loud; this read just no-ops.
        return {}
    canonical = _canonical_name(name)
    out: dict[str, str] = {}
    for key, opts in value.items():
        if _canonical_name(key) != canonical or not isinstance(opts, dict):
            continue
        for field in _RESERVED_OPTIONS:
            if field in opts:
                if not isinstance(opts[field], str):
                    raise RequiredReviewersConfigError(
                        f"{OVERRIDE_FILE} `{OVERRIDE_KEY}.{key}.{field}` must be a string"
                    )
                out[field] = opts[field]
    if "instructions" in out:
        # Anchor a relative instructions path to the config's own directory (and
        # expand ~), so it opens regardless of the caller's cwd.
        expanded = Path(out["instructions"]).expanduser()
        if not expanded.is_absolute():
            expanded = config.parent / expanded
        out["instructions"] = str(expanded)
    return out


def required_reviewers(names: tuple[str, ...]) -> list[ReviewerAdapter]:
    """Map required names → their registry adapters, preserving config order.

    `_validate` guarantees every name resolves, so `by_name` never returns None
    here; the explicit guard turns any future registry/validation mismatch into a
    loud error instead of a None leaking to callers (keeps the return type a
    clean `list[ReviewerAdapter]`)."""
    _validate(names)
    adapters: list[ReviewerAdapter] = []
    for n in names:
        adapter = by_name(n)
        if adapter is None:  # unreachable post-_validate — fail loud if it isn't
            raise RequiredReviewersConfigError(
                f"required reviewer {n!r} has no adapter after validation"
            )
        adapters.append(adapter)
    return adapters
