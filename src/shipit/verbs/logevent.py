"""The ``shipit log`` command group — the constrained dev-cycle write path
(ADR-0032 / LOG04).

``shipit log event <name> [--about "<text>"]`` is the ONE agent/hook-invocable
way a milestone enters the durable record: it accepts ONLY names registered in
:data:`shipit.events.EVENT_NAMES` (an unknown name is a loud error and exit 1,
so freeform narration is impossible on every tier), and it serves the two
weaker witness tiers — hook-witnessed (the managed post-commit hook emits
``commit.created`` through it) and skill-scripted (planning skills call it at
their checkpoints). The verb-witnessed tier never comes here: a verb that
performs a milestone emits via :func:`shipit.events.emit` directly.

Correlation is picked up, never asserted by the caller: the parent-exported
``SHIPIT_LOG_CTX_*`` keys rebind at the CLI root's logging setup (ADR-0029),
and the current checkout's branch derives ``epic``/``ws`` through the one
parser (:mod:`shipit.branchid`) — scoped to this emission, with a derived half
winning over an env-bound one (the checkout's branch is the local truth, the
fetch-seam precedent) and an out-of-grammar branch adding nothing.

``--about`` is the record's human ``msg`` — honored only for the
skill-scripted names (:data:`shipit.events.SKILL_SCRIPTED_NAMES`), capped to
one short line; every other name gets a composed domain phrase, so the verb
cannot become an agent diary (the narration constraint governs event *types*;
``msg`` stays the bounded prose slot every record already has).

``--from-hook`` declares the hook-witnessed calling context and makes the verb
fail OPEN past name validation: any emission failure is swallowed to exit 0
(WARNING per the hook fail-open canon), because a broken log path must never
block git.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import click

from .. import branchid, events, logcontext
from ..identity import Sha
from ._context import current_root_context

logger = logging.getLogger("shipit.logevent")

#: The one-short-line cap on ``--about`` (PRD: "capped to one short line").
#: Only the first line is kept, clipped here — bounded prose, not an essay.
ABOUT_MAX_CHARS = 200

#: How much of the commit sha the human ``msg`` shows — the full sha rides the
#: record as the flat ``sha`` extra; the message stays a readable phrase.
_SHORT_SHA = 12


def _compose_msg(
    name: str, about: str | None, commit: Sha | None
) -> tuple[str, dict[str, object]]:
    """The ``(msg, extra)`` for one emission — the verb's whole prose policy.

    A skill-scripted name with ``--about`` gets the caller's line (first line
    only, capped at :data:`ABOUT_MAX_CHARS`). ``commit.created`` composes its
    own phrase around the HEAD sha it exists to record (post-commit: the commit
    exists, the full sha lands as the flat ``sha`` extra). Everything else
    degrades to the name read as a domain phrase — dots to spaces — so the
    record never carries an empty ``msg``.
    """
    if name in events.SKILL_SCRIPTED_NAMES and about and about.strip():
        line = about.strip().splitlines()[0].strip()
        return line[:ABOUT_MAX_CHARS], {}
    if name == "commit.created" and commit is not None:
        return f"commit created {str(commit)[:_SHORT_SHA]}", {"sha": str(commit)}
    return name.replace(".", " "), {}


@contextmanager
def _scoped_identity(identity: branchid.BranchIdentity) -> Iterator[None]:
    """Bind the branch-derived identity for ONE emission, then restore.

    An in-grammar branch (it derived an epic) is the local truth for the WHOLE
    ``(epic, ws)`` pair: a work stream binds both halves; an umbrella branch
    binds epic and SUPPRESSES ``ws`` (via :func:`logcontext.cleared`), so a
    stale env-propagated Work Stream cannot fuse onto an epic-only branch into a
    mixed identity. An out-of-grammar branch derived nothing and touches
    nothing — the env-bound identity shows through untouched.
    """
    if identity.epic is None:
        yield
        return
    if identity.ws is not None:
        with logcontext.scoped(epic=identity.epic, ws=identity.ws):
            yield
        return
    with logcontext.scoped(epic=identity.epic), logcontext.cleared("ws"):
        yield


def run(
    name: str,
    *,
    about: str | None = None,
    from_hook: bool = False,
    branch: str | None = None,
    commit: Sha | None = None,
) -> int:
    """Emit one registered dev-cycle event; the constrained write path's core.

    ``branch``/``commit`` are the current checkout's, resolved once at the CLI
    root (ADR-0030) and threaded in by the click command — injectable so tests
    drive the full matrix without a git checkout.

    Exit contract: an unregistered ``name`` is ALWAYS a clean one-line
    ``error:`` + exit 1, hook context included — a typo in hook wiring is a
    config bug to surface, and a post-commit hook cannot block the commit
    anyway. Past that gate the emission is best-effort: any failure exits 0
    under ``from_hook`` (fail-open, WARNING logged) and ``error:`` + 1
    otherwise. Note the durable write itself is inherently fail-open — an
    unopenable log file already degraded to console-only at logging setup
    (:func:`shipit.logsetup.configure_logging`), and stdlib handlers swallow
    emit-time I/O errors — so this guard covers the residual seams (identity,
    binding) rather than re-implementing that posture.
    """
    if name not in events.EVENT_NAMES:
        known = ", ".join(sorted(events.EVENT_NAMES))
        print(
            f"error: unknown dev-cycle event {name!r} — the closed vocabulary "
            f"is: {known} (ADR-0032; register new names in "
            "shipit.events.EVENT_NAMES)",
            file=sys.stderr,
        )
        return 1
    try:
        identity = branchid.derive(branch)
        msg, extra = _compose_msg(name, about, commit)
        # Scoped, not process-lifetime: the binding is local to this one
        # emission. An in-grammar branch is the local truth for the whole
        # identity (an umbrella suppresses env ws); an out-of-grammar branch
        # touches nothing, so env-bound identity shows through.
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
        message = " ".join(str(exc).split())
        print(f"error: {message}", file=sys.stderr)
        return 1


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
    """Record dev-cycle event NAME into the durable per-repo log.

    NAME must be registered in the closed vocabulary (ADR-0032) — an unknown
    name is a hard error, so the record stays a milestone trail, never a
    diary. Domain keys are picked up, not passed: parent-exported
    SHIPIT_LOG_CTX_* keys plus epic/ws derived from the current branch.
    """
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
