"""logs — locate and read shipit's durable per-repo log (OBS01-WS04).

The READER half of WS01's file sink: the writer (:mod:`shipit.logsetup`) drops a
durable, per-repo, rotating log; this verb finds it and shows it. It NEVER
recomputes the location — it consumes :func:`shipit.logsetup.log_file_path`
(``resolve_log_dir`` + the handler's ``LOG_FILENAME``), the single source of
truth, so reader and writer can never disagree about where the log lives. No
platform ``if`` branch, no bespoke log-dir env var (the path library owns the
location, per the epic ``docs/prd/obs01-logging.md``).

The repo whose log we read defaults to the current checkout, resolved through the
:mod:`shipit.gh` boundary (the same source the sink uses); an explicit
``owner/repo`` argument overrides it. The ``gh`` boundary, the resolution base,
and the follow-loop ``sleep`` are injected in tests so nothing touches a real
``$HOME`` or shells out to ``gh``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

from .. import gh, logsetup

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


def _follow(path: Path, *, tail: int, sleep: Callable[[float], None]) -> int:
    """Stream the log live (``tail -f``): print the path + the last ``tail`` lines,
    then echo each appended line as it lands. Ends cleanly on Ctrl-C (exit 0), the
    way ``tail -f`` does. ``sleep`` is injected so a test can drive the poll loop
    and stop it deterministically.
    """
    print(str(path))
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in _last_n(fh.read().splitlines(), tail):
            print(line)
        # fh is now positioned at EOF; subsequent appends are picked up by readline.
        try:
            while True:
                line = fh.readline()
                if line:
                    print(line.rstrip("\n"))
                    continue
                sleep(_FOLLOW_INTERVAL)
        except KeyboardInterrupt:
            return 0


def run(
    repo: str | None = None,
    *,
    path_only: bool = False,
    follow: bool = False,
    tail: int = DEFAULT_TAIL,
    base_dir: str | Path | None = None,
    current_repo: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Locate (and read) the per-repo log. Returns an int exit code.

    ``repo`` overrides the default (the cwd checkout, resolved via the injected
    ``current_repo`` boundary). ``path_only`` prints just the resolved absolute
    path and exits 0 — locating the log never depends on it existing yet, so this
    always succeeds. Otherwise the file is read: ``follow`` streams appended lines
    (``tail -f``); the default prints the path plus the last ``tail`` lines. A
    missing log file is reported on stderr (no traceback) and exits non-zero.

    ``base_dir`` / ``current_repo`` / ``sleep`` are injected boundaries for tests.
    """
    current_repo = current_repo or gh.current_repo
    try:
        slug = repo if repo is not None else current_repo()
        owner_repo = _owner_repo(slug)
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
        return _follow(path, tail=tail, sleep=sleep or time.sleep)

    print(str(path))
    for line in _tail_lines(path, tail):
        print(line)
    return 0
