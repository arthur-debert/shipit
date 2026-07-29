"""``harness/eval/store`` — append-only JSONL stores, one per kind, keyed by Repo identity."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import platformdirs

if TYPE_CHECKING:
    from ...identity import Repo

logger = logging.getLogger("shipit.harness")

EVAL_KIND = "eval"

REVIEW_ROUNDS_KIND = "review-rounds"


def lock_exclusive(fh: IO[Any]) -> None:
    """Take an exclusive advisory lock on ``fh``; the kernel drops it when ``fh`` closes."""
    if sys.platform == "win32":
        return
    # `fcntl` is unix-only and this module sits on the CLI's import chain, so the
    # import stays inside the function. Windows is a no-op: single writer assumed.
    import fcntl

    fcntl.flock(fh, fcntl.LOCK_EX)


def lock_shared(fh: IO[Any]) -> None:
    """Take a shared advisory lock on ``fh``; same platform contract as :func:`lock_exclusive`."""
    if sys.platform == "win32":
        return
    import fcntl

    fcntl.flock(fh, fcntl.LOCK_SH)


def store_dir(base_dir: Path | None = None, *, kind: str = EVAL_KIND) -> Path:
    """The store root for one record ``kind``; ``base_dir`` overrides the family root."""
    if base_dir is not None:
        return Path(base_dir) / kind
    return Path(platformdirs.user_state_dir("shipit")) / kind


def repo_key(repo: Repo) -> str:
    """A repo's store key: its origin identity as a nested, collision-free ``<owner>/<name>``."""
    return f"{_slug(repo.owner.login)}/{_slug(repo.name)}"


def _slug(text: str) -> str:
    seps = {os.sep, os.altsep, ":", "/"} - {None}
    for sep in seps:
        text = text.replace(sep, "-")
    return text.strip("-") or "_"


def store_path(
    repo: Repo, base_dir: Path | None = None, *, kind: str = EVAL_KIND
) -> Path:
    return store_dir(base_dir, kind=kind) / f"{repo_key(repo)}.jsonl"


def append_record(
    record: dict[str, Any],
    repo: Repo,
    base_dir: Path | None = None,
    *,
    kind: str = EVAL_KIND,
) -> Path:
    """Append one JSONL line under an exclusive lock that close drops AFTER its final flush."""
    path = store_path(repo, base_dir, kind=kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        lock_exclusive(fh)
        fh.write(json.dumps(record) + "\n")
        fh.flush()
    return path


def read_records(
    repo: Repo, base_dir: Path | None = None, *, kind: str = EVAL_KIND
) -> list[dict[str, Any]]:
    """Every record of one ``kind``, oldest first; a missing store is empty, not an error."""
    path = store_path(repo, base_dir, kind=kind)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        lock_shared(fh)
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
