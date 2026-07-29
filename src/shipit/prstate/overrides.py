"""Severity overrides — the write-once correction store for finding
severities and the top rung of the precedence chain; the dev-cycle event
log is the durable record."""

from __future__ import annotations

import logging
from pathlib import Path

from .. import events
from ..finding import Severity, parse_severity
from ..identity import Repo
from ..logread.records import parse_record
from ..logsetup import log_file_path
from .errors import PrStateError

logger = logging.getLogger("shipit.prstate")

OVERRIDE_EVENT = "finding.severity_overridden"


def _log_files(repo: Repo, base_dir: str | Path | None = None) -> list[Path]:
    """The per-repo JSONL log file plus its rotated backups, oldest first —
    an override recorded a while ago can live in a backup."""
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
    """The recorded Severity overrides for ``pr``: comment id -> Severity.

    A missing log file is no overrides, never an error; ``base_dir``
    overrides the platformdirs log base.
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
    """Record one Severity override for ``comment_id`` on ``pr``; a
    re-override is refused, since an override is immutable once recorded."""
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
