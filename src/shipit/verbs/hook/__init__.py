"""The ``shipit hook`` command group — Claude Code lifecycle-hook entrypoints.

A NESTED click group (mirrors ``verbs/pr/``): ``shipit hook <event>`` is the
binary side of ADR-0012's enforcement — a thin committed line in
``.claude/settings.json`` invokes ``shipit hook pretooluse`` so all the rich
logic ships in the versioned package, never in the hook wiring. Each event is
its own module exposing a ``cmd`` click command, registered below by an
append-only line.

**Failure-arm log-level canon (LOG03 #311)** — every hook's failure arm is
calibrated by the hook's fail-mode, per the glassbox spray conventions:

- **Fail-CLOSED** (a failure aborts the operation — ``worktreecreate``): the
  failure propagates out of the process as a non-zero exit, so it is a
  propagating failure → log at **ERROR** with ``exc_info=True`` and whatever
  domain keys are bound/derivable at that point (e.g. the payload's
  ``session_id``), *before* exiting non-zero.
- **Fail-OPEN** (a failure is swallowed and the session continues —
  ``pretooluse``, ``sessionstart``, ``worktreeremove``, ``stop`` /
  ``subagent-stop``): the swallow is a degraded-but-continuing outcome → log
  at **WARNING** with ``exc_info=True``. This applies to every arm that
  swallows an exception, not just the outermost guard. Clean no-ops (nothing
  configured, nothing to do, a by-design conservative refusal) are mechanics
  and stay at DEBUG.

A hook's stderr print (where it has one) is the hook protocol's user-facing
surface and stays — but it is never the ONLY record: the JSONL log line above
is what makes the failure visible to anyone reading the story at info/error.
The next hook author copies this canon, not an individual call site.
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
from . import sessionstart  # noqa: E402  (SES01-WS01)
from . import worktreecreate  # noqa: E402  (TRE03-WS04)
from . import worktreeremove  # noqa: E402  (SES02-WS02)

hook.add_command(pretooluse.cmd)
hook.add_command(_eval.stop_cmd)
hook.add_command(_eval.subagent_stop_cmd)
hook.add_command(sessionstart.cmd)
hook.add_command(worktreecreate.cmd)
hook.add_command(worktreeremove.cmd)
