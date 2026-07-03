"""RootContext — the ambient identity, resolved ONCE at the CLI root (ADR-0030).

One of the four seam pieces of the CLI boundary contract: the root callback
resolves the current checkout's :class:`~shipit.identity.WorkingDir` (offline,
origin-derived per ADR-0024) exactly once per invocation and threads it onto
click's context as a frozen :class:`RootContext`. Shared parameters
(:mod:`._params`) read it as the default an explicit REPO/PATH argument
overrides; a verb that needs a repo but runs outside a checkout fails with ONE
uniform error (:class:`NoAmbientRepoError`, mapped to ``error: …`` + exit 1 by
the :mod:`._errors` shell).

This replaces the five divergent "optional REPO defaults to the current
checkout" implementations — most importantly the per-fetch API shellouts that
contradicted ADR-0024's offline identity source. Hooks (repo from the payload
cwd) and the detached review child (explicit ``--repo``) keep their own entry
points and never read this context.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from .. import execrun
from ..identity import Repo, WorkingDir, resolve_working_dir


class NoAmbientRepoError(RuntimeError):
    """A verb needed the ambient repo, but the run is outside a checkout.

    Raised by :meth:`RootContext.require_working_dir` — the ONE uniform
    "outside a repository" refusal. A runtime outcome, not a usage error: the
    invocation was well-formed, the environment lacks a checkout, so the
    :func:`~shipit.verbs._errors.cli_errors` shell renders it as ``error: …``
    and exit 1.
    """


#: The one uniform outside-a-checkout message every verb surfaces.
_NO_REPO_MESSAGE = (
    "not inside a repository checkout (no resolvable origin remote) — "
    "run from a checkout or pass the target explicitly"
)


@dataclass(frozen=True)
class RootContext:
    """The frozen per-invocation root state threaded via click's context.

    ``working_dir`` is the ambient checkout, or ``None`` when the invocation
    runs outside one — resolution is best-effort (:func:`resolve_root_context`)
    because many verbs (and the root's own logging setup) merely degrade
    without it. Verbs that REQUIRE it ask via :meth:`require_working_dir` /
    :meth:`require_repo`, which raise the one uniform refusal.
    """

    working_dir: WorkingDir | None

    @property
    def repo(self) -> Repo | None:
        """The ambient :class:`Repo`, or ``None`` outside a checkout."""
        return self.working_dir.repo if self.working_dir is not None else None

    def require_working_dir(self) -> WorkingDir:
        """The ambient :class:`WorkingDir`, or the ONE uniform refusal."""
        if self.working_dir is None:
            raise NoAmbientRepoError(_NO_REPO_MESSAGE)
        return self.working_dir

    def require_repo(self) -> Repo:
        """The ambient :class:`Repo`, or the ONE uniform refusal."""
        return self.require_working_dir().repo

    def default_path(self, explicit: str | None = None) -> str:
        """The path a PATH-taking verb should act on.

        An ``explicit`` argument always wins; omitted, the ambient checkout's
        root, falling back to the current directory outside one (a path verb
        like ``lint`` can meaningfully run on a plain directory).
        """
        if explicit is not None:
            return explicit
        return self.working_dir.path if self.working_dir is not None else "."


def resolve_root_context(cwd: str = ".") -> RootContext:
    """Resolve the ambient :class:`WorkingDir` at ``cwd`` — best-effort, offline.

    The CLI root's ONE identity resolution per invocation (ADR-0030): local
    git reads only (:func:`shipit.identity.resolve_working_dir`, ADR-0024),
    never an API call. Outside a checkout (no origin remote, unparseable
    remote) it returns an empty context rather than failing — whether that is
    fatal is each verb's call, made through :meth:`RootContext.require_repo`.
    """
    try:
        return RootContext(working_dir=resolve_working_dir(cwd))
    except (execrun.ExecError, ValueError):
        return RootContext(working_dir=None)


def current_root_context() -> RootContext:
    """The :class:`RootContext` the CLI root threaded onto click's context.

    The accessor shared params and verb bodies use, so none of them re-derive
    identity. Outside a click invocation (a unit test driving ``run()``
    directly), there is no context to read — an empty :class:`RootContext` is
    returned, keeping direct calls usable without a click runner.
    """
    ctx = click.get_current_context(silent=True)
    obj = ctx.find_object(RootContext) if ctx is not None else None
    return obj if obj is not None else RootContext(working_dir=None)
