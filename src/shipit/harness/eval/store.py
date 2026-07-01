"""Local eval store — append eval records to a harness-owned, never-committed file.

The store is JSONL, append-only, **keyed by `Repo` identity**, and lives OUTSIDE
every repo working tree: under platformdirs' user *state* dir (`~/Library/Application
Support/shipit/eval` on macOS, `~/.local/state/shipit/eval` on Linux), the same
`platformdirs`-rooted convention `logsetup` uses. So process telemetry never
dirties product history — a written record can never show up as a repo change
(docs/prd/har02-run-eval.md, ADR-0013: "local, never committed").

The key is the repo's **origin `owner/name` identity** (:class:`shipit.identity.Repo`),
NOT the resolved filesystem path (ADR-0024): every Tree/clone of one repo pools into
ONE store file, so `shipit eval report` joins a repo's runs across every checkout
instead of scattering one store per clone path. **No compat**: pre-existing
path-keyed stores simply orphan (local, uncommitted, regenerable data).

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
from typing import TYPE_CHECKING, Any

import platformdirs

if TYPE_CHECKING:
    from ...identity import Repo


def store_dir(base_dir: Path | None = None) -> Path:
    """The eval store's root directory (outside any repo tree).

    ``base_dir`` IS that root when given (returned verbatim, for tests); otherwise
    the root is ``platformdirs.user_state_dir("shipit")/eval``. The ``eval`` suffix
    belongs to the platformdirs default only — an injected ``base_dir`` is the leaf.
    """
    if base_dir is not None:
        return Path(base_dir)
    return Path(platformdirs.user_state_dir("shipit")) / "eval"


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


def store_path(repo: Repo, base_dir: Path | None = None) -> Path:
    """The JSONL store file for ``repo``'s identity: ``<root>/<owner>/<name>.jsonl``.

    The nested ``<owner>/<name>`` key (:func:`repo_key`) becomes a nested store
    file, so distinct repos never share a path (see the collision note there).
    """
    return store_dir(base_dir) / f"{repo_key(repo)}.jsonl"


def append_record(
    record: dict[str, Any], repo: Repo, base_dir: Path | None = None
) -> Path:
    """Append one eval record as a JSONL line to the repo's store; return its path.

    Keyed by ``repo``'s origin identity (:func:`repo_key`), so a run's record lands
    under one stable per-repo file regardless of which clone it ran in. Creates the
    store directory on first write. Returns the path so the caller (and tests) can
    assert where the record landed.
    """
    path = store_path(repo, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path
