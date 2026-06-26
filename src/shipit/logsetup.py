"""Central logging configuration for shipit — the observability spine's entrypoint.

Named ``logsetup`` (NOT ``logging``) so it never shadows the stdlib module. It
configures the package logger ``logging.getLogger("shipit")`` and attaches the
sinks shipit logs through. Each sink lives in its own builder so the wiring in
:func:`configure_logging` is a simple, additive union.

Three sinks, chosen for where shipit runs (PRD ``docs/prd/obs01-logging.md``):

- **Console** — quiet by default (WARNING+ to stderr), so the user-facing surface
  is unchanged in spirit from today. ``-v/--verbose`` raises it to DEBUG so an
  interactive debugging session can watch detail live.
- **CI** — when a CI environment is detected, a stdout handler so the run's record
  lands in the job log (DEBUG-level, the durable artifact CI keeps); and, when
  ``$GITHUB_STEP_SUMMARY`` is present, a best-effort handler that appends records
  to that file.
- **File** — the durable, per-repo, rotating diagnosis record. Path resolution is
  :func:`platformdirs.user_log_dir` — the single source of truth (no platform
  ``if`` branches, no bespoke override env var) — namespaced ``<base>/<owner>/<repo>/``
  and bounded by a :class:`~logging.handlers.RotatingFileHandler`. The base and
  the ``(owner, repo)`` namespace are injectable so tests cross the boundary
  without writing to a real ``$HOME``.

The three level controls are independent: the file sink is always verbose
(DEBUG); the console is quiet unless ``-v``; the CI stdout sink is verbose. Every
handler this module attaches carries a ``shipit-`` name prefix so a repeated
:func:`configure_logging` call replaces exactly its own handlers and never
double-attaches, while leaving any foreign handler alone.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platformdirs

from . import gh

#: The package logger every shipit module logs through (``logging.getLogger``
#: of a child name propagates here).
LOGGER_NAME = "shipit"

# Every handler this module attaches carries a name with this prefix, so we can
# recognise — and replace — exactly our own handlers on a repeated call without
# disturbing anything a host application may have attached to the logger.
_HANDLER_PREFIX = "shipit-"

#: CI-detection env vars, in no particular order. ``GITHUB_ACTIONS`` is the
#: GitHub-specific signal; ``CI`` is the de-facto cross-provider convention.
_CI_ENV_VARS = ("GITHUB_ACTIONS", "CI")

#: The basename of the active log file inside the per-repo directory.
LOG_FILENAME = "shipit.log"

#: Rotation bound: ~5 MB per file × 3 backups, so the log can never fill the
#: disk (PRD §Implementation Decisions — a starting point, not a config surface
#: in this epic).
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

#: Stable handler name for the file sink. Shares the ``shipit-`` prefix so the
#: idempotency sweep covers it too.
_FILE_HANDLER_NAME = _HANDLER_PREFIX + "file"

#: The verbose file record format — timestamp, level, logger, message.
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"

#: The plain record format shared by the console / CI surface sinks.
_SURFACE_FORMAT = "%(levelname)s %(name)s: %(message)s"


def _surface_formatter() -> logging.Formatter:
    """The plain record format shared by the console / CI surface sinks."""
    return logging.Formatter(_SURFACE_FORMAT)


# --------------------------------------------------------------------------
# Surface sinks — console + CI
# --------------------------------------------------------------------------


def is_ci(env: Mapping[str, str] | None = None) -> bool:
    """Return whether we appear to be running inside a CI environment.

    ``env`` is injectable so tests never depend on the real process environment;
    it defaults to ``os.environ``. A CI is detected when any known signal var is
    set to a non-empty, non-``false`` value (GitHub sets ``CI=true``).
    """
    env = os.environ if env is None else env
    for var in _CI_ENV_VARS:
        value = env.get(var)
        if value and value.strip().lower() not in ("", "0", "false"):
            return True
    return False


def build_console_handler(verbose: bool = False) -> logging.Handler:
    """Build the quiet-by-default console handler (stderr).

    WARNING and above by default — so normal output looks like it does today —
    raised to DEBUG when ``verbose`` is set.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "console")
    return handler


