"""``logread/engine`` — JSONL file reading, tail, and live follow, as iterators.

Lines are yielded verbatim; follow buffers torn writes whole and survives rotation.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .records import Filter

#: Seconds between polls while following; small enough to feel live.
FOLLOW_INTERVAL = 0.25


def last_n[T](items: list[T], n: int) -> list[T]:
    """All when ``n < 0``, none when ``n == 0`` — the explicit arm guards ``items[-0:]``."""
    if n < 0:
        return items
    return items[-n:] if n > 0 else []


def read_lines(path: Path, record_filter: Filter, tail: int = -1) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return last_n([ln for ln in lines if record_filter.matches(ln)], tail)


def follow_lines(
    path: Path,
    record_filter: Filter,
    *,
    tail: int = -1,
    sleep: Callable[[float], None] | None = None,
) -> Iterator[str]:
    sleep = sleep or time.sleep
    fh = path.open("r", encoding="utf-8", errors="replace")
    try:
        initial = fh.read()
        # A torn final line seeds the append loop's buffer, so its remainder reunites.
        pending = ""
        tail_lines = initial.splitlines()
        if initial and not initial.endswith("\n"):
            pending = tail_lines.pop()
        matching = [ln for ln in tail_lines if record_filter.matches(ln)]
        yield from last_n(matching, tail)
        while True:
            chunk = fh.readline()
            if chunk:
                # Parsing a half line under a filter would drop the record forever.
                pending += chunk
                if not pending.endswith("\n"):
                    continue
                stripped = pending.rstrip("\n")
                pending = ""
                if record_filter.matches(stripped):
                    yield stripped
                continue
            # Detected by INODE, not size: a busy fresh file can outgrow the old
            # offset between polls. Size still catches in-place truncation.
            try:
                disk = path.stat()
                rotated = (
                    disk.st_ino != os.fstat(fh.fileno()).st_ino
                    or disk.st_size < fh.tell()
                )
            except OSError:
                rotated = False  # mid-rotation flicker; retry on the next tick
            if rotated:
                # Swap only on success: the replacement can be missing for an instant.
                try:
                    replacement = path.open("r", encoding="utf-8", errors="replace")
                except OSError:
                    sleep(FOLLOW_INTERVAL)
                    continue
                fh.close()
                fh = replacement
                # A fragment from the old file can never be completed by the new.
                pending = ""
                continue
            sleep(FOLLOW_INTERVAL)
    finally:
        fh.close()
