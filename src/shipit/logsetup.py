"""Central logging configuration for shipit — the observability spine's entrypoint.

Named ``logsetup`` (NOT ``logging``) so it never shadows the stdlib module. It
configures the package logger ``logging.getLogger("shipit")`` and attaches the
sinks shipit logs through.

This work stream (OBS01-WS01) wires the durable, per-repo, rotating **file**
sink — the diagnosis record (PRD ``docs/prd/obs01-logging.md`` §Solution). The
console / CI handlers are a sibling work stream (WS02); each sink lives in its
own builder function so a sibling stream merges in by adding one call inside
:func:`configure_logging`.

Path resolution is :func:`platformdirs.user_log_dir` — the single source of
truth (no platform ``if`` branches, no bespoke override env var). The base and
the ``(owner, repo)`` namespace are injectable so tests cross the boundary
without writing to a real ``$HOME``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platformdirs

from . import gh

#: The package logger every shipit module logs through (``logging.getLogger``
#: of a child name propagates here).
LOGGER_NAME = "shipit"

#: The basename of the active log file inside the per-repo directory.
LOG_FILENAME = "shipit.log"

#: Rotation bound: ~5 MB per file × 3 backups, so the log can never fill the
#: disk (PRD §Implementation Decisions — a starting point, not a config surface
#: in this epic).
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

#: Stable handler name so a repeated :func:`configure_logging` never
#: double-attaches the file sink.
_FILE_HANDLER_NAME = "shipit-file"

#: The verbose record format — timestamp, level, logger, message.
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


def resolve_log_dir(
    owner_repo: tuple[str, str],
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """The per-repo log directory ``<base>/<owner>/<repo>/``.

    ``base_dir`` is the platformdirs base; when ``None`` it is resolved via
    ``platformdirs.user_log_dir("shipit")`` (macOS → ``~/Library/Logs/shipit``,
    Linux → ``~/.local/state/shipit/log``). Tests inject ``base_dir`` (and the
    ``owner_repo``) so the path is asserted without touching a real ``$HOME``.
    """
    base = (
        Path(base_dir)
        if base_dir is not None
        else Path(platformdirs.user_log_dir("shipit"))
    )
    owner, repo = owner_repo
    return base / owner / repo


def build_file_handler(
    owner_repo: tuple[str, str],
    *,
    base_dir: str | Path | None = None,
) -> RotatingFileHandler:
    """The durable per-repo rotating file sink — the diagnosis record.

    A :class:`~logging.handlers.RotatingFileHandler` bounded at :data:`MAX_BYTES`
    × :data:`BACKUP_COUNT` so it rolls over rather than growing without limit. It
    emits at ``DEBUG`` (the verbose record), independent of any console level a
    sibling work stream sets. The per-repo directory is created on demand.
    """
    log_dir = resolve_log_dir(owner_repo, base_dir=base_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / LOG_FILENAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.set_name(_FILE_HANDLER_NAME)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    return handler


def configure_logging(
    verbose: bool = False,
    *,
    owner_repo: tuple[str, str] | None = None,
    base_dir: str | Path | None = None,
) -> None:
    """Configure the ``shipit`` package logger and attach the file sink.

    Sets the package logger to ``DEBUG`` so the verbose file record is captured
    regardless of the (quieter) console level a sibling work stream applies, and
    is idempotent: repeated calls never double-attach the file handler (guarded
    by handler name). ``propagate`` is turned off so shipit's records do not
    also bubble to a host app's root logger.

    ``owner_repo`` / ``base_dir`` are injectable boundaries for tests; in normal
    use ``owner_repo`` is resolved from the current checkout via :mod:`shipit.gh`
    and ``base_dir`` from ``platformdirs``.

    WS02 merge seam: the console / CI handlers attach here too. Add their builder
    calls alongside the file handler below; ``verbose`` is the thread for the
    console-level control (this stream keeps it in the signature but the file
    sink is verbose unconditionally).
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not _has_handler(logger, _FILE_HANDLER_NAME):
        if owner_repo is None:
            owner_repo = _current_owner_repo()
        logger.addHandler(build_file_handler(owner_repo, base_dir=base_dir))


def _current_owner_repo() -> tuple[str, str]:
    """``(owner, repo)`` for the current checkout, via the :mod:`shipit.gh` boundary."""
    owner, _, repo = gh.current_repo().partition("/")
    return owner, repo


def _has_handler(logger: logging.Logger, name: str) -> bool:
    """Whether ``logger`` already carries a handler named ``name``."""
    return any(getattr(handler, "name", None) == name for handler in logger.handlers)
