"""Local record store family — append harness records to harness-owned, never-committed files.

ONE store family, two record kinds (RVW02-WS03 generalized the eval-only helpers
rather than duplicating them): the **eval record** kind (:data:`EVAL_KIND`, how a
run *behaved*) and the **review-round record** kind (:data:`REVIEW_ROUNDS_KIND`,
what a review *concluded*). Every kind shares the same convention: JSONL,
append-only, **keyed by `Repo` identity**, living OUTSIDE every repo working tree
under platformdirs' user *state* dir (``~/Library/Application Support/shipit`` on
macOS, ``~/.local/state/shipit`` on Linux — the same `platformdirs`-rooted
convention `logsetup` uses), one subdirectory per kind (``…/shipit/eval``,
``…/shipit/review-rounds``). So process telemetry never dirties product history —
a written record can never show up as a repo change (docs/legacy-prd/har02-run-eval.md,
ADR-0013: "local, never committed").

The key is the repo's **origin `owner/name` identity** (:class:`shipit.identity.Repo`),
NOT the resolved filesystem path (ADR-0024): every Tree/clone of one repo pools into
ONE store file per kind, so `shipit eval report` joins a repo's runs (and its review
rounds) across every checkout instead of scattering one store per clone path.
**No compat**: pre-existing path-keyed stores simply orphan (local, uncommitted,
regenerable data).

Integrity (RVW03-WS03): appends are serialized under an exclusive ``flock`` so
parallel settles from separate processes can never interleave two records into
one malformed line; readers skip a malformed line LOUDLY (a warning naming the
file + 1-based line number), never silently.

``base_dir`` is the FAMILY root (the dir the per-kind subdirs live under), injected
by tests (mirroring :mod:`shipit.logsetup`, whose ``resolve_log_dir`` returns an
injected ``base_dir`` verbatim) so they write to a tmp path — ONE injected root
covers every kind, which is what lets a reader (the eval report's review-axis
join) resolve both stores from a single override.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import platformdirs

if TYPE_CHECKING:
    from ...identity import Repo

logger = logging.getLogger("shipit.harness")

#: The eval-record kind: one JSONL line per run, saying how the run *behaved*
#: (tool calls, tokens, stuck-loops — :mod:`shipit.harness.eval.record`).
EVAL_KIND = "eval"

#: The review-round-record kind: one JSONL line per review round, saying what the
#: review *concluded* (findings with severities and dispositions, coverage, the
#: range reviewed — :mod:`shipit.review.roundrecord`).
REVIEW_ROUNDS_KIND = "review-rounds"


def store_dir(base_dir: Path | None = None, *, kind: str = EVAL_KIND) -> Path:
    """The store root for one record ``kind`` (outside any repo tree).

    The family root is ``base_dir`` when given (verbatim, for tests) else
    ``platformdirs.user_state_dir("shipit")``; the kind's store root is the
    ``kind`` subdirectory under it — so the eval store's default root is
    unchanged (``…/shipit/eval``) and every other kind sits beside it.
    """
    if base_dir is not None:
        return Path(base_dir) / kind
    return Path(platformdirs.user_state_dir("shipit")) / kind


def repo_key(repo: Repo) -> str:
    """A collision-free, filesystem-safe key for a repo — its origin identity as a
    nested ``<owner>/<name>`` path.

    Keyed by :class:`shipit.identity.Repo` IDENTITY (origin owner + name), NOT the
    resolved filesystem path (ADR-0024): two clones of one repo at different paths
    produce the SAME key and so pool into one store file — the fix for the scatter
    bug where every Tree clone orphaned a fresh path-keyed store. ``OwnerKind`` is
    deliberately absent from the key (it is excluded from :class:`Repo` identity),
    so the key is stable whether or not the owner's kind has been enriched.

    The key is a **nested ``<owner>/<name>`` path**, the same origin-keyed scheme
    :mod:`shipit.logsetup` proved (``<base>/<owner>/<repo>/``). This is provably
    collision-free where a flat ``owner-name`` join is NOT: ``-`` is legal in both a
    GitHub owner login and a repo name, so owner ``a-b`` + name ``c`` and owner ``a``
    + name ``b-c`` both flatten to ``a-b-c`` and would silently merge two distinct
    repos' records into one file. A ``/`` separator can never collide because
    neither a GitHub owner login nor a repo name may contain ``/`` — each is one
    unambiguous path segment. Each component is still slugified
    belt-and-suspenders (any stray ``os.sep`` / ``os.altsep`` / drive ``:`` inside a
    component → ``-``) so a per-repo store write can never escape its segment.
    """
    return f"{_slug(repo.owner.login)}/{_slug(repo.name)}"


def _slug(text: str) -> str:
    """Slugify one key component: every path separator → ``-``, trimmed.

    Operates on a SINGLE ``<owner>``/``<name>`` segment (the structural ``/`` that
    separates them in :func:`repo_key` is added around slugged components, never
    within one), so ``/`` is folded here too — a stray separator inside a component
    must not spill into a second path segment.
    """
    seps = {os.sep, os.altsep, ":", "/"} - {None}
    for sep in seps:
        text = text.replace(sep, "-")
    return text.strip("-") or "_"


def store_path(
    repo: Repo, base_dir: Path | None = None, *, kind: str = EVAL_KIND
) -> Path:
    """The JSONL store file for ``repo``'s identity under one record ``kind``:
    ``<root>/<kind>/<owner>/<name>.jsonl``.

    The nested ``<owner>/<name>`` key (:func:`repo_key`) becomes a nested store
    file, so distinct repos never share a path (see the collision note there),
    and distinct kinds never share a file (the kind is a directory level, so an
    eval record can never land in a review-rounds store).
    """
    return store_dir(base_dir, kind=kind) / f"{repo_key(repo)}.jsonl"


def append_record(
    record: dict[str, Any],
    repo: Repo,
    base_dir: Path | None = None,
    *,
    kind: str = EVAL_KIND,
) -> Path:
    """Append one record as a JSONL line to the repo's ``kind`` store; return its path.

    Keyed by ``repo``'s origin identity (:func:`repo_key`), so a run's record lands
    under one stable per-repo file regardless of which clone it ran in. Creates the
    store directory on first write. Returns the path so the caller (and tests) can
    assert where the record landed.

    Appends are SERIALIZED under an exclusive file lock (``flock``, RVW03-WS03):
    parallel settles append to the same per-repo file from separate processes, and
    a record larger than the writer's buffer would otherwise flush interleaved
    with a concurrent append's chunks — splicing two records into one malformed
    line. The lock is held only for the single line's write + flush, and releases
    with the file handle even on error.
    """
    path = store_path(repo, base_dir, kind=kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return path


def read_records(
    repo: Repo, base_dir: Path | None = None, *, kind: str = EVAL_KIND
) -> list[dict[str, Any]]:
    """Read every record of one ``kind`` for ``repo``, oldest-appended first.

    The read sibling of :func:`append_record`: resolves the SAME origin-keyed
    store path (:func:`store_path`) and parses the JSONL back into dicts, in
    append order (the file is append-only, so line order is chronological). A
    MISSING store (nothing ever appended) is an empty list, never an error —
    a reader that has no history simply sees none (the incremental-round query
    that has no prior review-round record for a PR then treats the round as a
    full round, RVW02-WS06). A malformed line is skipped rather than fatal — the
    store is local, uncommitted telemetry, and one corrupt line must not blind a
    reader to every intact record around it — but it is skipped LOUDLY
    (RVW03-WS03): a warning names the file and the 1-based line number, so a
    corrupted round can never silently read as "this arm found nothing".

    ``base_dir`` overrides the family root (tests), exactly as on the writers.
    """
    path = store_path(repo, base_dir, kind=kind)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "malformed record skipped in %s, line %d: not valid JSON",
                    path,
                    lineno,
                    exc_info=True,
                )
                continue
            if not isinstance(parsed, dict):
                logger.warning(
                    "malformed record skipped in %s, line %d: expected a JSON "
                    "object, got %s",
                    path,
                    lineno,
                    type(parsed).__name__,
                )
                continue
            records.append(parsed)
    return records