def build_ci_stdout_handler() -> logging.Handler:
    """Build the CI stdout handler so the run's record lands in the job log.

    Captures DEBUG and up: in CI the job log *is* the durable run record (per the
    PRD), so it carries the full verbose detail, not just INFO+.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci-stdout")
    return handler


def build_step_summary_handler(path: str) -> logging.Handler:
    """Build a handler that appends records to ``$GITHUB_STEP_SUMMARY``."""
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci-summary")
    return handler


# --------------------------------------------------------------------------
# File sink — the durable, per-repo, rotating diagnosis record
# --------------------------------------------------------------------------


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
    emits at ``DEBUG`` (the verbose record), independent of the console level. The
    per-repo directory is created on demand.
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


def _current_owner_repo() -> tuple[str, str]:
    """``(owner, repo)`` for the current checkout, via the :mod:`shipit.gh` boundary.

    The boundary returns ``owner/name`` (``gh repo view --json nameWithOwner``).
    A value that is not a two-part slug is a real failure — fail loud rather than
    silently writing to an empty/incorrect log directory.
    """
    slug = gh.current_repo()
    owner, sep, repo = slug.partition("/")
    if not sep or not owner or not repo:
        raise ValueError(
            f"expected an 'owner/repo' slug from gh.current_repo(), got {slug!r}"
        )
    return owner, repo


def resolve_current_owner_repo() -> tuple[str, str] | None:
    """Best-effort ``(owner, repo)`` for the current checkout, or ``None``.

    For the CLI entrypoint, where a logging-setup failure must never crash the
    command: if the repo can't be determined (not a checkout, ``gh`` unavailable,
    a malformed slug), return ``None`` so the caller simply runs without the file
    sink rather than aborting.
    """
    try:
        return _current_owner_repo()
    except (gh.GhError, ValueError):
        return None


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _clear_own_handlers(logger: logging.Logger) -> None:
    """Detach (and close) only the handlers this module previously attached.

    Keyed on the ``shipit-`` name prefix (which covers console, CI, and file
    handlers) so a repeated :func:`configure_logging` call never stacks duplicate
    handlers, while leaving foreign handlers alone.
    """
    for handler in list(logger.handlers):
        if (handler.name or "").startswith(_HANDLER_PREFIX):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(
    verbose: bool = False,
    env: Mapping[str, str] | None = None,
    *,
    owner_repo: tuple[str, str] | None = None,
    base_dir: str | Path | None = None,
) -> None:
    """Configure the ``shipit`` package logger and attach its sinks.

    The package logger is set to ``DEBUG`` (it passes everything through; each
    handler's own level decides what that surface shows) and is detached from the
    root logger so records do not double-emit. Safe to call repeatedly: only this
    module's own (``shipit-``prefixed) handlers are replaced, so successive calls
    re-apply levels without stacking duplicates.

    Sinks:

    - **Console** — always attached; quiet (WARNING+) unless ``verbose``.
    - **CI** — attached only when :func:`is_ci` (``env`` is injectable, defaulting
      to ``os.environ``): a stdout handler, plus a best-effort
      ``$GITHUB_STEP_SUMMARY`` appender.
    - **File** — attached when a target repo is known, i.e. when ``owner_repo`` or
      ``base_dir`` is provided. ``owner_repo`` / ``base_dir`` are injectable
      boundaries for tests; with ``base_dir`` given but ``owner_repo`` omitted, the
      repo is resolved (strictly) via :mod:`shipit.gh`. The CLI entrypoint resolves
      ``owner_repo`` best-effort (:func:`resolve_current_owner_repo`) and passes it,
      so a normal run gets the file sink and a non-repo run simply skips it.
    """
    env = os.environ if env is None else env

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    _clear_own_handlers(logger)

    # Console sink — always on, quiet by default.
    logger.addHandler(build_console_handler(verbose=verbose))

    # CI sinks — only when we detect a CI environment.
    if is_ci(env):
        logger.addHandler(build_ci_stdout_handler())
        summary_path = env.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            # The step-summary sink is best-effort: if the path can't be opened
            # (missing dir, permissions, …) we keep the stdout CI sink and carry
            # on rather than fail the command — a logging glitch never gates.
            try:
                logger.addHandler(build_step_summary_handler(summary_path))
            except OSError:
                logger.debug(
                    "could not open GITHUB_STEP_SUMMARY at %s; "
                    "skipping step-summary sink",
                    summary_path,
                )

    # File sink — the durable per-repo record, attached when a target repo is
    # known (a param was injected, or the CLI resolved and passed one).
    if owner_repo is not None or base_dir is not None:
        if owner_repo is None:
            owner_repo = _current_owner_repo()
        logger.addHandler(build_file_handler(owner_repo, base_dir=base_dir))
