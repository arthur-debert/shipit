"""``shipit hook worktreeremove`` — the ephemeral-Tree fast-path teardown (ADR-0027).

Claude Code fires ``WorktreeRemove`` when a session leaves the worktree it adopted
at ``WorktreeCreate`` — for us, when a coordinator session rooted in an ephemeral
session Tree exits cleanly. This boundary reclaims that Tree IMMEDIATELY — removes
the clone — instead of leaving it for the next ``tree gc`` sweep.

**Best-effort only, fail-OPEN** — the same posture as ``hook sessionstart``, the
OPPOSITE of ``hook worktreecreate``. The spike behind ADR-0027 showed this event
does NOT fire in headless mode, so nothing may depend on it: the ``gc`` reclaim rule
(:func:`shipit.tree.cleanup.classify`) is the load-bearing cleanup and this hook is
only its fast path. ANY failure — bad payload, unreadable git state, a
failed delete — logs at WARNING (the fail-open canon in :mod:`shipit.verbs.hook`:
a swallowed failure is a degraded-but-continuing outcome) and exits 0; a teardown
hiccup must never turn a clean session exit into an error. A by-design refusal
(nothing ephemeral in the payload, a dirty/unpushed Tree left for the gc ladder)
is a clean no-op and stays at DEBUG.

Fast does not mean careless — the reclaim rule's never-lose-work floor holds here
too:

- Only a real **Tree clone under the central root** is ever touched (ADR-0074:
  Trees are flat and self-describing, with no kind segment to key off — the flat
  leaf carries no ``ephemeral``/``write``/``review`` marker, and this event fires
  only for the worktree the session itself adopted at ``WorktreeCreate``). An
  arbitrary or hostile path — outside the root, or not a git clone — is left
  untouched, and the never-lose-work floor below is what keeps a Tree carrying work
  safe regardless of which Tree the payload names.
- A **dirty** Tree or one with **unpushed** commits (the upstream-independent
  list — commits on NO remote, so a fresh no-upstream ``ephemeral/<id>`` branch
  is judged by what it actually holds) is NEVER auto-removed, even on a clean
  exit; it falls through to the gc rule, whose own floor
  (:func:`shipit.tree.cleanup._has_local_only_work`) keeps it for the same
  reason. An UNREADABLE list blocks removal the same way — unknown must never
  read as "nothing to lose".

This never-lose-work floor is now EXACTLY gc's own
(:func:`shipit.tree.cleanup._has_local_only_work`): dirty or unpushed (or an unreadable
unpushed list) keeps the Tree. The FLOOR is the only thing shared with gc, NOT the
whole reclaim decision — the fast path still deliberately reclaims a clean ephemeral
Tree IMMEDIATELY on a clean exit, whereas gc would keep that same Tree until its idle
age crosses the 48h threshold. The two agree on what must never be lost, not on when a
safe Tree is finally collected.

The fast path used to apply a STRICTER floor — it carved out (#232) the SHA(s) the
Tree's own provisioning committed at birth and additionally blocked on an
upstream-``ahead`` count. ADR-0072's reclaim rule dropped the ephemeral ladder those
readings served, and WS03 retired the provisioning-record reader (the former
``shipit.tree.provision``) and the ``ps`` liveness pidfile with it, so both extra
readings are gone here too. Dropping the carve-out only ever makes the floor MORE
conservative (a Tree carrying an unpushed provisioning commit is now kept, not
reclaimed), and the ``ahead`` block guarded work that — being pushed to some remote —
the unpushed floor already treats as safe; so the fast path's floor now blocks removal
in exactly the cases gc's floor would, never fewer.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TextIO

import click

from ... import git
from ...tree.layout import central_root
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

    Reads the ``WorktreeRemove`` payload as JSON on stdin, removes the clone when —
    and only when — it is an ephemeral Tree under the central root holding no
    local-only work. Always exits 0; any failure or refusal is a silent no-op (the
    ``gc`` rule is the load-bearing cleanup).
    """
    raise SystemExit(run())


def run(stdin: TextIO | None = None) -> int:
    """Parse stdin → gate → remove the Tree. Returns 0 always (fail-open)."""
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
        remove_tree(tree)
        logger.debug("worktreeremove: reclaimed %s", tree)
    except Exception:  # noqa: BLE001 — fail-open: the gc ladder is the load-bearing cleanup.
        logger.warning(
            "worktreeremove hook failed open (nothing removed)", exc_info=True
        )
    return 0


def _target_tree(payload: dict[str, object]) -> Path | None:
    """The Tree the payload names, or ``None`` when it names no reclaimable Tree.

    Tries each of :data:`_PATH_FIELDS` in order and returns the first value that
    passes ALL the identity gates: an absolute-izable path that lives STRICTLY UNDER
    the central root (a Tree is one leaf BELOW the root — the root itself is never a
    Tree, and ``Path.is_relative_to`` is ``True`` for an equal path, so equality is
    rejected explicitly lest a payload naming the root, if the root carries a ``.git``,
    hand the root itself up for removal) and is a real clone (its ``.git`` is a
    directory). ADR-0074 retired
    the kind segment, so there is no ``ephemeral``-vs-other test to make here — the
    flat leaf carries no kind, this event fires only for the worktree the session
    adopted, and the never-lose-work floor (:func:`_removal_blocker`) is what keeps a
    Tree carrying work safe. The gates still make the unpinned payload contract safe:
    whatever field the harness sends — or a hostile value — either names a genuine
    Tree clone under the root or the hook does nothing.
    """
    root = central_root().resolve()
    for field in _PATH_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            continue
        if not (candidate / ".git").is_dir():
            continue
        return candidate
    return None


def _removal_blocker(tree: Path) -> str | None:
    """Why ``tree`` must NOT be fast-path removed — or ``None`` when it is safe.

    The never-lose-work floor, applied at the fast path — now IDENTICAL to gc's own
    (:func:`~shipit.tree.cleanup._has_local_only_work`): uncommitted changes or commits
    that exist on no remote (read fresh through the ``git`` boundary — the hook has no
    registry scan to lean on) block removal; the Tree then simply falls through to the
    ``gc`` sweep, whose floor keeps it for the same reason. An unreadable unpushed list
    blocks too: unknown never reads as "nothing to lose".

    Once stricter than gc's floor — it carved out the provisioning-commit SHAs (#232)
    and blocked on an upstream-``ahead`` count — the fast path shed both when WS03
    retired the provisioning-record reader and the ephemeral ladder those readings
    served (ADR-0072). What remains is exactly gc's never-lose-work floor: dropping the
    carve-out can only KEEP a Tree gc's floor would also keep, never remove one it
    protects. (This is only about the floor — the fast path still reclaims a clean Tree
    on exit that gc would hold for its idle window; see the module docstring.)
    """
    cwd = str(tree)
    if git.status_porcelain(cwd=cwd):
        return "uncommitted changes"
    unpushed = git.unpushed_shas(cwd=cwd)
    if unpushed is None:
        return "an unreadable unpushed-commit list"
    if unpushed:
        plural = "s" if len(unpushed) != 1 else ""
        return f"{len(unpushed)} unpushed commit{plural}"
    return None
