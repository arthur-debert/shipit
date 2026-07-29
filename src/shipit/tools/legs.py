"""``tools/legs`` — pure: (map entries, tool, selector, passthrough) → legs.

See docs/adr/0039-tool-verbs.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .. import config
from . import registry


class LegPlanError(Exception):
    """The invocation cannot be planned; the message is the whole diagnosis."""


@dataclass(frozen=True)
class Leg:
    """``argv`` is the COMPLETE producing command, run with cwd at ``path``."""

    path: str
    toolchain: str
    tool: str
    argv: tuple[str, ...]

    @property
    def label(self) -> str:
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
    """The ordered legs a ``tool`` invocation runs; ``entries`` order is fan-out
    order, and ``selector`` names a toolchain or a map path.
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
