"""``tree/provision`` — the READ side of the retired provisioning-commit record.

Tree provisioning no longer commits ANYTHING (ADR-0033): the TRE03-era
``shipit install --local`` step — which fail-closed into a
``chore(shipit): install/update the managed set`` reconcile commit on the
just-cut branch during every tool/managed-set drift window — is deleted, because
the Shipit pin (``.shipit.toml [shipit].version`` + the pinned ``bin/shipit``
launcher) makes Tree and tool coherent by construction. With the producer gone,
the record's WRITER is gone too: no new ``.git/shipit-provision.json`` is ever
written.

What remains is the reader, :func:`read_provision_shas`, and only because
Trees born BEFORE the pin still carry records on disk: the ephemeral gc
ladder's unpushed floor (#232, ADR-0027) subtracts exactly the recorded
commit SHA(s) — shipit's own reconcile, not the session's work — so those
drift-window Trees stay reclaimable instead of being held forever by a commit
shipit itself made. Every degenerate case — no record (now the universal
steady state), an unreadable file, malformed JSON, mis-typed fields — reads as
the EMPTY set: nothing excluded, the floor keeps the Tree. Conservative by
construction: this module can only ever *narrow* what counts as work, never
widen what gc may delete. Once the pre-pin Tree population ages out, this
whole module — and the ``provision_shas`` exclusion input it feeds
(:mod:`shipit.tree.cleanup` / :mod:`shipit.tree.gc`) — retires with it.

Identity is the commit SHA, deliberately NOT the commit message: a message
heuristic would also match a rebased/amended descendant carrying real changes,
whereas a SHA mismatch (rebase, amend) simply fails the exclusion and falls back
to KEEP — the safe direction (#232 coordinator note).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..identity import Sha

logger = logging.getLogger("shipit.tree")

#: The record's name inside the clone's ``.git`` directory (inside ``.git`` so
#: it could never dirty the working tree and trip the very floor it refines).
PROVISION_RECORD_NAME = "shipit-provision.json"

#: The record's one JSON field: the full SHAs of the commits provisioning made.
_COMMITS_KEY = "commits"


def record_path(tree: str | Path) -> Path:
    """Where ``tree``'s provision record lives: ``<tree>/.git/shipit-provision.json``."""
    return Path(tree) / ".git" / PROVISION_RECORD_NAME


def read_provision_shas(tree: str | Path) -> frozenset[Sha]:
    """The SHAs pre-pin provisioning committed in ``tree`` — gc's exclusion set.

    Returned as :class:`~shipit.identity.Sha` value objects (PROC03), parsed —
    and therefore VALIDATED — at this read boundary, so the exclusion set
    compares against :func:`shipit.git.unpushed_shas`'s typed list through the
    type, never via raw-string equality. EMPTY on every degenerate case — no
    record (the universal steady state since ADR-0033 retired the writer), an
    unreadable file, malformed JSON, a mis-typed entry, or an entry that is not
    a full sha — because an empty exclusion set means the unpushed floor keeps
    the Tree, exactly the conservative direction (#232): a corrupt record must
    never widen what a fleet-wide sweep may delete, and must never crash it.
    """
    try:
        raw = record_path(tree).read_text(encoding="utf-8")
        data = json.loads(raw)
        commits = data[_COMMITS_KEY]
        if isinstance(commits, list) and all(
            isinstance(sha, str) and sha for sha in commits
        ):
            # `Sha(...)` raising ValueError on a non-sha entry falls through to
            # the same degenerate-case handling: an invalid identity excludes
            # NOTHING (the floor keeps the Tree) rather than crashing the sweep.
            return frozenset(Sha(sha) for sha in commits)
        logger.debug("provision record for %s has mis-typed commits: %r", tree, data)
    except (OSError, ValueError, TypeError, KeyError):
        logger.debug("provision record unreadable for %s", tree, exc_info=True)
    return frozenset()
