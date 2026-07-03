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


#: ``--json`` defined once — every user-facing read verb grows this flag,
#: serialized from the typed result's ``to_dict()`` by the render seam.
json_option = click.option(
    "--json", "as_json", is_flag=True, help="Emit the result as a JSON object."
)

#: ``--dry-run`` defined once — report what would change, touch nothing.
dry_run_option = click.option(
    "--dry-run", is_flag=True, help="Print what would change without changing anything."
)
