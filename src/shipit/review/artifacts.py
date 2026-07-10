"""artifacts — the per-run artifact bundle for review sub-agent runs (RVW03-WS02).

Every review sub-agent run — a round-1 **Dimension pass**, an incremental
fix-range pass, the **Calibrator**, an offline range replay — persists a
per-run artifact bundle UNCONDITIONALLY, success and failure alike: the EXACT
prompt text sent (``prompt.txt``), the raw captured streams (``stdout.raw``,
``stderr.raw``), and a machine-readable meta record (``meta.json`` — backend,
model, argv, exit code, duration, timed-out flag, variant hash, run id). The
recurring situation this exists for (issue #681): a multi-minute multi-pass
round fails or under-delivers and the coordinator must answer — from disk,
without re-running — "what was pass X sent, what did it emit, how did it exit,
how long did it take".

Bundles live BESIDE the round store, under the same harness-owned,
never-committed state root (:func:`shipit.harness.eval.store.store_dir` — the
platformdirs user-state family root, ADR-0013 "local, never committed"), keyed
by the same origin :class:`~shipit.identity.Repo` identity (ADR-0024)::

    <family-root>/review-artifacts/<owner>/<name>/<round_id>/<run_or_name>/

so one injected ``base_dir`` covers the round store AND its bundles, and a
round record's ``round.artifacts`` points at the ``<round_id>`` directory its
``round.runs[*].artifacts`` entries live under. Each run writes only its OWN
directory — bundle writes never share a file, so they need no cross-process
lock (the round-store locking is WS03's, deliberately not re-solved here).

FAIL-OPEN, like every telemetry tee: an unwritable bundle (read-only state
root, resolution failure) logs a WARNING and the review proceeds untouched —
:class:`RunArtifacts` with ``dir=None`` is the DISABLED sink whose every write
no-ops, so callers thread one object and never branch. Meta writes ACCRETE
(:meth:`RunArtifacts.record` merges and rewrites ``meta.json``): each layer —
the launch seam knows argv/exit/streams, the fan-out knows run identity and
outcome — records what it knows when it knows it, so a crash between layers
still leaves everything written so far on disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..harness.eval.store import repo_key, store_dir
from ..identity import repo_from_slug

logger = logging.getLogger("shipit.review")

#: The bundle tree's kind directory under the shared state-family root —
#: a sibling of :data:`shipit.harness.eval.store.REVIEW_ROUNDS_KIND` (this one
#: holds per-run DIRECTORIES, not a JSONL record file, but it shares the root
#: so the never-committed / repo-keyed / test-injectable properties are the
#: store family's, stated once).
ARTIFACTS_KIND = "review-artifacts"

#: The bundle's fixed member files.
PROMPT_FILENAME = "prompt.txt"
STDOUT_FILENAME = "stdout.raw"
STDERR_FILENAME = "stderr.raw"
META_FILENAME = "meta.json"


def round_root(
    repo_slug: str | None, round_id: str, *, base_dir: Path | None = None
) -> Path | None:
    """The artifact directory ONE review round's bundles live under, or ``None``.

    ``<family-root>/review-artifacts/<owner>/<name>/<round_id>`` — resolved
    from the canonical ``owner/name`` slug through the ONE parser
    (:func:`shipit.identity.repo_from_slug`) and the store family's
    :func:`~shipit.harness.eval.store.repo_key`, so bundles pool per repo
    IDENTITY exactly like the round store they sit beside (ADR-0024).
    ``base_dir`` overrides the family root (tests), as on the store.

    FAIL-OPEN: a missing/malformed slug (a hand-built ctx) returns ``None`` —
    the disabled-sink cue — with a WARNING, never an error; bundles are
    telemetry and must not degrade the review they observe.
    """
    slug = (repo_slug or "").strip()
    if not slug:
        logger.warning(
            "review artifact bundle disabled for round %s: no repo identity",
            round_id,
        )
        return None
    try:
        repo = repo_from_slug(slug)
    except ValueError:
        logger.warning(
            "review artifact bundle disabled for round %s: unusable repo slug %r",
            round_id,
            slug,
            exc_info=True,
        )
        return None
    return store_dir(base_dir, kind=ARTIFACTS_KIND) / repo_key(repo) / round_id


class RunArtifacts:
    """The fail-open bundle writer for ONE review sub-agent run.

    ``dir`` is the run's own bundle directory, or ``None`` for the DISABLED
    sink (no resolvable repo identity): every write on a disabled sink no-ops,
    so callers thread one object unconditionally. Writes create the directory
    on demand and NEVER raise — any OS failure is logged at WARNING and
    swallowed (telemetry must not degrade the run it observes).

    :meth:`record` accretes ``meta.json``: fields merge into the in-memory
    meta and the file is rewritten whole, so each layer (the launch seam, the
    fan-out) records its facts as they become known and a crash between layers
    loses nothing already recorded. Values that JSON cannot carry degrade to
    ``repr`` rather than failing the write.
    """

    def __init__(self, dir: Path | None) -> None:  # noqa: A002 - the bundle dir IS the identity
        self.dir = dir
        self._meta: dict[str, Any] = {}

    @classmethod
    def disabled(cls) -> RunArtifacts:
        """The no-op sink — for callers with nothing to key a bundle by."""
        return cls(None)

    @classmethod
    def under(cls, round_dir: Path | None, name: str) -> RunArtifacts:
        """The bundle for one run named ``name`` under a round's root — the
        fan-out's minting path (``name`` is the pass's run id, or the fixed
        ``calibrator`` — one judge per round, its true run id is known only
        after the launch and lands in the meta). A ``None`` ``round_dir``
        (disabled round) yields the disabled sink."""
        return cls(None if round_dir is None else Path(round_dir) / name)

    def write_prompt(self, text: str) -> None:
        """Persist the EXACT prompt text the run launches with — written BEFORE
        the launch, so even a hung/killed child leaves its prompt inspectable."""
        self._write(PROMPT_FILENAME, text)

    def write_streams(self, stdout: str | None, stderr: str | None) -> None:
        """Persist the raw captured streams — success, nonzero exit, and the
        timeout's partial output alike (the full raw a truncated ``detail``
        string can never carry)."""
        self._write(STDOUT_FILENAME, stdout or "")
        self._write(STDERR_FILENAME, stderr or "")

    def record(self, **fields: Any) -> None:
        """Merge ``fields`` into the bundle's ``meta.json`` and rewrite it.

        Accretive: the launch seam records argv/exit-code/duration/timed-out;
        the fan-out records run identity (run id, kind, dimension, backend,
        model, variant) and the settled outcome. ``None`` values are recorded
        as written (an explicit unknown is data here, unlike log extras).
        """
        self._meta.update(fields)
        self._write(
            META_FILENAME,
            json.dumps(self._meta, indent=2, sort_keys=True, default=repr) + "\n",
        )

    def _write(self, filename: str, content: str) -> None:
        """One member-file write — directory on demand, FAIL-OPEN."""
        if self.dir is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / filename).write_text(content, encoding="utf-8")
        except OSError:
            logger.warning(
                "review artifact write failed for %s (the run is unaffected)",
                self.dir / filename,
                exc_info=True,
            )
