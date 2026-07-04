"""Central logging configuration for shipit — the observability spine's entrypoint.

Named ``logsetup`` (NOT ``logging``) so it never shadows the stdlib module. It
configures the package logger ``logging.getLogger("shipit")`` and attaches the
sinks shipit logs through. Each sink lives in its own builder so the wiring in
:func:`configure_logging` is a simple, additive union.

Three sinks, chosen for where shipit runs (PRD ``docs/prd/obs01-logging.md``):

- **Console** — quiet by default (WARNING+ to stderr), so the user-facing surface
  is unchanged in spirit from today. ``-v/--verbose`` raises it to DEBUG so an
  interactive debugging session can watch detail live.
- **CI** — when a CI environment is detected, a stderr handler so the run's record
  lands in the job log (DEBUG-level, the durable artifact CI keeps) while leaving
  stdout reserved for command / ``--json`` output; and, when
  ``$GITHUB_STEP_SUMMARY`` is present, a best-effort handler that appends records
  to that file.
- **File** — the durable, per-repo, rotating diagnosis record. Path resolution is
  :func:`platformdirs.user_log_dir` — the single source of truth (no platform
  ``if`` branches, no bespoke override env var) — namespaced ``<base>/<owner>/<name>/``
  by the canonical :class:`shipit.identity.Repo` (lowercased, so case-varying
  sources land ONE directory per repo) and bounded by a
  :class:`~logging.handlers.RotatingFileHandler`. The base and the ``Repo``
  namespace are injectable so tests cross the boundary without writing to a real
  ``$HOME``.

The file sink emits **JSONL** (ADR-0029, agents-first): one JSON object per
record with flat top-level fields — ``ts`` (ISO-8601 UTC), ``level``,
``logger``, ``msg``, plus any bound domain keys, present-when-bound (absent,
not null). The console / CI surfaces stay human-formatted. Both renderings
hang off the ONE processor pipeline (:data:`_PIPELINE`: context-merge →
enrich → redact seam), applied per sink by structlog's
:class:`~structlog.stdlib.ProcessorFormatter` — attached as each handler's
formatter, so untouched stdlib ``logging.getLogger`` call sites participate
via the foreign-record chain and only the final renderer differs.

The three level controls are independent: the file sink is always verbose
(DEBUG); the console is quiet unless ``-v``; the CI sink is verbose. Every
handler this module attaches carries a ``shipit-`` name prefix so a repeated
:func:`configure_logging` call replaces exactly its own handlers and never
double-attaches, while leaving any foreign handler alone.

**What a record looks like — the spray canon (LOG02).** Every module logs on a
``shipit.*`` logger to these conventions, fixed by the glassbox PRD and settled
across the codebase by the LOG02 spray + convergence (#245/#285); the mechanical
parts are ENFORCED by ``tests/test_logging_adoption_scoped.py``'s sweeps, so a
new module inherits them by test failure, not by memory:

- **Levels.** Lifecycle milestones at INFO (with ``duration_ms`` where
  meaningful); mechanics at DEBUG; degraded-but-continuing outcomes at WARNING;
  failures that propagate at ERROR with the exception attached. User-facing
  verb output remains ``print``/``echo`` — but anything that is the ONLY record
  of an action must also log.
- **Event names.** The human ``msg`` is a domain phrase — domain noun +
  past-tense/imperative ("tree created …", "review posted …") — NEVER a code
  identifier (no ``function_name:`` / ``module.attr:`` prefixes).
- **PR identity.** A PR renders as ``pr#N`` in messages (never ``pr=#N`` or
  ``owner/repo#N``), and the ``pr`` domain key is bound (:mod:`shipit.logcontext`)
  or passed in ``extra`` wherever the number is known, so the record is
  jq-sliceable as well as readable.
- **Exceptions.** Attach via ``exc_info=True`` — never ``exc_info=<instance>``,
  and never ALSO interpolated into the message text (the formatter renders the
  traceback; interpolation duplicates it).
- **Redaction.** Never mask per call site: the :func:`shipit.redact.redact_event`
  step in :data:`_PIPELINE` masks every record, on every sink, at format time
  (#277). The one deliberate exception is :class:`shipit.execrun.ExecError`,
  redacted at construction because the object surfaces to callers OUTSIDE the
  logging chain.
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

# --------------------------------------------------------------------------
# The processor pipeline — the ONE chain both renderings share (ADR-0029)
# --------------------------------------------------------------------------

#: The one processor pipeline every sink shares — context-merge
#: (:func:`shipit.logcontext.merge_domain_keys`: the bound DOMAIN keys land on
#: the record, absent when unbound — and ONLY the domain keys, so a stray
#: contextvar bound outside :mod:`shipit.logcontext` can never mint a field
#: beyond the closed correlation vocabulary) → stdlib ``extra=`` adoption
#: (:class:`~structlog.stdlib.ExtraAdder` — a plain
#: ``logger.info(..., extra={"phase": ...})`` lands ``phase`` as a flat event
#: extra; without it ``ProcessorFormatter`` drops LogRecord extras) →
#: enrichment (``logger``, ``level``, ISO-8601-UTC ``ts``, exceptions
#: flattened to a string) → the central redactor (:mod:`shipit.redact`,
#: ADR-0028/0029: secretsrc-registered values and token/PEM patterns masked in
#: ``msg`` and extras — placed last so context and extras are masked too).
#: Applied via :class:`~structlog.stdlib.ProcessorFormatter`'s
#: ``foreign_pre_chain``, so records from untouched stdlib ``logging`` call
#: sites (all of shipit today) flow through it; only the renderer differs per
#: sink (JSONL for the file, human for the surfaces).


#: Attributes stdlib ``Formatter.format`` plants ON the shared ``LogRecord``
#: as a side effect of rendering. The pipeline runs once per handler against
#: that one record, so by the second handler these look exactly like user
#: extras to :class:`~structlog.stdlib.ExtraAdder` — strip them, or every
#: multi-sink record grows a duplicate ``message`` field.
_STDLIB_FORMAT_ARTIFACTS = ("message", "asctime")

_EXTRA_ADDER = structlog.stdlib.ExtraAdder()


def _add_stdlib_extras(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Adopt stdlib ``extra={...}`` fields into the record — the supported
    call-site idiom is untouched stdlib logging, and without this step
    :class:`~structlog.stdlib.ProcessorFormatter` silently drops LogRecord
    extras. :class:`~structlog.stdlib.ExtraAdder` does the copying; the
    wrapper removes stdlib formatting artifacts (see
    :data:`_STDLIB_FORMAT_ARTIFACTS`) that an earlier handler's ``format()``
    call left on the shared record.
    """
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
    """Enforce the flat-record contract (ADR-0029) at the JSONL render seam.

    Any value that is not a JSON scalar (``str``/``int``/``float``/``bool``/
    ``None``) degrades to its ``repr`` — so a bound container (dict, list,
    tuple, …) can never nest the record, and a non-serializable object can
    never crash the log call. One mechanism covers both, which is why the
    renderer below needs no ``default=`` escape hatch.
    """
    for key, value in event_dict.items():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            event_dict[key] = repr(value)
    return event_dict


