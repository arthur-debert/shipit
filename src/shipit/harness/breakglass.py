"""``harness/breakglass`` — the escape hatch that lets a blocked coordinator edit
through, and the falsey spellings that disarm it.
"""

from __future__ import annotations

ENV = "SHIPIT_BREAK_GLASS"

#: Compared case-insensitively against the whitespace-stripped value.
FALSEY = frozenset({"", "0", "false", "no", "off"})


def is_armed(value: str) -> bool:
    return value.strip().lower() not in FALSEY
