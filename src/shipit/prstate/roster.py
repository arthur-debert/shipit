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

from ..review.calibrator import CalibratorConfig

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
    defaults); ``dimensions`` the local-agent reviewer's **Dimension pass** set
    (RVW02-WS04 — the per-reviewer fan-out option riding the same seam as
    ``model``/``instructions``; ``None`` → the shipped default set, membership
    validated by the loader against the closed dimension registry). An
    UNCONFIGURED reviewer is exactly the field defaults with its name — which
    is why :meth:`Roster.entry` can be total.
    """

    name: str
    required: bool = False
    rerun: bool = False
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
            # Shape only — MEMBERSHIP in the closed dimension registry is the
            # loader's job (it fails loud with the known set); this value only
            # defends its own invariant (a non-empty tuple of non-empty names).
            raise ValueError(
                f"RosterEntry.dimensions must be a non-empty tuple of dimension "
                f"names, got {self.dimensions!r}"
            )


@dataclass(frozen=True)
class ReviewPolicy:
    """The TABLE-LEVEL review-run policy (RVW02-WS04), bundled for the request
    path: the ONE calibrator config every reviewer's fan-out shares and the
    round-1 nit cap. ``None`` fields mean the run path's shipped defaults —
    the same None-means-shipped-default convention as every other roster
    value. Read only by the local-agent reviewer adapters; App reviewers place
    a plain request edge and never see it."""

    calibrator: CalibratorConfig | None = None
    nit_cap: int | None = None


@dataclass(frozen=True)
class Roster:
    """Every configured reviewer's settings as one frozen value.

    Built once at a verb boundary by ``reviewers_config.load_roster`` and passed
    down — onto the ``ReadinessView`` for the engine/adapters, into the request
    path for run options — so no call path resolves reviewer settings twice and
    no module-global cache exists to reset in tests. The EMPTY roster (the
    dataclass default) is the honest fixture default: no reviewer required,
    every per-reviewer setting at its shipped default.

    ``round_cap`` is the review-loop policy the roster carries ALONGSIDE the
    per-reviewer entries (a table-level `[reviewers]` key, not a reviewer): the
    maximum number of review rounds before the stopping rule fires. ``None``
    (the default) means the shipped default (``breakers.ROUND_CAP``) — the same
    None-means-shipped-default convention as ``RosterEntry.window_seconds``, so
    the breaker rule keeps owning its own constant.

    ``poll_interval`` is the second table-level policy value (ADR-0034): the
    fixed cadence, in whole seconds, at which `pr wait` — the ONE verb that
    blocks — re-polls the evaluator. Tool-owned, never a per-call flag. ``None``
    (the default) means the shipped default (``wait.POLL_INTERVAL_SECONDS``,
    60s) — the waiter keeps owning its own constant, same convention as
    ``round_cap``.

    Two more table-level values are the RVW02-WS04 review-run policy
    (:attr:`policy` bundles them for the request path): ``nit_cap`` — the
    round-1 nit budget the fan-out routing enforces (``None`` → uncapped, the
    shipped default; ``0`` → floor at minor) — and ``calibrator`` — the ONE
    fixed judge config shared by every reviewer
    (:class:`~shipit.review.calibrator.CalibratorConfig`; ``None`` → the
    shipped ``claude``-at-high default). Table-level ON PURPOSE (ADR-0045): a
    per-reviewer calibrator would fork the common severity ruler.
    """

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
            # 0 is legal (floor at minor) — the cap is a budget, not a count.
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
    def policy(self) -> ReviewPolicy:
        """The table-level review-RUN policy as one value (RVW02-WS04) — what
        the request path threads to a local reviewer's detached run alongside
        its per-reviewer entry, so the calibrator + nit cap arrive as values
        exactly like ``model``/``instructions`` do (never re-resolved from
        config inside the run path)."""
        return ReviewPolicy(calibrator=self.calibrator, nit_cap=self.nit_cap)

    @property
    def required(self) -> tuple[RosterEntry, ...]:
        """The required entries (the reviewers that hold Ready), config order."""
        return tuple(e for e in self.entries if e.required)

    @property
    def required_names(self) -> tuple[str, ...]:
        """The required reviewers' names, config order."""
        return tuple(e.name for e in self.required)