def _file_formatter() -> logging.Formatter:
    """The JSONL renderer for the file sink: one flat JSON object per record.

    ``event`` is renamed to ``msg`` (the contract's human-readable message
    field) — and in the same step a dev-cycle event tag riding the record as
    :data:`shipit.events.EXTRA_KEY` lands as the durable ``event`` field
    (ADR-0032; ``EventRenamer``'s ``replace_by`` exists for exactly this
    message-key/custom-``event`` swap, and handles the tag's absence — the
    common case — gracefully). Every value is forced to a JSON scalar
    (:func:`_flatten_to_scalars` — flat fields, nothing nested, contract
    enforced rather than assumed), and unbound keys are simply absent.
    """
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
    """Render a processed record for the human surfaces (console / CI).

    Preserves the historical shape — ``LEVEL logger: message`` — with any bound
    domain keys appended as ``key=value`` and an exception's traceback on the
    following lines (mirroring stdlib formatting). No timestamp: the terminal
    is live; the durable timestamped record is the file sink's job.
    """
    level = str(event_dict.pop("level", "")).upper()
    name = event_dict.pop("logger", "")
    message = event_dict.pop("event", "")
    # A dev-cycle event tag (ADR-0032) shows on the surfaces under its durable
    # name too — the message key is free now, so the same swap the file sink's
    # EventRenamer performs is one rename here.
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
    """The human-format renderer shared by the console / CI surface sinks —
    the same :data:`_PIPELINE` as the file sink, differing only in the final
    render step."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_PIPELINE,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _render_surface,
        ],
    )


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


def build_ci_handler() -> logging.Handler:
    """Build the CI handler so the run's record lands in the job log.

    Streams to **stderr**, not stdout: GitHub Actions captures both streams into
    the job log, so the run's record lands there either way — and routing to
    stderr keeps stdout reserved for command / ``--json`` output, which a record
    on stdout would interleave with and corrupt.

    Captures DEBUG and up: in CI the job log *is* the durable run record (per the
    PRD), so it carries the full verbose detail, not just INFO+.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_surface_formatter())
    handler.set_name(_HANDLER_PREFIX + "ci")
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
    repo: Repo,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """The per-repo log directory ``<base>/<owner>/<name>/``.

    ``repo`` is the canonical :class:`shipit.identity.Repo` — its lowercased
    owner/name are the path segments, so mixed-case origins and API slugs land ONE
    log directory per repo (ADR-0024). ``base_dir`` is the platformdirs base; when
    ``None`` it is resolved via ``platformdirs.user_log_dir("shipit")`` (macOS →
    ``~/Library/Logs/shipit``, Linux → ``~/.local/state/shipit/log``). Tests inject
    ``base_dir`` (and the ``repo``) so the path is asserted without touching a real
    ``$HOME``.
    """
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
    """The absolute path to the active log FILE: ``<base>/<owner>/<name>/shipit.log``.

    The single source of truth for the concrete log file — the directory from
    :func:`resolve_log_dir` joined with :data:`LOG_FILENAME` (the basename the
    :class:`~logging.handlers.RotatingFileHandler` writes). Readers (``shipit
    logs``) consume THIS rather than recomputing the platformdirs path, so the
    reader can never disagree with the writer about where the log lives.
    """
    return resolve_log_dir(repo, base_dir=base_dir) / LOG_FILENAME


