"""RootContext — the ambient identity, resolved once at the CLI root.

See docs/adr/0030-cli-boundary-parse-to-values-typed-results.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from .. import execrun
from ..identity import Repo, WorkingDir, resolve_working_dir


class NoAmbientRepoError(RuntimeError):
    """A verb needed the ambient repo, but the run is outside a checkout."""


_NO_REPO_MESSAGE = (
    "not inside a repository checkout (no resolvable origin remote) — "
    "run from a checkout or pass the target explicitly"
)


@dataclass(frozen=True)
class RootContext:
    """The frozen per-invocation root state threaded via click's context; ``working_dir`` is None outside a checkout."""

    working_dir: WorkingDir | None

    @property
    def repo(self) -> Repo | None:
        return self.working_dir.repo if self.working_dir is not None else None

    def require_working_dir(self) -> WorkingDir:
        if self.working_dir is None:
            raise NoAmbientRepoError(_NO_REPO_MESSAGE)
        return self.working_dir

    def require_repo(self) -> Repo:
        return self.require_working_dir().repo

    def default_path(self, explicit: str | None = None) -> str:
        """``explicit`` wins; omitted, the ambient checkout root, else the current directory."""
        if explicit is not None:
            return explicit
        return self.working_dir.path if self.working_dir is not None else "."


def resolve_root_context(cwd: str = ".") -> RootContext:
    """Resolve the ambient WorkingDir at ``cwd`` — offline, and an empty context outside a checkout rather than a failure."""
    try:
        return RootContext(working_dir=resolve_working_dir(cwd))
    except (execrun.ExecError, ValueError):
        return RootContext(working_dir=None)


def ambient_identity(explicit_repo: Repo | None) -> tuple[Repo, str | None]:
    """The ``(repo, branch)`` a ``pr`` verb resolves its target against, both from a single working-dir resolution; an explicit ``repo`` wins and carries no branch."""
    if explicit_repo is not None:
        return explicit_repo, None
    wd = current_root_context().require_working_dir()
    return wd.repo, wd.revision.branch


def current_root_context() -> RootContext:
    """The RootContext click threaded onto its context, or an empty one outside a click invocation."""
    ctx = click.get_current_context(silent=True)
    obj = ctx.find_object(RootContext) if ctx is not None else None
    return obj if obj is not None else RootContext(working_dir=None)
