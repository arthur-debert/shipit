"""The leg planner — pure: (map entries, tool, selector, passthrough) → legs.

A **Leg** is one Tool applied to one path→toolchain map entry (``test rust``,
CONTEXT.md). :func:`plan_legs` turns the typed map
(:class:`shipit.config.ToolchainEntry`, parsed at the config boundary per
ADR-0030) into the ordered invocations a verb executes, applying the ADR-0039
rules in one place:

- a bare invocation fans out over ALL legs, in map order — the hooks' and
  CI's form;
- a selector (a toolchain name, or a map path for a repo with several legs of
  one toolchain) filters the fan-out; an unknown selector is a
  :class:`LegPlanError` naming the known legs;
- passthrough args forward VERBATIM, appended to the selected leg's producing
  command — and only ever to ONE leg: passthrough with several legs selected
  (no selector on a multi-leg repo, or a selector matching several paths) is
  a hard :class:`LegPlanError` listing the legs, never a broadcast;
- a single-leg repo may omit the selector (the no-selector sugar).

Pure (no I/O, no Exec): fully fixture-testable, the same split the lint
verb's ``route``/``verdict`` pair uses. The effectful shell that runs the
planned legs is :mod:`shipit.verbs.test`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .. import config
from . import registry


class LegPlanError(Exception):
    """The invocation cannot be planned — a USAGE error (exit 2, ADR-0030).

    Raised for an unknown leg selector, and for passthrough args that would
    reach more than one leg. The message is the whole user-facing diagnosis
    (it names the known/selected legs), so the verb prints it verbatim.
    """


@dataclass(frozen=True)
class Leg:
    """One planned invocation: a tool applied to one map entry.

    ``argv`` is the COMPLETE producing command — the per-path override or the
    registry default, with any passthrough args already appended — run with
    cwd at ``path`` (relative to the repo root; ``"."`` for the root).
    """

    path: str
    toolchain: str
    tool: str
    argv: tuple[str, ...]

    @property
    def label(self) -> str:
        """The leg's display name — ``rust (.)`` — used by every listing."""
        return f"{self.toolchain} ({self.path})"


def _legs_list(legs: Sequence[Leg]) -> str:
    return ", ".join(leg.label for leg in legs)


def plan_legs(
    entries: Sequence[config.ToolchainEntry],
    *,
    tool: str,
    selector: str | None = None,
    passthrough: Sequence[str] = (),
) -> tuple[Leg, ...]:
    """The ordered legs a ``tool`` invocation runs, per the ADR-0039 rules.

    ``entries`` is the typed path→toolchain map in DECLARATION ORDER (the
    fan-out order — ``.shipit.toml`` order is the contract, no re-sorting).
    ``selector`` names a toolchain or a map path; ``passthrough`` is appended
    verbatim to the (single) selected leg's argv. Raises
    :class:`LegPlanError` on the selector/passthrough rule violations
    documented in the module docstring, and :class:`ValueError` for an entry
    whose toolchain is not registered — a caller bug: entries reach the
    planner already validated by :func:`shipit.config.load_toolchains`.
    """
    legs: list[Leg] = []
    for entry in entries:
        tc = registry.toolchain(entry.toolchain)
        if tc is None:  # pragma: no cover — load_toolchains validates
            raise ValueError(f"unregistered toolchain {entry.toolchain!r}")
        argv = entry.commands.get(tool) or tc.command(tool)
        legs.append(
            Leg(path=entry.path, toolchain=entry.toolchain, tool=tool, argv=argv)
        )

    selected = legs
    if selector is not None:
        selected = [leg for leg in legs if selector in (leg.toolchain, leg.path)]
        if not selected:
            raise LegPlanError(
                f"unknown leg {selector!r} — this repo's {tool} legs: "
                f"{_legs_list(legs)}"
            )

    if passthrough and len(selected) > 1:
        # Never a broadcast: flags meant for one runner would break another.
        if selector is None:
            raise LegPlanError(
                f"passthrough args need a leg selector on a multi-leg repo — "
                f"this repo's {tool} legs: {_legs_list(selected)}; "
                f"e.g. `shipit {tool} {selected[0].toolchain} -- …`"
            )
        raise LegPlanError(
            f"passthrough args need exactly one leg, but {selector!r} matches "
            f"{len(selected)}: {_legs_list(selected)} — select one by path, "
            f"e.g. `shipit {tool} {selected[0].path} -- …`"
        )

    if passthrough:
        if not selected:
            # No leg to append to — an empty map reached the planner (the verb
            # rejects that earlier, but plan_legs is a public pure function and
            # must not IndexError on empty input).
            raise LegPlanError(
                f"no {tool} legs declared — nothing to forward passthrough args to"
            )
        leg = selected[0]
        selected = [
            Leg(
                path=leg.path,
                toolchain=leg.toolchain,
                tool=leg.tool,
                argv=(*leg.argv, *passthrough),
            )
        ]
    return tuple(selected)
