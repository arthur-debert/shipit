"""Roster — reviewer configuration as ONE value (CLI01-WS04, ADR-0030).

The resolved, validated reviewer configuration, read ONCE at a verb boundary
(`reviewers_config.load_roster`) and passed along as a value — never re-resolved
mid-flow. It replaces the three parallel string-keyed dict resolvers (required,
rerun, wait window, run options) and both module-global caches, discharging
ADR-0021 rule 4 for its named example: per-reviewer settings can no longer
disagree with each other because they travel together on one frozen value.

The Roster is *configuration about* reviewers; reviewer *identity* stays with
the Backend / reviewer-adapter registries (per the CONTEXT.md avoid-list there
is deliberately NO new Reviewer identity object). Entries are keyed by reviewer
name — the canonical lowercase adapter/wire name.

Construction is validation: a `RosterEntry`/`Roster` that constructs is
well-formed (canonical name, positive window, canonical `<N>s` timeout, no
duplicate entries). Config-shape errors (an unknown reviewer, a wrong-typed
option) are the LOADER's job — they fail loud at `load_roster` with the precise
config-file message; these values only defend their own invariants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The canonical duration shape a per-reviewer ``timeout`` carries: whole seconds
#: with the ``s`` suffix (e.g. ``600s``) — exactly what the local-agent run path
#: passes to the agent CLI. The loader normalizes config input to this; a value
#: constructed directly must already be canonical.
_TIMEOUT_SHAPE = re.compile(r"^[1-9][0-9]*s$")


@dataclass(frozen=True)
class RosterEntry:
    """One reviewer's resolved settings — a row of the :class:`Roster`.

    ``name`` is the canonical lowercase adapter name (the wire string the
    engine's context maps are keyed by). ``required`` is whether this reviewer
    holds Ready; ``rerun`` whether it re-reviews every push (default OFF —
    review-once); ``window_seconds`` the per-reviewer readiness wait window
    (``None`` → the engine's shipped default); ``model`` / ``instructions`` /
    ``timeout`` the local-agent RUN options (``None`` → the run path's own
    defaults). An UNCONFIGURED reviewer is exactly the field defaults with its
    name — which is why :meth:`Roster.entry` can be total.
    """

    name: str
    required: bool = False
    rerun: bool = False
    window_seconds: int | None = None
    model: str | None = None
    instructions: str | None = None
    timeout: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("RosterEntry.name must be a non-empty reviewer name")
        if self.name != self.name.lower():
            raise ValueError(
                f"RosterEntry.name must be the canonical lowercase reviewer name, "
                f"got {self.name!r}"
            )
        for flag in ("required", "rerun"):
            if not isinstance(getattr(self, flag), bool):
                raise ValueError(f"RosterEntry.{flag} must be a bool")
        if self.window_seconds is not None and (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, int)
            or self.window_seconds <= 0
        ):
            raise ValueError(
                f"RosterEntry.window_seconds must be a positive int of seconds, "
                f"got {self.window_seconds!r}"
            )
        for option in ("model", "instructions"):
            value = getattr(self, option)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"RosterEntry.{option} must be a non-empty string")
        if self.timeout is not None and (
            not isinstance(self.timeout, str) or not _TIMEOUT_SHAPE.match(self.timeout)
        ):
            raise ValueError(
                f"RosterEntry.timeout must be a canonical `<N>s` duration "
                f"(e.g. '600s'), got {self.timeout!r}"
            )


@dataclass(frozen=True)
class Roster:
    """Every configured reviewer's settings as one frozen value.

    Built once at a verb boundary by ``reviewers_config.load_roster`` and passed
    down — onto the ``ReadinessView`` for the engine/adapters, into the request
    path for run options — so no call path resolves reviewer settings twice and
    no module-global cache exists to reset in tests. The EMPTY roster (the
    dataclass default) is the honest fixture default: no reviewer required,
    every per-reviewer setting at its shipped default.
    """

    entries: tuple[RosterEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(e, RosterEntry) for e in self.entries
        ):
            raise ValueError("Roster.entries must be a tuple of RosterEntry values")
        names = [e.name for e in self.entries]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"Roster has duplicate reviewer entries {duplicates} — "
                "one entry per reviewer"
            )

    def entry(self, name: str) -> RosterEntry:
        """The settings for reviewer `name` — TOTAL, never None.

        A configured reviewer returns its entry; an unconfigured one returns the
        all-defaults entry (not required, review-once, shipped window, no run
        options), so every consumer reads settings the same way instead of
        re-rolling a `.get(name, default)` per setting. Matching is by canonical
        lowercase name, the same normalization the loader applies to keys.
        """
        key = name.lower()
        for e in self.entries:
            if e.name == key:
                return e
        return RosterEntry(name=key)

    @property
    def required(self) -> tuple[RosterEntry, ...]:
        """The required entries (the reviewers that hold Ready), config order."""
        return tuple(e for e in self.entries if e.required)

    @property
    def required_names(self) -> tuple[str, ...]:
        """The required reviewers' names, config order."""
        return tuple(e.name for e in self.required)
