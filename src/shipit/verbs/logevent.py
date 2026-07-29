"""The ``shipit log`` command group — the constrained dev-cycle write path.

See docs/adr/0032-dev-cycle-events-tagged-records-witness-tiers.md.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import click

from .. import branchid, events, logcontext
from ..identity import Sha
from ._context import current_root_context
from ._errors import cli_errors

logger = logging.getLogger("shipit.logevent")

ABOUT_MAX_CHARS = 200

_SHORT_SHA = 12


def _compose_msg(
    name: str, about: str | None, commit: Sha | None
) -> tuple[str, dict[str, object]]:
    """The ``(msg, extra)`` for one emission; ``--about`` is honored only for skill-scripted names, capped to one line."""
    if name in events.SKILL_SCRIPTED_NAMES and about and about.strip():
        line = about.strip().splitlines()[0].strip()
        return line[:ABOUT_MAX_CHARS], {}
    if name == "commit.created" and commit is not None:
        return f"commit created {str(commit)[:_SHORT_SHA]}", {"sha": str(commit)}
    return name.replace(".", " "), {}


@contextmanager
def _scoped_identity(identity: branchid.BranchIdentity) -> Iterator[None]:
    """Bind the branch-derived identity for ONE emission, then restore; an umbrella branch binds the epic and suppresses ``ws``."""
    if identity.epic is None:
        yield
        return
    if identity.ws is not None:
        with logcontext.scoped(epic=identity.epic, ws=identity.ws):
            yield
        return
    with logcontext.scoped(epic=identity.epic), logcontext.cleared("ws"):
        yield


@cli_errors
def run(
    name: str,
    *,
    about: str | None = None,
    from_hook: bool = False,
    branch: str | None = None,
    commit: Sha | None = None,
) -> int:
    """Emit one registered dev-cycle event; an unregistered name always exits 1, and past that gate ``from_hook`` makes any failure exit 0."""
    if name not in events.EVENT_NAMES:
        known = ", ".join(sorted(events.EVENT_NAMES))
        raise events.UnknownEventError(
            f"unknown dev-cycle event {name!r} — the closed vocabulary "
            f"is: {known} (ADR-0032; register new names in "
            "shipit.events.EVENT_NAMES)"
        )
    try:
        identity = branchid.derive(branch)
        msg, extra = _compose_msg(name, about, commit)
        with _scoped_identity(identity):
            events.emit(logger, name, msg, extra=extra or None)
        return 0
    except Exception as exc:  # noqa: BLE001 - the verb IS the fail-open seam
        if from_hook:
            logger.warning(
                "dev-cycle event %s not recorded from hook; failing open",
                name,
                exc_info=True,
            )
            return 0
        logger.error("dev-cycle event %s not recorded", name, exc_info=True)
        raise events.EventNotRecordedError(str(exc)) from exc


@click.group(
    name="log",
    help=(
        "The constrained dev-cycle write path.\n\n"
        "`shipit log event <name>` records a registered milestone into the "
        "durable per-repo JSONL log (ADR-0032). Registered names only — "
        "there is no freeform write path. `--help` is the map."
    ),
)
def log() -> None:
    """Root of the ``log`` subcommand group; verbs are attached below."""


@log.command(name="event")
@click.argument("name")
@click.option(
    "--about",
    default=None,
    help=(
        "One short line for the record's human msg. Honored only for the "
        "skill-scripted names (session.intent, planning.*); other events "
        "compose their own."
    ),
)
@click.option(
    "--from-hook",
    is_flag=True,
    help=(
        "Declare the hook-witnessed calling context: fail OPEN (exit 0) on "
        "any emission failure, so a broken log path never blocks git."
    ),
)
def event_cmd(name: str, about: str | None, from_hook: bool) -> None:
    """Record dev-cycle event NAME into the durable per-repo log."""
    working_dir = current_root_context().working_dir
    revision = working_dir.revision if working_dir is not None else None
    raise SystemExit(
        run(
            name,
            about=about,
            from_hook=from_hook,
            branch=revision.branch if revision is not None else None,
            commit=revision.commit if revision is not None else None,
        )
    )
