"""Finding verdicts — the recorded classifications the review loop turns on (#423).

The agent addressing a review round classifies every finding it addresses —
``nitpick`` or ``substantive`` — as a byproduct of triaging the thread. There
is NO auto-classification of any kind (the old ``_NITPICK_MARKERS`` body-regex
machinery is gone; a reviewer's own ``nit:`` tag is just input to the agent's
judgment). This module is the verdict STORE: the dev-cycle event log is the
durable record (ADR-0032 — one ``finding.classified`` event per verdict, keyed
by the finding comment's id), and this module owns both halves of that seam:

- :func:`record_verdict` — the ONE write path. Write-once: re-classifying an
  already-classified comment is an error (:class:`shipit.prstate.errors.
  PrStateError`), so a verdict is immutable once recorded.
- :func:`load_verdicts` — the ONE read path: the per-repo JSONL log filtered to
  this PR's ``finding.classified`` events, folded to ``comment id -> verdict``.
  The engine consumes this off the snapshot (``ReadinessView.verdicts``,
  threaded on at the gather seam), so the breaker and the classify gate read
  values, never the filesystem.

The verdict vocabulary is CLOSED (:data:`VERDICTS`): ``nitpick`` — cosmetic,
nothing that changes correctness or behaviour; ``substantive`` — everything
else. A round whose recorded verdicts are ALL nitpick stops the loop
(``breakers.is_all_nitpick_round``); a round with any unclassified finding
cannot advance past the CLASSIFY gate at all.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import events
from ..identity import Repo
from ..logread.records import parse_record
from ..logsetup import log_file_path
from .errors import PrStateError

#: The engine's logger (shared name across :mod:`shipit.prstate`): a recorded
#: verdict is a review-loop milestone, attributed where it happened.
logger = logging.getLogger("shipit.prstate")

#: The dev-cycle event one verdict lands as (registered in
#: :data:`shipit.events.EVENT_NAMES`); the reader selects on it.
VERDICT_EVENT = "finding.classified"

#: The closed verdict vocabulary. ``nitpick``: cosmetic — docstring/wording
#: fixes, style already settled, nothing that changes correctness or
#: behaviour. ``substantive``: everything else.
NITPICK = "nitpick"
SUBSTANTIVE = "substantive"
VERDICTS = (NITPICK, SUBSTANTIVE)


def _log_files(repo: Repo, base_dir: str | Path | None = None) -> list[Path]:
    """The per-repo JSONL log file plus its rotated backups, oldest first.

    The writer is a ``RotatingFileHandler`` (``shipit.log`` + ``shipit.log.N``
    backups, higher N = older), so a verdict recorded a while ago can live in a
    backup. Reading oldest-first keeps the fold chronological; write-once means
    a comment id can only ever carry one verdict anyway.
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


def load_verdicts(
    repo: Repo, pr: int, *, base_dir: str | Path | None = None
) -> dict[int, str]:
    """The recorded verdicts for ``pr``: finding comment id -> verdict.

    Reads the per-repo dev-cycle log (active file + rotated backups) and keeps
    every ``finding.classified`` record whose flat ``pr`` field matches —
    selection on the record's own identity fields, exactly how the log reader
    filters (ADR-0032). Write-once makes the fold order-insensitive in
    practice; ``setdefault`` keeps the FIRST record authoritative even if a
    duplicate ever slipped past the write guard (immutability wins). A missing
    log file — a fresh checkout, a repo never classified in — is simply no
    verdicts, never an error.

    ``base_dir`` overrides the platformdirs log base (tests inject a tmp dir;
    production omits it — the same seam :func:`shipit.logsetup.log_file_path`
    exposes).
    """
    out: dict[int, str] = {}
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
                if record.get(events.RECORD_KEY) != VERDICT_EVENT:
                    continue
                if record.get("pr") != pr:
                    continue
                comment = record.get("comment")
                verdict = record.get("verdict")
                if type(comment) is not int or verdict not in VERDICTS:
                    continue  # a malformed record cannot mint a verdict
                out.setdefault(comment, verdict)
    return out


def record_verdict(
    repo: Repo,
    pr: int,
    comment_id: int,
    verdict: str,
    *,
    reason: str | None = None,
    base_dir: str | Path | None = None,
) -> None:
    """Record ONE verdict for finding ``comment_id`` on ``pr`` — write-once.

    The single write path: validates the verdict against the closed
    :data:`VERDICTS` vocabulary, refuses a re-classification (the verdict is
    immutable once recorded — a changed mind is a bug in the process, not a
    supported edit), then emits the ``finding.classified`` dev-cycle event
    with the verdict's identity flat on the record (``pr`` / ``comment`` /
    ``verdict`` / optional ``reason``) so the reader selects on data. The
    durable write rides the ordinary logging pipeline — the same per-repo
    JSONL file every event lands in.
    """
    if verdict not in VERDICTS:
        raise PrStateError(
            f"unknown verdict {verdict!r} — a finding is classified "
            f"{' or '.join(VERDICTS)} (#423)"
        )
    existing = load_verdicts(repo, pr, base_dir=base_dir)
    if comment_id in existing:
        raise PrStateError(
            f"finding {comment_id} on PR #{pr} is already classified "
            f"{existing[comment_id]!r} — verdicts are written once and immutable"
        )
    extra: dict[str, object] = {"pr": pr, "comment": comment_id, "verdict": verdict}
    cleaned = reason.strip() if reason else ""
    if cleaned:
        extra["reason"] = cleaned.splitlines()[0]
    events.emit(
        logger,
        VERDICT_EVENT,
        "finding %d on pr#%d classified %s",
        comment_id,
        pr,
        verdict,
        extra=extra,
    )
