"""The ``shipit hook`` command group — Claude Code lifecycle-hook entrypoints.

Each hook's failure-arm log level follows its fail mode: fail-closed logs ERROR, fail-open WARNING, a clean no-op DEBUG.
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


from . import eval as _eval  # noqa: E402
from . import (  # noqa: E402
    pretooluse,
    sessionstart,
    worktreecreate,
    worktreeremove,
)

hook.add_command(pretooluse.cmd)
hook.add_command(_eval.stop_cmd)
hook.add_command(_eval.subagent_stop_cmd)
hook.add_command(sessionstart.cmd)
hook.add_command(worktreecreate.cmd)
hook.add_command(worktreeremove.cmd)
