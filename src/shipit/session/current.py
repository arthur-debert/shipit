"""Current-session resolution (ADR-0027 / ADR-0074 / LOG04) — which session is "this one"?

A coordinator session Tree's per-launch session id is the flat dir leaf's trailing
``<id>`` — the harness session UUID the WorktreeCreate hook binds as the Tree's
``<id>`` (ADR-0074), so the dir name IS the resume handle. The SessionStart hook
(:mod:`shipit.verbs.hook.sessionstart`) exports it into the session environment as
``SHIPIT_LOG_CTX_SESSION`` so every in-session shipit process rebinds it at logging
setup. This module is the ONE reader of "the current session" for any verb that needs
it (``shipit logs --session current``, LOG04-WS04), resolving from exactly two
sources, strongest first:

1. the **session environment** — the hook's exported key, present for every
   command run inside a session (works from any cwd the session wanders to);
2. the **containing flat Tree's ``<id>``** — the path-is-the-signal detection
   (ADR-0074) for a process whose cwd IS a session Tree but whose environment never
   went through the hook (a bare shell ``cd``'d into the Tree).

Resolution is BEST-EFFORT and never raises: a process outside any Tree gets ``None``,
and the caller says what that means for its verb. The detection half is shared with
the SessionStart hook (it delegates to :func:`containing_tree`), so the exporter and
every resolver agree on what a Tree looks like by construction.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .. import logcontext
from ..tree import layout


def containing_tree(cwd: Path) -> Path | None:
    """The flat Tree dir containing ``cwd``, or ``None`` when ``cwd`` is in no Tree.

    ADR-0074: every Tree is one flat directory ONE segment below the central root
    (``<root>/<repo>-<agent>-<timestamp>-<id>``), with no owner and no kind segment.
    So resolving the Tree from ``cwd`` is pure containment plus a single truncation —
    ``parts[0]`` — with **no depth arithmetic and no ``tree_kind`` dispatch** (both
    retired with the nested shape). Containment under
    :func:`shipit.tree.layout.central_root` is checked FIRST so a random directory
    outside the root never resolves to a bogus Tree; both sides are resolved so a
    symlinked root (macOS ``/tmp`` → ``/private/tmp``) cannot split one dir into
    "inside" and "outside" spellings. ``cwd`` may be the Tree root OR any directory
    WITHIN it (a bare shell wanders into ``src/``): truncating to the first segment
    below the root recovers the Tree either way.

    The first segment must be a CONFORMING flat leaf
    (:func:`shipit.tree.layout.parse_flat_leaf`) — its ``<agent>`` a backend binary,
    ``<timestamp>`` a real ``%Y%m%d-%H%M%S`` stamp, ``<id>`` a full UUID — else
    ``None``. That is what keeps an OLD nested Tree (whose top segment is an owner
    or a kind, not a flat leaf; it coexists by attrition) and any arbitrary non-Tree
    directory under the root from being mis-identified as a Tree.

    Raises whatever the environment read raises (a relative ``SHIPIT_TREES_ROOT`` is a
    :class:`ValueError` from ``central_root``); callers pick their own fail-open
    calibration — the SessionStart hook skips at DEBUG, :func:`current_session_id`
    degrades to ``None``.
    """
    resolved = cwd.resolve()
    root = layout.central_root().resolve()
    if not resolved.is_relative_to(root):
        return None
    parts = resolved.relative_to(root).parts
    if not parts:
        return None
    if layout.parse_flat_leaf(parts[0]) is None:
        return None
    return root / parts[0]


def current_session_id(
    env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> str | None:
    """The current session's id, or ``None`` when this process is in no session.

    Environment first — ``SHIPIT_LOG_CTX_SESSION``, the SessionStart hook's export
    (the var name comes from :data:`shipit.logcontext.ENV_PREFIX`, so exporter and
    resolver can never disagree on naming) — then the containing flat Tree's ``<id>``
    for the hook-less case (a bare shell ``cd``'d into a Tree). The path fallback reads
    the ``<id>`` from the leaf through :func:`shipit.tree.layout.parse_flat_leaf` — the
    SAME recognizer :func:`containing_tree` gated on, so a non-conforming leaf never
    reaches here — which for a coordinator session Tree IS the harness session id.
    Never raises: any detection error (a broken ``SHIPIT_TREES_ROOT``, a vanished cwd)
    degrades to ``None``, because "which session am I in" is an identification question
    and an unidentifiable session is a valid answer, not a crash. ``env``/``cwd``
    default to the real process environment and working directory; both are injectable
    so tests never read the real ones.
    """
    env = os.environ if env is None else env
    exported = env.get(logcontext.ENV_PREFIX + "SESSION")
    if exported:
        return exported
    try:
        tree = containing_tree(Path.cwd() if cwd is None else cwd)
    except (OSError, ValueError):
        return None
    if tree is None:
        return None
    leaf = layout.parse_flat_leaf(tree.name)
    return leaf.tree_id if leaf is not None else None