def build_file_handler(
    repo: Repo,
    *,
    base_dir: str | Path | None = None,
) -> RotatingFileHandler:
    """The durable per-repo rotating file sink — the diagnosis record.

    A :class:`~logging.handlers.RotatingFileHandler` bounded at :data:`MAX_BYTES`
    × :data:`BACKUP_COUNT` so it rolls over rather than growing without limit. It
    emits at ``DEBUG`` (the verbose record), independent of the console level, as
    JSONL (:func:`_file_formatter` — one flat JSON object per record, ADR-0029).
    The per-repo directory is created on demand.
    """
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
    """The canonical :class:`shipit.identity.Repo` for the current checkout.

    Derived LOCALLY from the origin remote (:func:`shipit.identity.resolve_repo`,
    ADR-0024) — offline and Tree-safe, with owner/name lowercased so every case
    variant of one repo writes ONE log directory. No origin remote or an
    unparseable URL is a real failure — fail loud rather than silently writing to
    an empty/incorrect log directory.
    """
    return identity.resolve_repo()


def configure_logging_for_slug(
    slug: str,
    *,
    verbose: bool = False,
    base_dir: str | Path | None = None,
) -> bool:
    """Wire the per-repo file sink from a KNOWN ``owner/repo`` slug — best-effort.

    The detached review child (OBS03) knows its repo DETERMINISTICALLY from its
    ``--repo`` argument, so — unlike the CLI bootstrap, which resolves the ambient
    identity best-effort off cwd (the ADR-0030 root context, which can degrade
    outside a checkout) — it can attach the file sink with certainty. The child passes that
    slug here — parsed by the ONE canonical parser
    (:func:`shipit.identity.repo_from_slug`), so an API-cased slug lands the same
    log directory as the locally-resolved identity (ADR-0024) — so the detached
    run's diagnostics can reach ``<logdir>/<owner>/<name>/shipit.log``, independent
    of cwd resolution — this is what attempts to make good on OBS03 story 5 (a
    crashed detached run should leave a durable "why", not just a terminal check
    run). Best-effort, not a hard guarantee: see the return contract below.

    Returns whether the file sink was attached. Best-effort: a malformed slug or a
    logging-setup failure is swallowed (returns ``False``) — a logging glitch must
    NEVER crash the review (mirrors the CLI root's best-effort posture).
    ``base_dir`` is the platformdirs base, injected by tests so the child's records
    are asserted without writing to a real ``$HOME``.
    """
    try:
        repo = identity.repo_from_slug(slug)
        configure_logging(verbose=verbose, repo=repo, base_dir=base_dir)
        return True
    except Exception:  # noqa: BLE001 - logging setup must never crash the review
        return False


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


