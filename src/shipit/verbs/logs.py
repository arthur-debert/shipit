"""logs — locate and read shipit's durable per-repo JSONL log (LOG01-WS04).

The READER half of WS01's file sink: the writer (:mod:`shipit.logsetup`) drops a
durable, per-repo, rotating **JSONL** log (ADR-0029 — one flat JSON object per
record: ``ts``, ``level``, ``logger``, ``msg``, plus domain keys
present-when-bound); this verb finds it and shows it. It NEVER recomputes the
location — it consumes :func:`shipit.logsetup.log_file_path` (``resolve_log_dir``
+ the handler's ``LOG_FILENAME``), the single source of truth, so reader and
writer can never disagree about where the log lives. No platform ``if`` branch,
no bespoke log-dir env var (the path library owns the location, per the glassbox
PRD ``docs/prd/glassbox.md``).

The verb reads JSONL ONLY — a hard cutover, no dual-format sniffing (ADR-0029;
pre-cutover freeform files age out via rotation). Two output modes: the default
renders each record legibly for humans (``ts LEVEL logger: msg [key=value …]``);
``--raw`` passes the stored lines through unmodified — and prints nothing else —
so stdout pipes straight into jq. In the rendered view, a line that is not a
JSON object is skipped with a stderr note, never a crash: the log is diagnosis
data, and one corrupt line (a torn write, a rotation seam) must not take down
the reader. ``--raw`` does not parse at all — malformed lines pass through
untouched, because judging them is the downstream tool's job.

The repo whose log we read defaults to the current checkout, resolved through the
:mod:`shipit.gh` boundary (the same source the sink uses); an explicit
``owner/repo`` argument overrides it. The ``gh`` boundary, the resolution base,
and the follow-loop ``sleep`` are injected in tests so nothing touches a real
``$HOME`` or shells out to ``gh``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from .. import execrun, gh, logsetup, redact

#: Default number of trailing lines the no-flag invocation prints.
DEFAULT_TAIL = 50

#: Seconds between polls while following (``-f``); small enough to feel live.
_FOLLOW_INTERVAL = 0.25

#: Exit code when the log file does not exist yet (a clean "nothing to read",
#: not a crash — the path is valid, the run that writes it just hasn't happened).
_EXIT_NO_LOG = 1

#: Exit code for a bad ``owner/repo`` (usage error).
_EXIT_BAD_REPO = 2


def _owner_repo(slug: str) -> tuple[str, str]:
    """Split an ``owner/repo`` slug into its parts, rejecting anything else.

    A value that is not a two-part slug is surfaced to the user rather than
    silently targeting an empty/incorrect log directory.
    """
    owner, sep, repo = slug.partition("/")
    if not sep or not owner or not repo:
        raise ValueError(f"expected an 'owner/repo' slug, got {slug!r}")
    return owner, repo


def _last_n(lines: list[str], n: int) -> list[str]:
    """The last ``n`` of ``lines``: all when ``n < 0``, none when ``n == 0``,
    else the final ``n``.

    The explicit ``n == 0`` arm guards the ``lines[-0:]`` trap (``-0 == 0``, so a
    naive slice would return EVERY line for ``-n 0`` instead of none).
    """
    if n < 0:
        return lines
    return lines[-n:] if n > 0 else []


def _tail_lines(path: Path, n: int) -> list[str]:
    """The last ``n`` lines of ``path`` (newlines stripped).

    The file is bounded (``RotatingFileHandler`` caps it at ~5 MB), so reading it
    whole is cheap and keeps this simple — no seek-from-end arithmetic.
    """
    return _last_n(path.read_text(encoding="utf-8", errors="replace").splitlines(), n)


#: Longest malformed-line snippet quoted in the skip note; enough to identify
#: the line without spraying a whole corrupt record onto stderr.
_SNIPPET_LEN = 80

#: Record fields the renderer places explicitly (everything else — the bound
#: domain keys and event extras — trails as ``key=value``).
_RENDERED_FIELDS = ("ts", "level", "logger", "msg", "exception")


def _render_record(line: str) -> str | None:
    """Render one JSONL record for humans, or ``None`` when the line is not one.

    The legible shape mirrors the console surface (``LEVEL logger: msg``) with
    the durable record's extra facts folded in: the ``ts`` up front (the file's
    reason to exist is the timestamped history) and every remaining flat field —
    the bound domain keys (``pr``, ``session``, …) and event extras — trailing
    as sorted ``key=value`` pairs. An ``exception`` (WS01 flattens tracebacks to
    a string) lands on the following lines, the way stdlib formatting would.

    Only a JSON *object* is a record; any other parse (or a parse failure) is
    the caller's cue to skip the line with a note.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    ts = record.get("ts", "")
    level = str(record.get("level", "")).upper()
    logger = record.get("logger", "")
    msg = record.get("msg", "")
    exception = record.get("exception")
    rendered = f"{ts} {level} {logger}: {msg}"
    extras = " ".join(
        f"{k}={v}" for k, v in sorted(record.items()) if k not in _RENDERED_FIELDS
    )
    if extras:
        rendered = f"{rendered} [{extras}]"
    if exception:
        rendered = f"{rendered}\n{exception}"
    return rendered


