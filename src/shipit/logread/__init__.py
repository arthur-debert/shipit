"""``shipit.logread`` — selecting lines from shipit's durable JSONL log.

Nothing here prints: records come out of iterators and the verb writes them.
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
