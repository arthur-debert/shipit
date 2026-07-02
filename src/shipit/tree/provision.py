"""``tree/provision`` — which commits did *provisioning itself* make in this Tree?

The gc-stranding fix for drift-window provisioning commits (#232, ADR-0027). Tree
provisioning runs ``shipit install --local`` in the fresh clone; whenever the
repo's committed managed set lags the running shipit version (an upgrade drift
window), that install FAILS CLOSED into a reconcile commit
(``chore(shipit): install/update the managed set``) on the Tree's just-cut branch.
That commit exists on no remote, so the ephemeral gc ladder's rung-1 floor
("dirty ∨ unpushed → KEEP", :func:`shipit.tree.cleanup.classify`) — correctly
absolute for *work* — would hold an abandoned drift-window session Tree FOREVER:
even the 4-day hard cap requires "pushed".

The fix records the provisioning commit's IDENTITY at Tree birth so the ladder can
exclude **exactly it** — and nothing else — from the unpushed floor:

- :func:`write_record` stores the SHA(s) the install step produced in
  ``<tree>/.git/shipit-provision.json``, beside the ``session/liveness`` pidfile
  precedent and for the same reason: inside ``.git`` the record can never dirty
  the working tree (a tracked/untracked file would trip the very floor this
  exists to refine), and it dies with the Tree by construction.
- :func:`read_provision_shas` returns the recorded SHAs as the EXCLUSION SET the
  ladder subtracts, degrading every failure — no record, unreadable file,
  malformed JSON, mis-typed fields — to the EMPTY set: nothing excluded, so the
  floor keeps the Tree. Conservative by construction: this module can only ever
  *narrow* what counts as work, never widen what gc may delete.

Identity is the commit SHA, deliberately NOT the commit message: a message
heuristic would also match a rebased/amended descendant carrying real changes,
whereas a SHA mismatch (rebase, amend) simply fails the exclusion and falls back
to KEEP — the safe direction (#232 coordinator note).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("shipit.tree")

#: The record's name inside the clone's ``.git`` directory (see module docstring
#: for why it must NOT live in the working tree).
PROVISION_RECORD_NAME = "shipit-provision.json"

#: The record's one JSON field: the full SHAs of the commits provisioning made.
_COMMITS_KEY = "commits"


def record_path(tree: str | Path) -> Path:
    """Where ``tree``'s provision record lives: ``<tree>/.git/shipit-provision.json``."""
    return Path(tree) / ".git" / PROVISION_RECORD_NAME


def write_record(tree: str | Path, shas: Sequence[str]) -> None:
    """Record ``shas`` as the commits provisioning made in ``tree``.

    Called at Tree birth, only when the managed-set install actually committed
    (steady-state provisioning is a no-op and writes nothing — an absent record
    is the norm, not an error). Raises :class:`OSError` when the Tree has no
    ``.git`` directory to hold it or the write fails; the caller
    (:func:`shipit.tree.create._provision`) treats the record as additive and
    degrades to not-recorded — which the reader resolves to KEEP.
    """
    path = record_path(tree)
    if not path.parent.is_dir():
        raise FileNotFoundError(
            f"{path.parent} is not a directory — {tree} is not a git clone, "
            "so there is nowhere safe to record the provisioning commit"
        )
    payload = {_COMMITS_KEY: list(shas)}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_provision_shas(tree: str | Path) -> frozenset[str]:
    """The SHAs provisioning committed in ``tree`` — the gc floor's exclusion set.

    EMPTY on every degenerate case — no record (the steady-state norm), an
    unreadable file, malformed JSON, a mis-typed or empty entry — because an
    empty exclusion set means the unpushed floor keeps the Tree, exactly the
    conservative direction (#232): a corrupt record must never widen what a
    fleet-wide sweep may delete, and must never crash it.
    """
    try:
        raw = record_path(tree).read_text(encoding="utf-8")
        data = json.loads(raw)
        commits = data[_COMMITS_KEY]
        if isinstance(commits, list) and all(
            isinstance(sha, str) and sha for sha in commits
        ):
            return frozenset(commits)
        logger.debug("provision: record for %s has mis-typed commits: %r", tree, data)
    except (OSError, ValueError, TypeError, KeyError):
        logger.debug("provision: no readable record for %s", tree, exc_info=True)
    return frozenset()
