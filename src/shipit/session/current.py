"""Current-session resolution (ADR-0027 / LOG04) — which session is "this one"?

The per-launch session id is the ephemeral session Tree's dir leaf (ADR-0027):
``tree/create.py`` binds it at the Tree-birth seam, and the SessionStart hook
(:mod:`shipit.verbs.hook.sessionstart`) exports it into the session environment
as ``SHIPIT_LOG_CTX_SESSION`` so every in-session shipit process rebinds it at
logging setup. This module is the ONE reader of "the current session" for any
verb that needs it (``shipit logs --session current``, LOG04-WS04), resolving
from exactly those two sources, strongest first:

1. the **session environment** — the hook's exported key, present for every
   command run inside a session (works from any cwd the session wanders to);
2. the **ephemeral Tree leaf** — the path-is-the-signal detection (ADR-0018/
   0027) for a process whose cwd IS a session Tree but whose environment never
   went through the hook (a bare shell ``cd``'d into the Tree).

Resolution is BEST-EFFORT and never raises: a process outside any session gets
``None``, and the caller says what that means for its verb. The detection half
is shared with the SessionStart hook (it delegates here), so the exporter and
every resolver agree on what an ephemeral session Tree looks like by
construction.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .. import logcontext
from ..tree import layout


def ephemeral_session_tree(cwd: Path) -> Path | None:
    """The RESOLVED ephemeral session-Tree dir when ``cwd`` is one, else ``None``.

    The path IS the signal (ADR-0018/0027): an ephemeral Tree lives at exactly
    ``<root>/<org>/<repo>/ephemeral/<leaf>`` (the shape ``tree/create.py``
    mints), and its leaf is the per-launch session id. Containment under
    :func:`shipit.tree.layout.central_root` is checked FIRST so a random
    directory that merely happens to sit in an ``ephemeral/`` folder never mints
    a bogus session key; both sides are resolved so a symlinked root (macOS
    ``/tmp`` → ``/private/tmp``) cannot split one dir into "inside" and
    "outside" spellings. ``cwd`` may be the Tree root OR any directory WITHIN it
    (a bare shell wanders into ``src/``): the Tree root is the first four
    segments below the central root, so we truncate to those before the kind
    check rather than demanding an exact depth. Pinning the kind test to that
    reconstructed root — not to ``cwd`` at whatever depth — is what keeps a
    nested ``…/ephemeral/<x>`` dir DEEPER inside a Tree (e.g. a directory named
    ``ephemeral`` in a Tree's clone) from passing: its ``ephemeral`` segment
    isn't at the root's kind position, so :func:`shipit.tree.layout.tree_kind`
    reads a different parent and rejects it.

    Raises whatever the environment read raises (a relative
    ``SHIPIT_TREES_ROOT`` is a :class:`ValueError` from ``central_root``);
    callers pick their own fail-open calibration — the SessionStart hook skips
    at DEBUG, :func:`current_session_id` degrades to ``None``.
    """
    resolved = cwd.resolve()
    root = layout.central_root().resolve()
    if not resolved.is_relative_to(root):
        return None
    parts = resolved.relative_to(root).parts
    if len(parts) < 4:
        return None
    tree = root.joinpath(*parts[:4])
    if layout.tree_kind(tree) != layout.EPHEMERAL_KIND:
        return None
    return tree


def current_session_id(
    env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> str | None:
    """The current session's id, or ``None`` when this process is in no session.

    Environment first — ``SHIPIT_LOG_CTX_SESSION``, the SessionStart hook's
    export (the var name comes from :data:`shipit.logcontext.ENV_PREFIX`, so
    exporter and resolver can never disagree on naming) — then the ephemeral
    Tree leaf of ``cwd`` for the hook-less case. Never raises: any detection
    error (a broken ``SHIPIT_TREES_ROOT``, a vanished cwd) degrades to ``None``,
    because "which session am I in" is an identification question and an
    unidentifiable session is a valid answer, not a crash. ``env``/``cwd``
    default to the real process environment and working directory; both are
    injectable so tests never read the real ones.
    """
    env = os.environ if env is None else env
    exported = env.get(logcontext.ENV_PREFIX + "SESSION")
    if exported:
        return exported
    try:
        tree = ephemeral_session_tree(Path.cwd() if cwd is None else cwd)
    except (OSError, ValueError):
        return None
    return tree.name if tree is not None else None
