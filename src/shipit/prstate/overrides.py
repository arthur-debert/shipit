"""Severity overrides — the write-once correction store for finding severities.

Every finding resolves to a 4-tier :class:`~shipit.finding.Severity` through
the precedence chain (machine marker → reviewer-adapter mapping → the
adapter's unclassified-severity policy → ``major`` fail-safe —
:mod:`shipit.prstate.severity`; ADR-0044). This module is the chain's TOP
rung: a write-once **Severity override**, recorded when a reviewer-emitted
severity is judged wrong, beating every other source. It is a DORMANT
correction path — `shipit pr classify` is the only writer, and it is
deliberately absent from role prompts and operator-facing guidance (decision
records — ADR-0044 and the RVW02 PRD — still describe it). There is no
classification stage anywhere: an override corrects a severity; nothing needs
one to exist.

The dev-cycle event log is the durable record (ADR-0032 — one
``finding.severity_overridden`` event per override, keyed by the finding
comment's id), and this module owns both halves of that seam:

- :func:`record_override` — the ONE write path. Write-once: re-overriding an
  already-overridden finding is an error (:class:`shipit.prstate.errors.
  PrStateError`), so an override is immutable once recorded.
- :func:`load_overrides` — the ONE read path: the per-repo JSONL log filtered
  to this PR's ``finding.severity_overridden`` events, folded to ``comment id
  -> Severity``. The engine consumes this off the snapshot
  (``ReadinessView.overrides``, threaded on at the gather seam), so the breaker
  and the classify verb read values, never the filesystem.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import events
from ..finding import Severity, parse_severity
from ..identity import Repo
from ..logread.records import parse_record
from ..logsetup import log_file_path
from .errors import PrStateError

#: The engine's logger (shared name across :mod:`shipit.prstate`): a recorded
#: override is a review-loop milestone, attributed where it happened.
logger = logging.getLogger("shipit.prstate")

#: The dev-cycle event one override lands as (registered in
#: :data:`shipit.events.EVENT_NAMES`); the reader selects on it.
OVERRIDE_EVENT = "finding.severity_overridden"


def _log_files(repo: Repo, base_dir: str | Path | None = None) -> list[Path]:
    """The per-repo JSONL log file plus its rotated backups, oldest first.

    The writer is a ``RotatingFileHandler`` (``shipit.log`` + ``shipit.log.N``
    backups, higher N = older), so an override recorded a while ago can live in
    a backup. Reading oldest-first keeps the fold chronological; write-once
    means a comment id can only ever carry one override anyway.
    """
    active = log_file_path(repo, base_dir=base_dir)
    backups = sorted(
        (
            p
            for p in active.parent.glob(f"{active.name}.*")
            if p.suffix.lstrip(".").isdigit()
        ),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,  # highest suffix = oldest rollover
    )
    return [*backups, active]


def load_overrides(
    repo: Repo, pr: int, *, base_dir: str | Path | None = None
) -> dict[int, Severity]:
    """The recorded Severity overrides for ``pr``: finding comment id -> Severity.

    Reads the per-repo dev-cycle log (active file + rotated backups) and keeps
    every ``finding.severity_overridden`` record whose flat ``pr`` field
    matches — selection on the record's own identity fields, exactly how the
    log reader filters (ADR-0032). Write-once makes the fold order-insensitive
    in practice; ``setdefault`` keeps the FIRST record authoritative even if a
    duplicate ever slipped past the write guard (immutability wins). A missing
    log file — a fresh checkout, a repo never overridden in — is simply no
    overrides, never an error.

    ``base_dir`` overrides the platformdirs log base (tests inject a tmp dir;
    production omits it — the same seam :func:`shipit.logsetup.log_file_path`
    exposes).
    """
    out: dict[int, Severity] = {}
    for path in _log_files(repo, base_dir):
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                record = parse_record(line)
                if record is None:
                    continue
                if record.get(events.RECORD_KEY) != OVERRIDE_EVENT:
                    continue
                if record.get("pr") != pr:
                    continue
                comment = record.get("comment")
                severity = parse_severity(record.get("severity"))
                if type(comment) is not int or severity is None:
                    continue  # a malformed record cannot mint an override
                out.setdefault(comment, severity)
    return out


def record_override(
    repo: Repo,
    pr: int,
    comment_id: int,
    severity: Severity,
    *,
    reason: str | None = None,
    base_dir: str | Path | None = None,
) -> None:
    """Record ONE Severity override for finding ``comment_id`` on ``pr`` — write-once.

    The single write path: refuses a re-override (the override is immutable
    once recorded — a changed mind is a bug in the process, not a supported
    edit), then emits the ``finding.severity_overridden`` dev-cycle event with
    the override's identity flat on the record (``pr`` / ``comment`` /
    ``severity`` / optional ``reason``) so the reader selects on data. The
    durable write rides the ordinary logging pipeline — the same per-repo
    JSONL file every event lands in.
    """
    existing = load_overrides(repo, pr, base_dir=base_dir)
    if comment_id in existing:
        raise PrStateError(
            f"finding {comment_id} on PR #{pr} already carries the severity "
            f"override {existing[comment_id].value!r} — overrides are written "
            "once and immutable"
        )
    extra: dict[str, object] = {
        "pr": pr,
        "comment": comment_id,
        "severity": severity.value,
    }
    cleaned = reason.strip() if reason else ""
    if cleaned:
        extra["reason"] = cleaned.splitlines()[0]
    events.emit(
        logger,
        OVERRIDE_EVENT,
        "finding %d on pr#%d severity overridden to %s",
        comment_id,
        pr,
        severity.value,
        extra=extra,
    )
