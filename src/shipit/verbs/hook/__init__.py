"""The ``shipit hook`` command group — Claude Code lifecycle-hook entrypoints.

A NESTED click group (mirrors ``verbs/pr/``): ``shipit hook <event>`` is the
binary side of ADR-0012's enforcement — a thin committed line in
``.claude/settings.json`` invokes ``shipit hook pretooluse`` so all the rich
logic ships in the versioned package, never in the hook wiring. Each event is
its own module exposing a ``cmd`` click command, registered below by an
append-only line.
"""

from __future__ import annotations

import click


@click.group(
    name="hook",
    help=(
        "Claude Code lifecycle-hook entrypoints.\n\n"
        "The binary side of the agent harness: `.claude/settings.json` calls "
        "`shipit hook <event>` on stdin/stdout. `--help` is the map."
    ),
)
def hook() -> None:
    """Root of the ``hook`` subcommand group; events are attached below."""


# --- event registration (append-only; one import + one add_command per event) ---
from . import eval as _eval  # noqa: E402  (HAR02-WS01)
from . import pretooluse  # noqa: E402  (HAR01-WS01)
from . import worktreecreate  # noqa: E402  (TRE03-WS04)

hook.add_command(pretooluse.cmd)
hook.add_command(_eval.stop_cmd)
hook.add_command(_eval.subagent_stop_cmd)
hook.add_command(worktreecreate.cmd)
