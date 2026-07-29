"""The shared CLI parameter library — argv becomes value objects at parse."""

from __future__ import annotations

import click

from ..identity import Repo, repo_from_slug
from ..tree.cleanup import parse_duration
from ._context import current_root_context


class RepoSlugParam(click.ParamType):
    """Mints a Repo from an ``owner/name`` slug at parse; an already-minted Repo passes through."""

    name = "repo"

    def convert(self, value: object, param, ctx) -> Repo:
        if isinstance(value, Repo):
            return value
        try:
            return repo_from_slug(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


REPO_SLUG = RepoSlugParam()


class DurationParam(click.ParamType):
    """Mints seconds from a human duration (``14d``/``36h``/``90m``) at parse; an already-converted float passes through."""

    name = "duration"

    def convert(self, value: object, param, ctx) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return parse_duration(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


DURATION = DurationParam()


class VersionSpecParam(click.ParamType):
    """Mints a VersionSpec from ``<semver>`` or a bump word at parse."""

    name = "version"

    def convert(self, value: object, param, ctx):
        from ..release.version import VersionSpec, parse_spec

        if isinstance(value, VersionSpec):
            return value
        try:
            return parse_spec(str(value))
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


VERSION_SPEC = VersionSpecParam()


class BareSemverParam(click.ParamType):
    """A concrete bare-semver version at parse — no bump words, no ``v`` prefix, no build metadata."""

    name = "version"

    def convert(self, value: object, param, ctx) -> str:
        from ..release.version import parse_spec

        raw = str(value)
        try:
            spec = parse_spec(raw)
        except ValueError as exc:
            self.fail(str(exc), param, ctx)
        if spec.semver is None:
            self.fail(
                f"{raw!r} is a bump word, but the version here is read off "
                "an existing tag (ADR-0041) — pass the concrete version the "
                "tag names (e.g. 1.2.3)",
                param,
                ctx,
            )
        return spec.semver


BARE_SEMVER = BareSemverParam()


def _ambient_repo() -> Repo | None:
    """Callable default for an omitted REPO: the root context's repo, or None outside a checkout."""
    return current_root_context().repo


def _ambient_path() -> str:
    """Callable default for an omitted PATH: the ambient checkout root, else cwd."""
    return current_root_context().default_path()


def repo_argument(fn):
    """Optional REPO argument: explicit slug → Repo at parse; omitted → ambient."""
    return click.argument(
        "repo", required=False, type=REPO_SLUG, default=_ambient_repo
    )(fn)


def path_argument(fn):
    """Optional PATH argument with the ambient default an explicit path overrides."""
    return click.argument("path", required=False, default=_ambient_path)(fn)


def pr_number_argument(fn):
    """Optional PR argument; click validates only the explicit int, and resolving which PR stays a runtime boundary call."""
    return click.argument("pr", required=False, type=click.IntRange(min=1))(fn)


def shape_options(fn):
    """The shared Tree-shape option stack: ``--epic``/``--ws``/``--issue``/``--session``; which shape a combination selects is the domain's decision, not click's."""
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


json_option = click.option(
    "--json", "as_json", is_flag=True, help="Emit the result as a JSON object."
)

dry_run_option = click.option(
    "--dry-run", is_flag=True, help="Print what would change without changing anything."
)
