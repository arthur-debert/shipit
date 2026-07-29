"""Roster — reviewer configuration as one frozen value, read once at a verb
boundary and passed along. Construction is validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..review.calibrator import CalibratorConfig

#: Whole seconds with the ``s`` suffix, as the agent CLI takes it.
_TIMEOUT_SHAPE = re.compile(r"^[1-9][0-9]*s$")


@dataclass(frozen=True)
class RosterEntry:
    """One reviewer's resolved settings; ``None`` means the shipped default
    throughout, so an unconfigured reviewer is exactly the field defaults
    with its name."""

    name: str
    required: bool = False
    rerun: bool = True
    window_seconds: int | None = None
    model: str | None = None
    instructions: str | None = None
    timeout: str | None = None
    dimensions: tuple[str, ...] | None = None

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
        if self.dimensions is not None and (
            not isinstance(self.dimensions, tuple)
            or not self.dimensions
            or any(not isinstance(d, str) or not d for d in self.dimensions)
        ):
            # Shape only: membership in the closed registry is the loader's.
            raise ValueError(
                f"RosterEntry.dimensions must be a non-empty tuple of dimension "
                f"names, got {self.dimensions!r}"
            )


@dataclass(frozen=True)
class ReviewPolicy:
    """The shared calibrator config and the round-1 nit cap; ``None`` means
    uncapped and judge-off respectively."""

    calibrator: CalibratorConfig | None = None
    nit_cap: int | None = None


@dataclass(frozen=True)
class Roster:
    """Every configured reviewer's settings as one frozen value; the
    table-level policy values ride alongside the entries, and the calibrator
    is table-level so no per-reviewer one forks the common severity ruler."""

    entries: tuple[RosterEntry, ...] = ()
    round_cap: int | None = None
    poll_interval: int | None = None
    nit_cap: int | None = None
    calibrator: CalibratorConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(e, RosterEntry) for e in self.entries
        ):
            raise ValueError("Roster.entries must be a tuple of RosterEntry values")
        if self.round_cap is not None and (
            isinstance(self.round_cap, bool)
            or not isinstance(self.round_cap, int)
            or self.round_cap < 1
        ):
            raise ValueError(
                f"Roster.round_cap must be a positive int of review rounds, "
                f"got {self.round_cap!r}"
            )
        if self.poll_interval is not None and (
            isinstance(self.poll_interval, bool)
            or not isinstance(self.poll_interval, int)
            or self.poll_interval < 1
        ):
            raise ValueError(
                f"Roster.poll_interval must be a positive int of seconds, "
                f"got {self.poll_interval!r}"
            )
        if self.nit_cap is not None and (
            isinstance(self.nit_cap, bool)
            or not isinstance(self.nit_cap, int)
            or self.nit_cap < 0
        ):
            # 0 is legal: the cap is a budget, not a count.
            raise ValueError(
                f"Roster.nit_cap must be a non-negative int of round-1 nits, "
                f"got {self.nit_cap!r}"
            )
        if self.calibrator is not None and not isinstance(
            self.calibrator, CalibratorConfig
        ):
            raise ValueError(
                f"Roster.calibrator must be a CalibratorConfig, got {self.calibrator!r}"
            )
        names = [e.name for e in self.entries]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"Roster has duplicate reviewer entries {duplicates} — "
                "one entry per reviewer"
            )

    def entry(self, name: str) -> RosterEntry:
        """The settings for `name`; an unconfigured reviewer gets defaults."""
        key = name.lower()
        for e in self.entries:
            if e.name == key:
                return e
        return RosterEntry(name=key)

    @property
    def policy(self) -> ReviewPolicy:
        """The table-level review-run policy for the request path."""
        return ReviewPolicy(calibrator=self.calibrator, nit_cap=self.nit_cap)

    @property
    def required(self) -> tuple[RosterEntry, ...]:
        return tuple(e for e in self.entries if e.required)

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(e.name for e in self.required)
