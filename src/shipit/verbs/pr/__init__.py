"""The ``shipit pr`` command group — the PR-flow surface."""

from __future__ import annotations

import click


@click.group(
    name="pr",
    help=(
        "PR flow — drive a draft PR through review to ready.\n\n"
        "Read-only `status` reports where the PR stands and the single next "
        "action; the act/flip verbs follow. `--help` is the map."
    ),
)
def pr() -> None:
    """Root of the ``pr`` subcommand group; verbs are attached below."""


from . import status  # noqa: E402  (WS04)

pr.add_command(status.cmd)
from . import review  # noqa: E402  (WS05)

pr.add_command(review.cmd)
from . import next_action, ready  # noqa: E402  (WS06)

pr.add_command(next_action.cmd)
pr.add_command(ready.cmd)
from . import classify  # noqa: E402  (ADR-0044)

pr.add_command(classify.cmd)
from . import wait  # noqa: E402  (RVW02-WS01, ADR-0034)

pr.add_command(wait.cmd)
