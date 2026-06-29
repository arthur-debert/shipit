"""Local eval store — append eval records to a harness-owned, never-committed file.

The store is JSONL, append-only, **keyed by repo**, and lives OUTSIDE every repo
working tree: under platformdirs' user *state* dir (`~/Library/Application
Support/shipit/eval` on macOS, `~/.local/state/shipit/eval` on Linux), the same
`platformdirs`-rooted convention `logsetup` uses. So process telemetry never
dirties product history — a written record can never show up as a repo change
(docs/prd/har02-run-eval.md, ADR-0013: "local, never committed").

``base_dir`` is the eval-store root itself, injected by tests (mirroring
:mod:`shipit.logsetup`, whose ``resolve_log_dir`` returns an injected ``base_dir``
verbatim) so they write to a tmp path. It is returned as-is — the ``eval``
suffix is appended only when computing the *default* root from platformdirs, so
an injected ``base_dir`` is already the leaf the store writes under.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import platformdirs


def store_dir(base_dir: Path | None = None) -> Path:
    """The eval store's root directory (outside any repo tree).

    ``base_dir`` IS that root when given (returned verbatim, for tests); otherwise
    the root is ``platformdirs.user_state_dir("shipit")/eval``. The ``eval`` suffix
    belongs to the platformdirs default only — an injected ``base_dir`` is the leaf.
    """
    if base_dir is not None:
        return Path(base_dir)
    return Path(platformdirs.user_state_dir("shipit")) / "eval"


def repo_key(repo_root: str | Path) -> str:
    """A filesystem-safe key for a repo — its absolute path slugified.

    Mirrors Claude Code's own project-dir convention (path separators → ``-``) so
    one repo's runs pool into one store file and distinct repos never collide. As
    well as the platform path separator(s) — ``os.sep`` and, where the platform has
    one, ``os.altsep`` — the drive-letter colon is slugified too: a Windows absolute
    path like ``C:\\repo`` carries a ``:``, which is not a legal filename character,
    so leaving it would break the store write.
    """
    seps = {os.sep, os.altsep, ":"} - {None}
    slug = str(Path(repo_root).resolve())
    for sep in seps:
        slug = slug.replace(sep, "-")
    slug = slug.strip("-")
    return slug or "_"


def store_path(repo_root: str | Path, base_dir: Path | None = None) -> Path:
    """The JSONL store file for ``repo_root``."""
    return store_dir(base_dir) / f"{repo_key(repo_root)}.jsonl"


def append_record(
    record: dict[str, Any], repo_root: str | Path, base_dir: Path | None = None
) -> Path:
    """Append one eval record as a JSONL line to the repo's store; return its path.

    Creates the store directory on first write. Returns the path so the caller (and
    tests) can assert where the record landed.
    """
    path = store_path(repo_root, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path
