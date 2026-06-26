"""Logging configuration for shipit's surface sinks.

Named ``logsetup`` (not ``logging``) so it never shadows the stdlib module.

shipit routes its output through the package logger ``logging.getLogger("shipit")``.
This module owns the *surface* sinks — the ones a human or a CI job reads:

- **Console** — quiet by default (WARNING+ to stderr), so the user-facing surface
  is unchanged in spirit from today. ``-v/--verbose`` raises it to DEBUG so an
  interactive debugging session can watch detail live.
- **CI** — when a CI environment is detected, a stdout handler so the run's record
  lands in the job log; and, when ``$GITHUB_STEP_SUMMARY`` is present, a second
  handler that appends the same records to that file.

The **file sink** (the durable per-repo diagnosis record) is a sibling work
stream and is wired in elsewhere; each sink here lives in its own builder so the
merge is a trivial additive union inside :func:`configure_logging`. The console
and CI levels are deliberately independent of the file sink's DEBUG/INFO level.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping

LOGGER_NAME = "shipit"

# Every handler this module attaches carries a name with this prefix, so we can
# recognise — and replace — exactly our own handlers on a repeated call without
# disturbing anything a host application may have attached to the logger.
_HANDLER_PREFIX = "shipit-"

#: CI-detection env vars, in no particular order. ``GITHUB_ACTIONS`` is the
#: GitHub-specific signal; ``CI`` is the de-facto cross-provider convention.
_CI_ENV_VARS = ("GITHUB_ACTIONS", "CI")


def _formatter() -> logging.Formatter:
    """A single, plain record format shared by every surface sink."""
    return logging.Formatter("%(levelname)s %(name)s: %(message)s")


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
    handler.setFormatter(_formatter())
    handler.set_name(_HANDLER_PREFIX + "console")
    return handler


def build_ci_stdout_handler() -> logging.Handler:
    """Build the CI stdout handler so the run's record lands in the job log."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci-stdout")
    return handler


def build_step_summary_handler(path: str) -> logging.Handler:
    """Build a handler that appends records to ``$GITHUB_STEP_SUMMARY``."""
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci-summary")
    return handler


def _clear_own_handlers(logger: logging.Logger) -> None:
    """Detach (and close) only the handlers this module previously attached.

    Keyed on the ``shipit-`` name prefix so a repeated :func:`configure_logging`
    call never stacks duplicate handlers, while leaving foreign handlers alone.
    """
    for handler in list(logger.handlers):
        if (handler.name or "").startswith(_HANDLER_PREFIX):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(
    verbose: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    """Configure the ``shipit`` package logger and attach the surface sinks.

    The package logger is set to ``DEBUG`` (it passes everything through; each
    handler's own level decides what that surface shows) and is detached from
    the root logger so records do not double-emit. The console handler is always
    attached; the CI handlers are attached only when a CI environment is
    detected (``env`` is injectable for tests, defaulting to ``os.environ``).

    Safe to call repeatedly: only this module's own handlers are replaced, so
    successive calls re-apply the level without stacking duplicates.

    The file sink is wired in by a sibling work stream; it slots in here as one
    more additive ``logger.addHandler(...)`` line with no other change.
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
            logger.addHandler(build_step_summary_handler(summary_path))
