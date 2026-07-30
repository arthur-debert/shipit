"""Central logging configuration — the ``shipit`` logger and its console/CI/file sinks.

Named ``logsetup`` so it never shadows stdlib ``logging``. See docs/adr/0029-agents-first-jsonl-logging.md.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import platformdirs
import structlog

from . import events, identity, logcontext, redact
from .identity import Repo
from .session import current as session_current
from .tree import layout

LOGGER_NAME = "shipit"

_HANDLER_PREFIX = "shipit-"

_CI_ENV_VARS = ("GITHUB_ACTIONS", "CI")

LOG_FILENAME = "shipit.log"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_FILE_HANDLER_NAME = _HANDLER_PREFIX + "file"


_STDLIB_FORMAT_ARTIFACTS = ("message", "asctime")

_EXTRA_ADDER = structlog.stdlib.ExtraAdder()


def _add_stdlib_extras(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Adopt stdlib ``extra={...}`` fields into the record, minus the artifacts an earlier handler's ``format()`` left on the shared record."""
    event_dict = _EXTRA_ADDER(logger, method_name, event_dict)
    for key in _STDLIB_FORMAT_ARTIFACTS:
        event_dict.pop(key, None)
    return event_dict


_PIPELINE = (
    logcontext.merge_domain_keys,
    _add_stdlib_extras,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
    structlog.processors.format_exc_info,
    redact.redact_event,
)


def _flatten_to_scalars(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Degrade any non-JSON-scalar value to its ``repr`` so a record can never nest or fail to serialize."""
    for key, value in event_dict.items():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            event_dict[key] = repr(value)
    return event_dict


def _file_formatter() -> logging.Formatter:
    """The JSONL renderer for the file sink: one flat JSON object per record, ``event`` renamed to ``msg``."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_PIPELINE,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.EventRenamer("msg", replace_by=events.EXTRA_KEY),
            _flatten_to_scalars,
            structlog.processors.JSONRenderer(),
        ],
    )


def _render_surface(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> str:
    """Render a processed record for the human surfaces as ``LEVEL logger: message`` plus bound ``key=value`` pairs."""
    level = str(event_dict.pop("level", "")).upper()
    name = event_dict.pop("logger", "")
    message = event_dict.pop("event", "")
    if events.EXTRA_KEY in event_dict:
        event_dict[events.RECORD_KEY] = event_dict.pop(events.EXTRA_KEY)
    event_dict.pop("ts", None)
    exception = event_dict.pop("exception", None)
    line = f"{level} {name}: {message}"
    extras = " ".join(f"{k}={v}" for k, v in sorted(event_dict.items()))
    if extras:
        line = f"{line} [{extras}]"
    if exception:
        line = f"{line}\n{exception}"
    return line


def _surface_formatter() -> logging.Formatter:
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_PIPELINE,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _render_surface,
        ],
    )


def is_ci(env: Mapping[str, str] | None = None) -> bool:
    """Whether we appear to be in CI; ``env`` defaults to ``os.environ``."""
    env = os.environ if env is None else env
    for var in _CI_ENV_VARS:
        value = env.get(var)
        if value and value.strip().lower() not in ("", "0", "false"):
            return True
    return False


def build_console_handler(verbose: bool = False) -> logging.Handler:
    """The console handler (stderr): WARNING+, or DEBUG when ``verbose``."""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "console")
    return handler


def build_ci_handler() -> logging.Handler:
    """The CI handler: DEBUG+ to stderr, leaving stdout for command/``--json`` output."""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci")
    return handler


def build_step_summary_handler(path: str) -> logging.Handler:
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci-summary")
    return handler


