"""The shared CLI parameter library (ADR-0030) — argv becomes value objects at parse.

One of the four seam pieces: custom click parameter types and reusable
decorators that mint value objects at argv parse, so construction IS the
validation. A malformed argument becomes a click usage error (exit 2, the
usage tier of the two-tier exit contract) and never reaches a verb body —
verb modules contain no argument validation.

The repeated CLI concepts are defined ONCE here:

- ``REPO_SLUG`` / :func:`repo_argument` — an ``owner/name`` slug through the
  canonical parser (:func:`shipit.identity.repo_from_slug`), defaulting to the
  ambient repo from the :class:`~shipit.verbs._context.RootContext`.
- :func:`path_argument` — an optional PATH with the ambient default (the
  checkout root, else the current directory).
- :data:`json_option` / :data:`dry_run_option` — the shared flags, one
  spelling, one help string.

The PR target is the deliberate exception (ADR-0030): click validates only
the explicit ``int``; resolving "which PR" (explicit number vs the current
branch's PR) stays a runtime boundary call, because "no PR for this branch"
is a runtime outcome, not a usage error.
"""

from __future__ import annotations

import click

from ..identity import Repo, repo_from_slug
from ..tree.cleanup import parse_duration
from ._context import current_root_context


class RepoSlugParam(click.ParamType):
    """Mints a :class:`~shipit.identity.Repo` from an ``owner/name`` slug at parse.

    The canonical parser (:func:`shipit.identity.repo_from_slug`) is the ONE
    place a slug becomes an identity, so an explicit REPO argument normalizes
    (lowercased owner/name, ADR-0024) exactly like a locally-resolved one. A
    malformed slug fails as a click usage error — exit 2, never verb-body code.
    An already-minted :class:`Repo` (the ambient default) passes through.
    """

    name = "repo"

    def convert(self, value: object, param, ctx) -> Repo:
        if isinstance(value, Repo):
            return value
        try:
            return repo_from_slug(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


#: The shared instance verbs reference (a ParamType is stateless).
REPO_SLUG = RepoSlugParam()


class DurationParam(click.ParamType):
    """Mints seconds (``float``) from a human duration (``14d``/``36h``/``90m``)
    at parse.

    The canonical parser (:func:`shipit.tree.cleanup.parse_duration`) is the
    ONE place a duration string becomes seconds, so a flag like ``tree gc
    --threshold`` validates at argv parse: a malformed duration is a click
    usage error — exit 2, never verb-body code (the CLI02-WS03 exit-contract
    move). An already-converted ``float`` (a programmatic default) passes
    through.
    """

    name = "duration"

    def convert(self, value: object, param, ctx) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return parse_duration(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


#: The shared instance verbs reference (a ParamType is stateless).
DURATION = DurationParam()


def _ambient_repo() -> Repo | None:
    """Callable default for an omitted REPO: the root context's repo (or ``None``).

    Evaluated by click at parameter processing time, under the invocation's
    context — so it reads the ONE root resolution, never re-deriving identity.
    ``None`` (outside a checkout) flows to the verb, whose domain call decides
    whether that is fatal (via :meth:`RootContext.require_repo`).
    """
    return current_root_context().repo


def _ambient_path() -> str:
    """Callable default for an omitted PATH: the ambient checkout root, else cwd."""
    return current_root_context().default_path()


def repo_argument(fn):
    """Optional REPO argument: explicit slug → :class:`Repo` at parse; omitted → ambient."""
    return click.argument(
        "repo", required=False, type=REPO_SLUG, default=_ambient_repo
    )(fn)


def path_argument(fn):
    """Optional PATH argument with the ambient default an explicit path overrides."""
    return click.argument("path", required=False, default=_ambient_path)(fn)


def pr_number_argument(fn):
    """Optional PR argument — the shared PR-target primitive, defined once.

    Click validates only the explicit primitive (a positive ``int`` a
    :class:`~shipit.pr.PrId` could carry; anything else is a usage error, exit
    2). Resolving "which PR" (explicit number vs the current branch's PR) is
    the deliberate ADR-0030 exception: the verb hands the validated primitive
    to :func:`shipit.gh.resolve_pr`, which MINTS the ``PrId`` at the runtime
    boundary — because "no PR for this branch" is a runtime outcome, not a
    usage error.
    """
    return click.argument("pr", required=False, type=click.IntRange(min=1))(fn)


#: ``--json`` defined once — every user-facing read verb grows this flag,
#: serialized from the typed result's ``to_dict()`` by the render seam.
json_option = click.option(
    "--json", "as_json", is_flag=True, help="Emit the result as a JSON object."
)

#: ``--dry-run`` defined once — report what would change, touch nothing.
dry_run_option = click.option(
    "--dry-run", is_flag=True, help="Print what would change without changing anything."
)