def _emit(line: str, *, raw: bool) -> None:
    """Emit one log line in the chosen mode.

    ``raw`` is the jq passthrough: the line goes out exactly as stored, parsed by
    nobody (a malformed line is the downstream tool's to judge). Otherwise the
    line is rendered for humans; a blank line is dropped silently (file padding,
    not a record) and a malformed one is skipped with a stderr note — stdout
    carries only rendered records, and a corrupt line never crashes the reader.
    The malformed-line snippet is the one path that echoes raw file content the
    writer's pipeline never finished redacting (a torn write, a pre-cutover
    freeform line), so it passes through :func:`shipit.redact.redact_text`
    (token/PEM pattern masking) before reaching stderr.

    Every ``print`` flushes: ``-f`` output is piped as often as watched
    (``shipit logs -f --raw | jq .``), and Python block-buffers a
    non-interactive stdout — without the flush, records would sit in the buffer
    instead of streaming live.
    """
    if raw:
        print(line, flush=True)
        return
    if not line.strip():
        return
    rendered = _render_record(line)
    if rendered is None:
        # Redact BEFORE truncating, so a secret straddling the snippet cut is
        # still seen whole by the pattern matcher.
        snippet = redact.redact_text(line)[:_SNIPPET_LEN]
        print(
            f"logs: skipped malformed line: {snippet!r}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(rendered, flush=True)


def _follow(path: Path, *, tail: int, raw: bool, sleep: Callable[[float], None]) -> int:
    """Stream the log live (``tail -f``): the last ``tail`` lines, then each
    appended line as it lands — every line through :func:`_emit`, so follow and
    the static view render (or pass through) identically. The path header prints
    only in the human mode: raw stdout is reserved for JSONL. Ends cleanly on
    Ctrl-C (exit 0), the way ``tail -f`` does. ``sleep`` is injected so a test
    can drive the poll loop and stop it deterministically.
    """
    if not raw:
        print(str(path), flush=True)
    fh = path.open("r", encoding="utf-8", errors="replace")
    try:
        for line in _last_n(fh.read().splitlines(), tail):
            _emit(line, raw=raw)
        # fh is now positioned at EOF; subsequent appends are picked up by readline.
        while True:
            line = fh.readline()
            if line:
                _emit(line.rstrip("\n"), raw=raw)
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
            sleep(_FOLLOW_INTERVAL)
    except KeyboardInterrupt:
        return 0
    finally:
        fh.close()


def run(
    repo: str | None = None,
    *,
    path_only: bool = False,
    follow: bool = False,
    raw: bool = False,
    tail: int = DEFAULT_TAIL,
    base_dir: str | Path | None = None,
    current_repo: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Locate (and read) the per-repo JSONL log. Returns an int exit code.

    ``repo`` overrides the default (the cwd checkout, resolved via the injected
    ``current_repo`` boundary). ``path_only`` prints just the resolved absolute
    path and exits 0 — locating the log never depends on it existing yet, so this
    always succeeds. Otherwise the file is read: ``follow`` streams appended lines
    (``tail -f``); the default prints the path plus the last ``tail`` records.
    ``raw`` swaps the human rendering for an unmodified-JSONL passthrough (no
    path header — stdout is pure JSONL for jq) and composes with both views. A
    missing log file is reported on stderr (no traceback) and exits non-zero.

    ``base_dir`` / ``current_repo`` / ``sleep`` are injected boundaries for tests.
    """
    current_repo = current_repo or gh.current_repo
    try:
        slug = repo if repo is not None else current_repo()
        owner_repo = _owner_repo(slug)
    except execrun.ExecError as exc:
        # Resolving the cwd repo shelled out and failed — not a checkout, or gh
        # is unavailable. Keep the verb's promise of a clean message, no traceback.
        print(
            "logs: could not determine the current repo (not a git checkout, or "
            f"gh unavailable); pass an explicit owner/repo. ({exc})",
            file=sys.stderr,
        )
        return _EXIT_BAD_REPO
    except ValueError as exc:
        print(f"logs: {exc}", file=sys.stderr)
        return _EXIT_BAD_REPO

    path = logsetup.log_file_path(owner_repo, base_dir=base_dir)

    if path_only:
        print(str(path))
        return 0

    if not path.exists():
        print(
            f"logs: no log yet at {path} — it is created on the first shipit run "
            f"that logs for {owner_repo[0]}/{owner_repo[1]}.",
            file=sys.stderr,
        )
        return _EXIT_NO_LOG

    if follow:
        return _follow(path, tail=tail, raw=raw, sleep=sleep or time.sleep)

    if not raw:
        print(str(path))
    for line in _tail_lines(path, tail):
        _emit(line, raw=raw)
    return 0
