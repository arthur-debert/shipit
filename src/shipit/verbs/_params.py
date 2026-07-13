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
- :func:`shape_options` — the Tree-shape stack
  (``--epic``/``--ws``/``--issue``/``--session``), one spelling for every
  Tree-taking verb; which shape a combination selects stays a domain decision.
- :data:`json_option` / :data:`dry_run_option` — the shared flags, one
  spelling, one help string.
- :data:`VERSION_SPEC` — a release version argument (``<semver>`` or a bump
  word) through the canonical parser
  (:func:`shipit.release.version.parse_spec`, ADR-0041).
- :data:`BARE_SEMVER` — a CONCRETE version argument for the tag-state
  re-derivation verbs (``release notes``, #898): bare semver only, no bump
  words (the version is read off an existing tag, ADR-0041).

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


class VersionSpecParam(click.ParamType):
    """Mints a :class:`~shipit.release.version.VersionSpec` from a version
    argument (``<semver>`` or ``major``/``minor``/``patch``) at parse.

    The canonical parser (:func:`shipit.release.version.parse_spec`) is the
    ONE place a version argument becomes a spec (ADR-0041/0030): a leading
    ``v``, build metadata, or a string that is neither semver nor a bump word
    fails as a click usage error — exit 2, never verb-body code. Shared by
    every release-stage verb that takes a version.
    """

    name = "version"

    def convert(self, value: object, param, ctx):
        from ..release.version import VersionSpec, parse_spec  # lazy: verb-only

        if isinstance(value, VersionSpec):
            return value
        try:
            return parse_spec(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


#: The shared instance verbs reference (a ParamType is stateless).
VERSION_SPEC = VersionSpecParam()


class BareSemverParam(click.ParamType):
    """A CONCRETE bare-semver version at parse — no bump words, no ``v`` prefix.

    The tag-state re-derivation verbs (``release notes``, #898) take the
    version READ OFF an existing tag (ADR-0041: ``v<version>`` by
    construction), so a bump word — resolvable only against tag history, for
    a cut that has not happened yet — is a usage error here, rejected at
    argv parse (exit 2) like every malformed version.
    """

    name = "version"

    def convert(self, value: object, param, ctx) -> str:
        from ..changelog import is_semver  # lazy: verb-only

        raw = str(value)
        if is_semver(raw):
            return raw
        self.fail(
            f"{raw!r} is not a bare semver version (expected e.g. 1.2.3 or "
            "1.2.3-rc.1 — no 'v' prefix, no bump words: the version is read "
            "off the tag, ADR-0041)",
            param,
            ctx,
        )


#: The shared instance verbs reference (a ParamType is stateless).
BARE_SEMVER = BareSemverParam()


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


def shape_options(fn):
    """The shared Tree-shape option stack: ``--epic``/``--ws``/``--issue``/``--session``.

    The one spelling of the shape vocabulary every Tree-taking verb speaks
    (``spawn subagent``; ``tree create`` adds its own ``--branch``/``--slug`` on
    top): the **epic shape** (``--epic E --ws N`` → branch ``E/WSnn``, base
    ``origin/E/umbrella``) and the **issue shape** (``--issue N [--session S]``
    → branch ``issues/<n>/<session>``, base ``origin/main``), per naming.lex §3.

    Click validates only each option's primitive here. WHICH shape the
    combination selects — the ``--epic``/``--ws`` pairing, positivity, whether
    an issue is required at all — is deliberately the domain pipeline's own
    shape stage (a runtime refusal through the error shell), because the valid
    combinations are per-verb semantics: a reviewer spawn legitimately carries
    no ``--issue``, so a parse-time requirement would reject it before the
    domain could accept it (the ADR-0030 PR-target precedent: runtime outcome,
    not usage error).
    """
    fn = click.option(
        "--session",
        default="work",
        show_default=True,
        help=(
            "Issue shape: session name in the branch issues/<n>/<session>. The "
            "suffix keeps issues/<n>/ a ref directory so a +1 session on the same "
            "issue (e.g. --session onboard) coexists with the default `work` "
            "(naming.lex §3). Ignored by the --epic/--ws shape."
        ),
    )(fn)
    fn = click.option(
        "--issue",
        type=int,
        default=None,
        help=(
            "Issue shape: issue number N (branch issues/<n>/<session>, cut from "
            "origin/main). Omit --epic/--ws to select this shape."
        ),
    )(fn)
    fn = click.option(
        "--ws",
        type=int,
        default=None,
        help=(
            "Epic shape (with --epic): work stream number N — the WSnn half of "
            "the branch E/WSnn."
        ),
    )(fn)
    fn = click.option(
        "--epic",
        default=None,
        help=(
            "Epic shape (with --ws): epic code E, e.g. TRE03 — branch E/WSnn, cut "
            "from origin/E/umbrella. Omit both for the standalone --issue shape."
        ),
    )(fn)
    return fn


#: ``--json`` defined once — every user-facing read verb grows this flag,
#: serialized from the typed result's ``to_dict()`` by the render seam.
json_option = click.option(
    "--json", "as_json", is_flag=True, help="Emit the result as a JSON object."
)

#: ``--dry-run`` defined once — report what would change, touch nothing.
dry_run_option = click.option(
    "--dry-run", is_flag=True, help="Print what would change without changing anything."
)
