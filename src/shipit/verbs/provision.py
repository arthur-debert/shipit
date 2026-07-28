"""`shipit provision` — the TOMBSTONE for a retired verb (ADR-0066, #1070).

`provision` used to fetch lexd — the one lint-gate tool that was not on
conda-forge — into the consumer's env by hand. ADR-0066 retired it: lexd is
published to the public Artifact channel (ADR-0064) and resolves as an ordinary
conda dependency through `pixi.lock`, delivered by the managed
`[feature.shipit-lexd]` block. The verb was deleted with no replacement path.

This is NOT a compatibility shim and restores nothing: it takes any arguments,
does no work, and always fails. It exists because the call sites outlived the
verb — a dozen `pixi.toml` lane tasks still run `./bin/shipit provision lexd`
(#1070). Most sit inside a shipit-managed `[tasks]` span and the next reconcile
deletes them, but the `feature.lint.tasks.lint-full` wiring is the consumer's
own and no reconcile rewrites it (#1127). Those tasks already fail; without a
tombstone they fail as click's `No such command 'provision'`, which tells an
operator staring at a red CI lane nothing about lexd, ADR-0066, or the one-line
edit that fixes it.

The reconcile-time tripwire (`shipit.install.reconcile.stale_provision_tasks`,
decided post-plan by `_plan_stale_provision`) is the FIRST line for the calls a
reconcile canNOT repair: it refuses the install before the pin bump lands. It
only judges the task commands it can read exactly, and declines the rest rather
than guessing — this tombstone is what makes that narrowing safe, since a
declined call still lands on this same remedy where it runs, one CI run later.
"""

from __future__ import annotations

import click

from ..install.errors import InstallError
from ._errors import cli_errors

#: The one remedy text, worded off the same facts as the reconcile refusal
#: (:func:`shipit.install.reconcile.format_stale_provision`): what is retired,
#: what replaced it, and the edit that fixes the caller.
RETIRED_MESSAGE = (
    "`shipit provision` is RETIRED (ADR-0066) — it no longer exists as a step. "
    "lexd rides the public Artifact channel as an ordinary conda dependency, "
    "delivered by the managed [feature.shipit-lexd] block and already on PATH in "
    "the lint environment. Remove this call from your pixi task (drop the "
    "`shipit provision lexd &&` prefix, or the whole `provision-lexd` task if "
    "that is all it does) and re-run `shipit install`"
)


@click.command(
    name="provision",
    # HIDDEN: a gravestone answers when called, it does not advertise itself. A
    # retired verb listed in `shipit --help` reads as one you could use, and it
    # is not one — the only reason it resolves at all is to give a call site
    # that predates the retirement a message it can act on.
    hidden=True,
    context_settings={"ignore_unknown_options": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def cmd(args: tuple[str, ...]) -> None:
    """Retired (ADR-0066): lexd rides the Artifact channel — remove this call.

    Accepts and ignores every argument the retired verb ever took, `lexd`
    included, so that EVERY surviving call site gets the remedy rather than a
    usage error about the arguments of a verb that no longer does anything.
    Exit: always 1.
    """
    raise SystemExit(run(*args))


@cli_errors
def run(*args: str) -> int:
    """Always refuse, with the remedy: one ``error: …`` line, and return 1.

    That is the DECORATED contract a caller sees. The body raises
    :class:`~shipit.install.errors.InstallError` — an operator-fixable state in
    the consumer's manifest — which the :func:`~._errors.cli_errors` shell maps
    to the stderr line + exit 1 (ADR-0030), so no exception escapes and there is
    no success return to distinguish it from. The reconcile-time refusal takes
    that same shape, off the same error type.
    """
    raise InstallError(RETIRED_MESSAGE)