def resolve_log_dir(
    repo: Repo,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """The per-repo log directory ``<base>/<owner>/<name>/``; ``base_dir`` defaults to the platformdirs base."""
    base = (
        Path(base_dir)
        if base_dir is not None
        else Path(platformdirs.user_log_dir("shipit"))
    )
    return base / repo.owner.login / repo.name


def log_file_path(
    repo: Repo,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """The absolute path to the active log FILE: ``<base>/<owner>/<name>/shipit.log``."""
    return resolve_log_dir(repo, base_dir=base_dir) / LOG_FILENAME


def build_file_handler(
    repo: Repo,
    *,
    base_dir: str | Path | None = None,
) -> RotatingFileHandler:
    """The durable per-repo rotating JSONL file sink, at DEBUG; the directory is created on demand."""
    log_dir = resolve_log_dir(repo, base_dir=base_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / LOG_FILENAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.set_name(_FILE_HANDLER_NAME)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_file_formatter())
    return handler


def _current_repo() -> Repo:
    """The canonical repo for the current checkout, derived from the origin remote; raises when there is none."""
    return identity.resolve_repo()


def configure_logging_for_slug(
    slug: str,
    *,
    verbose: bool = False,
    base_dir: str | Path | None = None,
) -> bool:
    """Attach the file sink for a known ``owner/repo`` slug; returns whether it was attached, swallowing every failure."""
    try:
        repo = identity.repo_from_slug(slug)
        configure_logging(verbose=verbose, repo=repo, base_dir=base_dir)
        return True
    except Exception:  # noqa: BLE001 - logging setup must never crash the review
        return False


def rebind_own_tree(env: Mapping[str, str]) -> None:
    """Re-key ``tree``/``agent`` onto the Tree this process is actually running in when it is NOT the Tree the parent exported.

    ``agent`` takes the Tree id, as `shipit spawn subagent` binds it; the
    inherited ``session`` is kept, being genuinely shared. Never raises, and
    does nothing when the two agree or when there is no Tree. A native subagent
    inherits its spawner's export, which is what this separates — docs/adr/0083.
    """
    inherited = (env.get(logcontext.ENV_PREFIX + "TREE") or "").strip()
    if not inherited:
        return
    try:
        tree = session_current.containing_tree(Path.cwd())
    except (OSError, ValueError):
        return
    if tree is None or str(tree) == inherited:
        return
    leaf = layout.parse_flat_leaf(tree.name)
    if leaf is None:
        return
    logcontext.bind(tree=str(tree), agent=leaf.tree_id)


def _clear_own_handlers(logger: logging.Logger) -> None:
    """Detach and close only the handlers this module attached, keyed on the ``shipit-`` name prefix."""
    for handler in list(logger.handlers):
        if (handler.name or "").startswith(_HANDLER_PREFIX):
            logger.removeHandler(handler)
            handler.close()


def reset_logging() -> None:
    """Detach shipit's own sinks from the package logger — the clean slate each invocation starts from."""
    _clear_own_handlers(logging.getLogger(LOGGER_NAME))


def configure_logging(
    verbose: bool = False,
    env: Mapping[str, str] | None = None,
    *,
    repo: Repo | None = None,
    base_dir: str | Path | None = None,
) -> None:
    """Configure the ``shipit`` logger and attach its sinks; safe to call repeatedly, and it rebinds any parent-exported domain keys from ``env``."""
    env = os.environ if env is None else env

    logcontext.bind_from_env(env)
    rebind_own_tree(env)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    _clear_own_handlers(logger)

    logger.addHandler(build_console_handler(verbose=verbose))

    if is_ci(env):
        logger.addHandler(build_ci_handler())
        summary_path = env.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                logger.addHandler(build_step_summary_handler(summary_path))
            except OSError:
                logger.debug(
                    "could not open GITHUB_STEP_SUMMARY at %s; "
                    "skipping step-summary sink",
                    summary_path,
                )

    if repo is not None or base_dir is not None:
        if repo is None:
            repo = _current_repo()
        try:
            logger.addHandler(build_file_handler(repo, base_dir=base_dir))
        except OSError:
            logger.warning(
                "per-repo log file unavailable under %s; continuing without "
                "the durable file sink",
                resolve_log_dir(repo, base_dir=base_dir),
                exc_info=True,
            )
