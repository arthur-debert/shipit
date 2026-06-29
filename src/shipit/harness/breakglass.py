"""Break-glass escape-hatch semantics — the ONE definition of `SHIPIT_BREAK_GLASS`.

HAR01's runtime escape hatch (`SHIPIT_BREAK_GLASS=<truthy>`) lets a coordinator's
otherwise-blocked code edit through (LOGGED). Two callers must agree on what counts
as ARMED: the live :mod:`shipit.verbs.hook.pretooluse` hook (which reads the env
var) and the eval break-glass grep (:func:`shipit.harness.eval.extractors.break_glass_count`,
which greps tool-call inputs). The env name and the falsey spellings that DISARM it
live here ONCE so the two cannot drift — a disarming spelling for the hook is a
disarming spelling for the eval.
"""

from __future__ import annotations

#: The break-glass environment variable name.
ENV = "SHIPIT_BREAK_GLASS"

#: Values that DISARM the escape (treated as off): the empty string and the usual
#: false-y spellings. Comparison is case-insensitive on the whitespace-stripped value.
FALSEY = frozenset({"", "0", "false", "no", "off"})


def is_armed(value: str) -> bool:
    """True when ``value`` ARMS the escape (i.e. is not one of the falsey spellings)."""
    return value.strip().lower() not in FALSEY
