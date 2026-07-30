"""`shipit hook bashguard` — the same `PreToolUse` decider as `pretooluse`, under a marker-distinct name.

See docs/adr/0080-the-pretooluse-guard-splits-by-matcher.md.
"""

from __future__ import annotations

import click

from .pretooluse import run


@click.command(name="bashguard")
def cmd() -> None:
    """Decide a `Bash`/`Agent`/`EnterWorktree` tool call: deny native-worktree use or an un-isolated Tree-backed spawn, else allow."""
    raise SystemExit(run())
