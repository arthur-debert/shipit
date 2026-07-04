"""logread — the domain package for reading shipit's durable JSONL log.

The READER's engine half, promoted out of the verb layer (CLI02 / ADR-0030):
everything about *selecting lines from the log file* lives here — parsing a
stored line into a record, the AND-composed domain-key filter, the Work
Stream display-form normalization, the frozen query value the CLI mints at
parse, and the read/tail/follow iterator engine (rotation detection,
torn-write buffering, malformed-line resilience). Nothing in this package
prints: records and follow updates come OUT of iterators, so follow behavior
is testable without a live terminal loop, and the verb
(:mod:`shipit.verbs.logs`) owns every terminal write (the human line
renderer, the malformed-line stderr note, the flow-view emission).

The package composes with the two LOG04 modules that were already
contract-shaped and stay where they are: :mod:`shipit.flowview` (the pure
story renderer) and :mod:`shipit.branchid` (branch-identity derivation).
The log's LOCATION is not this package's business either — the writer's
:func:`shipit.logsetup.log_file_path` stays the single source of truth.
"""

from __future__ import annotations

from .engine import FOLLOW_INTERVAL, follow_lines, last_n, read_lines
from .query import DEFAULT_TAIL, LogQuery, build_query
from .records import Filter, normalize_ws, parse_record

__all__ = [
    "DEFAULT_TAIL",
    "FOLLOW_INTERVAL",
    "Filter",
    "LogQuery",
    "build_query",
    "follow_lines",
    "last_n",
    "normalize_ws",
    "parse_record",
    "read_lines",
]
