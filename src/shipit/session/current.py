"""``session/current`` — which session is "this one"?

Resolved from the SessionStart hook's exported env key, else from the containing
flat Tree's ``<id>``. Best-effort: never raises. See docs/adr/0074-trees-are-flat.md.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .. import logcontext
from ..tree import layout


def containing_tree(cwd: Path) -> Path | None:
    """The flat Tree dir containing ``cwd`` (root or anywhere within), else ``None``.

    Both sides are resolved so a symlinked root cannot split one dir into two
    spellings. Raises whatever reading ``SHIPIT_TREES_ROOT`` raises.
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
    """The current session's id: the exported env key, else the containing Tree's
    ``<id>``, else ``None``. Never raises.
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
