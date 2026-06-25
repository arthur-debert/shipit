"""The ``shipit pr`` command group — the PR-flow surface (PRD prf01).

A NESTED click group (shipit's first): ``shipit pr <verb>`` drives the
draft->ready review loop. This package is the extension point — each verb is its
own module exposing a ``cmd`` click command, registered below by an append-only
line. WS05 (``review``) and WS06 (``next``/``ready``) each add one ``from .``
import + one ``pr.add_command(...)`` line, so concurrent work streams touch
disjoint lines and don't conflict.

The shared PR-resolution helper lives in :mod:`._resolve` (``resolve_pr``) so
every verb resolves the target PR identically.
"""

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


# --- verb registration (append-only; one import + one add_command per verb) ---
from . import status  # noqa: E402  (WS04)

pr.add_command(status.cmd)
from . import review  # noqa: E402  (WS05)

pr.add_command(review.cmd)
from . import next_action, ready  # noqa: E402  (WS06)

pr.add_command(next_action.cmd)
pr.add_command(ready.cmd)