def reset_logging() -> None:
    """Detach shipit's own sinks from the package logger — the clean-slate the
    CLI bootstrap assumes.

    The CLI resolves identity BEFORE it wires sinks (:func:`configure_logging`
    runs late so the file sink can be per-repo), which means the bootstrap phase
    — the git-``exec`` identity resolution — must run with NO sink attached to
    stay quiet, as the ``root`` entrypoint's contract intends. A one-shot
    production process starts that way. But when several invocations share ONE
    process (the test suite, or any in-process embedding), a prior invocation's
    sinks are STILL attached when the next one resolves identity, so that
    invocation's pre-config bootstrap records (the ``exec`` DEBUG lines) leak to
    the earlier run's pinned stderr sink. Calling this at the very top of every
    invocation restores the clean slate; in a one-shot process it is a no-op.
    """
    _clear_own_handlers(logging.getLogger(LOGGER_NAME))


def configure_logging(
    verbose: bool = False,
    env: Mapping[str, str] | None = None,
    *,
    repo: Repo | None = None,
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
      to ``os.environ``): a stderr handler (stdout stays clean for ``--json``
      output), plus a best-effort ``$GITHUB_STEP_SUMMARY`` appender.
    - **File** — attached when a target repo is known, i.e. when ``repo`` (a
      canonical :class:`shipit.identity.Repo`) or ``base_dir`` is provided.
      ``repo`` / ``base_dir`` are injectable boundaries for tests; with
      ``base_dir`` given but ``repo`` omitted, the repo is resolved (strictly) via
      :func:`shipit.identity.resolve_repo`. The CLI entrypoint resolves the ambient
      identity best-effort (the ADR-0030 root context) and passes its repo, so a
      normal run gets the file sink and a non-repo run simply skips it.

    Logging setup is also the child half of the cross-process context seam
    (ADR-0029): any domain keys a parent shipit process exported into ``env``
    (``SHIPIT_LOG_CTX_*``, :func:`shipit.logcontext.env_export`) are rebound here,
    so a detached/spawned child's records carry the parent's ``pr``/``repo``/…
    from its first record on — and an explicitly-exported key wins over anything
    the child bound off its own best-effort cwd resolution before this call.
    """
    env = os.environ if env is None else env

    # Rebind parent-exported domain keys FIRST (the child half of the ADR-0029
    # env seam) so every sink attached below — and every record after — carries
    # them. No-op when the environment carries no SHIPIT_LOG_CTX_* vars.
    logcontext.bind_from_env(env)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    _clear_own_handlers(logger)

    # Console sink — always on, quiet by default.
    logger.addHandler(build_console_handler(verbose=verbose))

    # CI sinks — only when we detect a CI environment.
    if is_ci(env):
        logger.addHandler(build_ci_handler())
        summary_path = env.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            # The step-summary sink is best-effort: if the path can't be opened
            # (missing dir, permissions, …) we keep the CI sink and carry
            # on rather than fail the command — a logging glitch never blocks.
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
    # Best-effort, like the step-summary sink above: an unopenable log path
    # (read-only home, permissions) degrades to console-only rather than
    # crashing the command — a logging glitch never blocks, and for the
    # hook-witnessed tier (post-commit → `shipit log event`, LOG04/ADR-0032) a
    # broken log path must never block git. The swallow is a
    # degraded-but-continuing outcome → WARNING with the exception attached,
    # per the fail-open canon.
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
