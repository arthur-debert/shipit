"""``tree/activity`` — how long since anyone touched this Tree, MEASURED (ADR-0072).

``newest_mtime(path) -> float | None`` answers the one question ``gc`` actually cares
about — *has anyone worked here recently?* — by asking the filesystem instead of
inferring it from a proxy. It is the only genuinely new signal ADR-0072 introduces,
and it replaces the pidfile liveness probe, the PR read, and the root-mtime clock
that between them approximated it wrongly (#1018).

Why a walk, and not the two signals already on the record:

- the clone ROOT's mtime bumps only when an entry is added or removed in THAT
  directory, so it does not observe an agent editing under ``src/`` at all —
  measured against the live fleet it lags real activity by up to **10 hours**;
- ``HEAD``'s committer stamp moves only when the agent commits, so it is blind to a
  session that edits, provisions, or reads for hours without committing (exactly
  #1018's shape).

The newest file mtime sees both, and the fleet measurement says it separates cleanly:
every live Tree reads **< 1h** idle, every dead one **> 41h** (ADR-0072).

**The prune set is load-bearing, not an optimization.** A naive walk of a Tree costs
~191.7ms (17,374 files); pruned, ~1.9ms (509 files) — ``.pixi`` alone is ~97% of the
file count (ADR-0072; re-measured here at ~425ms vs ~7ms on a 155-Tree fleet, the same
37-59× gap). An unpruned walk would cost more than the entire ``gh``-based apparatus
this signal deletes, which is why :data:`PRUNE_DIRS` is part of the decision and not
a tuning knob.

**Unreadable is not idle** (ADR-0072): every failure mode — an unreadable directory
(``os.walk``'s errors are re-raised, not swallowed; see :func:`_reraise`), a file
removed mid-walk, a ``stat`` that raises, a Tree with no eligible file at all —
returns ``None``, which every caller reads as KEEP. A boolean rule has nowhere to put
"I could not tell", and that gap would be a deletion licence: a wrongly-kept Tree
costs disk until the next sweep, a wrongly-deleted one costs work that no longer
exists.

A broken symlink is NOT among those failure modes, though ADR-0072 lists it as one.
The ADR is enumerating cases where the answer cannot be established, and for this one
it can: the walk reads links with ``lstat``, so a link whose target is gone still has
its OWN mtime, which is the stamp we want and is exactly as readable as any file's.
Blanking the whole Tree's signal over it would be strictly worse than the ADR's own
intent — it is the unknown-is-KEEP rule applied to something that is not unknown.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("shipit.tree")

#: Directories the activity walk NEVER descends into. Two reasons, both load-bearing:
#: they are enormous (``.pixi`` is ~97% of a Tree's file count, and the walk is 100×
#: slower without the prune — ADR-0072), and their mtimes are NOT agent activity —
#: an env solve, a build, or a fetch writes thousands of files that say nothing about
#: whether a human or agent is working in this Tree. Pruning them makes the signal both
#: cheap AND more truthful.
PRUNE_DIRS = frozenset(
    {
        ".git",
        ".pixi",
        "node_modules",
        "target",
        ".venv",
        "dist",
        "build",
        "__pycache__",
    }
)


def _reraise(error: OSError) -> None:
    """Re-raise a traversal error out of :func:`os.walk` — the unknown-is-KEEP hinge.

    ``os.walk`` SWALLOWS ``scandir`` failures by default: an unreadable directory is
    silently skipped and the walk completes normally, returning the maximum over
    whatever it *could* read. For this signal that default is a deletion licence —
    a Tree with an unreadable subtree would report the stale mtime of its readable
    files, and that number, unlike ``None``, licenses a delete (ADR-0072's
    unknown-is-not-false rule). Passing this as ``onerror`` turns the swallowed error
    back into the ``OSError`` that :func:`newest_mtime` catches and reports as ``None``.
    """
    raise error


def newest_mtime(path: str | Path) -> float | None:
    """The newest file mtime under ``path`` (epoch seconds) — ``None`` when unreadable.

    Walks ``path`` with :data:`PRUNE_DIRS` pruned and returns the largest mtime of any
    FILE found (symlinks are read with ``lstat`` — the link's own stamp, never its
    target's, so a link out of the Tree cannot import foreign activity or fail on a
    broken target). Directory mtimes are deliberately not considered: the root's is
    already a separate, weaker signal, and a directory stamp only says an entry was
    added or removed in it.

    Returns ``None`` — never a stale-looking number, never ``0`` — when the answer
    cannot be established: the path does not exist or is not a directory, any read or
    ``stat`` raises :class:`OSError` (permissions, a concurrent removal, a vanished
    mount), or the walk completes having found no eligible file at all. Callers read
    ``None`` as "recent activity, keep" (ADR-0072's unknown-is-not-false rule), so a
    transient filesystem hiccup can never license a delete. The failure is logged at
    DEBUG rather than swallowed silently.

    Cost, measured across a live 155-Tree fleet: ~7ms per Tree warm / ~11ms cold with
    the prune set applied, versus ~425ms unpruned — a 37-59× gap (ADR-0072's own
    figures, 1.9ms vs 191.7ms, are the same finding on a different fleet). The ratio,
    not the absolute, is the point: unpruned, the walk would cost more than the whole
    ``gh``-based apparatus it replaces.
    """
    root = Path(path)
    newest: float | None = None
    try:
        if not root.is_dir():
            logger.debug("tree activity: %s is not a directory; idle unreadable", root)
            return None
        for dirpath, dirnames, filenames in os.walk(root, onerror=_reraise):
            # Prune IN PLACE so os.walk never descends — the whole point (ADR-0072).
            dirnames[:] = [name for name in dirnames if name not in PRUNE_DIRS]
            for name in filenames:
                stamp = os.lstat(os.path.join(dirpath, name)).st_mtime
                if newest is None or stamp > newest:
                    newest = stamp
    except OSError:
        # Unreadable is NOT idle (ADR-0072): report nothing rather than a partial
        # maximum, so the caller keeps the Tree instead of deleting it on a hiccup.
        logger.debug("tree activity: %s could not be walked", root, exc_info=True)
        return None
    if newest is None:
        logger.debug(
            "tree activity: %s yielded no eligible file; idle unreadable", root
        )
    return newest
