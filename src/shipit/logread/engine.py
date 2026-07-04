"""The JSONL read engine — file reading, tail, and live follow, as iterators.

The effectful half of the reader's domain package (CLI02 / ADR-0030): it
reads the log FILE and yields the stored lines that survive selection —
nothing here prints, parses for rendering, or knows a terminal exists.
:func:`read_lines` is the bounded static read (filter, then tail);
:func:`follow_lines` is the live one (``tail -f``): the initial tail, then
each appended matching line as it lands, with the two liveness hazards owned
HERE so every consumer inherits them —

- **torn writes**: a concurrent write can be read mid-line, so a fragment —
  whether it arrives through the append loop or is already the file's final
  line at open — is buffered until its newline lands before the line is
  judged (a torn read is not a malformed record, and under an active filter
  parsing a half line would drop the record permanently);
- **rotation**: the writer is a ``RotatingFileHandler``, so the active file
  can be rolled over mid-follow — detected by FILE IDENTITY (the path's inode
  no longer matches the open handle's; a size check alone races a busy fresh
  file) plus the size shrink for in-place truncation, and answered by
  reopening.

Both functions yield the lines EXACTLY as stored: rendering (human lines,
the malformed-line stderr note, raw passthrough) is the verb's job over this
iterator's output, which is what makes follow behavior testable without a
live terminal loop — a test drives the generator with an injected ``sleep``
and asserts on the yielded lines.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, TypeVar

from .records import Filter

#: Seconds between polls while following; small enough to feel live.
FOLLOW_INTERVAL = 0.25

_T = TypeVar("_T")


def last_n(items: list[_T], n: int) -> list[_T]:
    """The last ``n`` of ``items``: all when ``n < 0``, none when ``n == 0``,
    else the final ``n``.

    Generic over the element type so the one tail helper serves both the raw
    line lists and the parsed ``--flow`` record lists. The explicit ``n == 0``
    arm guards the ``items[-0:]`` trap (``-0 == 0``, so a naive slice would
    return EVERY item for ``-n 0`` instead of none).
    """
    if n < 0:
        return items
    return items[-n:] if n > 0 else []


def read_lines(path: Path, record_filter: Filter, tail: int = -1) -> list[str]:
    """The stored lines that survive ``record_filter``, tailed to ``tail``.

    The static read: whole file, filter, then the tail count — so a tailed
    read means "the last N MATCHING lines", never "the last N lines, if they
    happen to match". The file is bounded by rotation, so whole-file filtering
    is cheap (no index until a real slicing gap shows, per the PRD). Lines
    come back exactly as stored — with NO filter active that includes blank
    padding and malformed lines, whose treatment (passthrough, note, drop) is
    the caller's rendering contract, not the engine's.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return last_n([ln for ln in lines if record_filter.matches(ln)], tail)


def follow_lines(
    path: Path,
    record_filter: Filter,
    *,
    tail: int = -1,
    sleep: Callable[[float], None] | None = None,
) -> Iterator[str]:
    """Stream the log live: the last ``tail`` matching lines, then each
    appended matching line as it lands — forever.

    The generator never returns on its own; the consumer decides when the
    stream ends (in production, Ctrl-C — a ``KeyboardInterrupt`` raised out
    of ``sleep`` propagates through and the ``finally`` closes the handle; in
    tests, an injected ``sleep`` drives the poll loop and stops it
    deterministically). Selection is the SAME ``record_filter`` as the static
    read, applied to the initial tail and to every appended line, so follow
    and static select identically. Torn writes are buffered to whole lines
    and rotation is detected and survived, per the module docstring.
    """
    sleep = sleep or time.sleep
    fh = path.open("r", encoding="utf-8", errors="replace")
    try:
        initial = fh.read()
        # A torn final line (content with no trailing newline) is NOT a record
        # yet: seed it into the SAME `pending` buffer the append loop uses so
        # its remainder — read next once the writer finishes the line —
        # reunites here, instead of yielding the head now and the tail later
        # as two split records. Symmetric with the readline path below.
        pending = ""
        tail_lines = initial.splitlines()
        if initial and not initial.endswith("\n"):
            pending = tail_lines.pop()
        matching = [ln for ln in tail_lines if record_filter.matches(ln)]
        yield from last_n(matching, tail)
        # fh is now positioned at EOF; subsequent appends are picked up by readline.
        while True:
            chunk = fh.readline()
            if chunk:
                # A concurrent write can be read mid-line, so readline() may
                # return a fragment with no trailing newline. Buffer until the
                # newline lands before judging the line: a torn read is not a
                # malformed record, and under an active filter parsing a half
                # line would drop it — permanently, since its remainder (read
                # next) is not valid JSON on its own either.
                pending += chunk
                if not pending.endswith("\n"):
                    continue
                stripped = pending.rstrip("\n")
                pending = ""
                if record_filter.matches(stripped):
                    yield stripped
                continue
            # No new data. The writer is a RotatingFileHandler, so the active
            # shipit.log can be rolled over mid-follow — at which point our open
            # handle points at the stale renamed file and would go silent.
            # Detect it by FILE IDENTITY: rollover is a rename + fresh create,
            # so the path's inode no longer matches our handle's. (A size
            # comparison alone is a race — a busy fresh file can outgrow our
            # old read offset between polls and the shrink would never be
            # seen.) The size check stays for the in-place truncation case,
            # where the inode never changes.
            try:
                disk = path.stat()
                rotated = (
                    disk.st_ino != os.fstat(fh.fileno()).st_ino
                    or disk.st_size < fh.tell()
                )
            except OSError:
                rotated = False  # mid-rotation flicker; retry on the next tick
            if rotated:
                fh.close()
                fh = path.open("r", encoding="utf-8", errors="replace")
                continue
            sleep(FOLLOW_INTERVAL)
    finally:
        fh.close()
