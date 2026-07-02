"""``shipit hook worktreeremove`` — the ephemeral-Tree fast-path teardown (ADR-0027).

Claude Code fires ``WorktreeRemove`` when a session leaves the worktree it adopted
at ``WorktreeCreate`` — for us, when a coordinator session rooted in an ephemeral
session Tree exits cleanly. This boundary reclaims that Tree IMMEDIATELY — remove
the :mod:`shipit.session.liveness` pidfile, then the clone — instead of leaving it
for the next ``tree gc`` sweep.

**Best-effort only, fail-OPEN** — the same posture as ``hook sessionstart``, the
OPPOSITE of ``hook worktreecreate``. The spike behind ADR-0027 showed this event
does NOT fire in headless mode, so nothing may depend on it: the ``gc`` ephemeral
ladder (:func:`shipit.tree.cleanup.classify`) is the load-bearing cleanup and this
hook is only its fast path. ANY failure — bad payload, unreadable git state, a
failed delete — logs at WARNING (the fail-open canon in :mod:`shipit.verbs.hook`:
a swallowed failure is a degraded-but-continuing outcome) and exits 0; a teardown
hiccup must never turn a clean session exit into an error. A by-design refusal
(nothing ephemeral in the payload, a dirty/unpushed Tree left for the gc ladder)
is a clean no-op and stays at DEBUG.

Fast does not mean careless — the ladder's absolute floor holds here too:

- Only an **ephemeral** Tree (``…/ephemeral/<id>``, by the leaf's parent segment)
  **under the central root** is ever touched. Every other kind — a write Tree, a
  shared review clone, an arbitrary path in the payload — is left to its own
  reclaim rule; a hook fed a hostile or confused path deletes nothing.
- A **dirty** Tree or one with **unpushed** commits (the upstream-independent
  list — commits on NO remote, so a fresh no-upstream ``ephemeral/<id>`` branch
  is judged by what it actually holds) is NEVER auto-removed, even on a clean
  exit; it ages into the gc ladder where the same floor keeps it. An UNREADABLE
  list blocks removal the same way — unknown must never read as "nothing to lose".
  Mirroring that ladder's rung-1 carve-out (#232), the commit SHA(s) recorded by
  the Tree's own provisioning (:mod:`shipit.tree.provision` — the managed-set
  reconcile a drift window commits at birth) are excluded from the block: they
  are shipit's commit, not the session's work, and without the exclusion every
  drift-window session Tree would dodge the fast path on a clean exit. The
  ladder's ``ahead`` side holds too: an upstream-ahead count the exclusion does
  not account for (commits pushed to some other branch, or a miscount) blocks
  the fast path exactly as :func:`shipit.tree.cleanup._has_local_only_work`
  conservatively keeps (codex review on #233).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TextIO

import click

from ... import git
from ...session import liveness
from ...tree import provision
from ...tree.layout import EPHEMERAL_KIND, central_root, tree_kind
from ...tree.readonly import remove_tree

logger = logging.getLogger("shipit.hook")

#: Payload fields that may carry the removed worktree's path, tried in order. The
#: WorktreeRemove payload is not pinned by a spike yet (it does not fire headless,
#: so the create-spike could not capture one); the create payload carries ``cwd``,
#: and ``path``/``worktree_path`` are the plausible explicit fields. Every
#: candidate still has to pass the ephemeral/central-root/clean gates below, so a
#: wrong guess degrades to a no-op, never a wrong delete.
_PATH_FIELDS = ("path", "worktree_path", "cwd")


@click.command(name="worktreeremove")
def cmd() -> None:
    """Reclaim a clean ephemeral session Tree on session exit (best-effort).

    Reads the ``WorktreeRemove`` payload as JSON on stdin, removes the Tree's
    liveness pidfile and the clone itself when — and only when — it is an
    ephemeral Tree under the central root holding no local-only work. Always
    exits 0; any failure or refusal is a silent no-op (the ``gc`` ladder is the
    load-bearing cleanup).
    """
    raise SystemExit(run())


def run(stdin: TextIO | None = None) -> int:
    """Parse stdin → gate → remove pidfile + Tree. Returns 0 always (fail-open)."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"WorktreeRemove payload is not an object: {payload!r}")
        tree = _target_tree(payload)
        if tree is None:
            logger.debug(
                "worktreeremove: no ephemeral Tree under the central root in the "
                "payload — nothing to reclaim"
            )
            return 0
        blocker = _removal_blocker(tree)
        if blocker is not None:
            logger.debug(
                "worktreeremove: %s has %s — left for the gc ladder", tree, blocker
            )
            return 0
        liveness.remove_pidfile(tree)
        remove_tree(tree)
        logger.debug("worktreeremove: reclaimed %s", tree)
    except Exception:  # noqa: BLE001 — fail-open: the gc ladder is the load-bearing cleanup.
        logger.warning(
            "worktreeremove hook failed open (nothing removed)", exc_info=True
        )
    return 0


def _target_tree(payload: dict[str, object]) -> Path | None:
    """The ephemeral Tree the payload names, or ``None`` when it names no such Tree.

    Tries each of :data:`_PATH_FIELDS` in order and returns the first value that
    passes ALL the identity gates: an absolute-izable path whose leaf-parent
    segment is the ``ephemeral`` kind, that lives UNDER the central root, and that
    is a real clone (its ``.git`` is a directory). The gates are what make the
    unpinned payload contract safe: whatever field the harness actually sends —
    or a hostile value — either names a genuine ephemeral session Tree or the
    hook does nothing.
    """
    root = central_root().resolve()
    for field in _PATH_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value).resolve()
        if tree_kind(candidate) != EPHEMERAL_KIND:
            continue
        if not candidate.is_relative_to(root):
            continue
        if not (candidate / ".git").is_dir():
            continue
        return candidate
    return None


def _removal_blocker(tree: Path) -> str | None:
    """Why ``tree`` must NOT be fast-path removed — or ``None`` when it is safe.

    The gc ladder's absolute floor, applied at the fast path: uncommitted changes
    or commits that exist on no remote (read fresh through the ``gh`` boundary —
    the hook has no registry scan to lean on) block removal; the Tree then simply
    ages into the ``gc`` sweep, whose ladder keeps it for the same reason. An
    unreadable unpushed list blocks too: unknown never reads as "nothing to lose".
    The one carve-out mirrors the ladder's (#232): SHAs the Tree's own provisioning
    recorded at birth are shipit's managed-set reconcile, not session work, so
    exactly they — and nothing else — do not block. And as in the ladder's
    :func:`~shipit.tree.cleanup._has_local_only_work`, the carve-out must also
    explain the ``ahead`` reading: an upstream-ahead count beyond the local-only
    commits (work pushed to some other branch, or a miscount) blocks
    conservatively rather than letting the fast path outrun the gc floor.
    """
    cwd = str(tree)
    if git.status_porcelain(cwd=cwd):
        return "uncommitted changes"
    unpushed = git.unpushed_shas(cwd=cwd)
    if unpushed is None:
        return "an unreadable unpushed-commit list"
    provisioned = provision.read_provision_shas(tree)
    remaining = [sha for sha in unpushed if sha not in provisioned]
    if remaining:
        plural = "s" if len(remaining) != 1 else ""
        return f"{len(remaining)} unpushed commit{plural}"
    ahead, _behind = git.ahead_behind(cwd=cwd)
    if ahead > len(unpushed):
        return (
            f"an upstream-ahead count ({ahead}) beyond its "
            f"{len(unpushed)} excluded provisioning commit(s)"
        )
    return None
