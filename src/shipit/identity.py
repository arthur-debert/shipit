"""identity — shipit's canonical git-identity value objects + resolvers (ADR-0024).

The **deep module** for shipit's core nouns as identity value objects: **`Repo`**
(`owner`, `name`), **`Owner`** (`login`, `kind`) / **`OwnerKind`**, and
**`WorkingDir`** (`path`, `repo`, `revision`). Each is defined ONCE here so every
subsystem keys on the same thing — most load-bearingly the eval store, which keys
by :class:`Repo` identity so one repo's runs pool across every clone.

Two rules make this layer offline- and Tree-safe (ADR-0022):

- **Identity derives LOCALLY.** A :class:`Repo` is read from the origin remote
  (``git remote get-url origin``), never a live API call — so it resolves inside a
  dissociated Tree with no network. :func:`resolve_owner_kind` is the ONE resolver
  that touches the API, and it enriches an OPTIONAL field that is *not* part of
  identity.
- **`OwnerKind` is excluded from identity.** ``Owner.kind`` is ``compare=False``,
  so an :class:`Owner` — and the :class:`Repo` composing it — hashes and compares
  identically before and after the kind is enriched. The store key never moves
  when kind is resolved.

The value objects are thin, frozen, and composable (ADR-0021): a
:class:`WorkingDir` *has-a* :class:`Repo`, a :class:`Repo` *has-an* :class:`Owner`
— composition, never inheritance (a **Tree** *has* a WorkingDir; the **main
checkout** is a WorkingDir that is not a Tree). Logic lives in free functions over
them (``resolve_*``), each taking an injectable git ``boundary`` (defaulting to
:mod:`shipit.gh`) so the module is unit-testable in isolation with a fake boundary
and fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from . import gh


class OwnerKind(Enum):
    """The closed registry of what an **Owner** can be — a user or an organization.

    Mirrors the other closed registries (Role / Toolchain / Reviewer adapter):
    adding a member is one entry and nothing downstream changes. Resolved via the
    API on demand (:func:`resolve_owner_kind`), never required to identify a
    :class:`Repo` — org-only capabilities (rulesets, Actions org policy) hang off
    the ``ORGANIZATION`` member.
    """

    USER = "user"
    ORGANIZATION = "organization"


@dataclass(frozen=True)
class Owner:
    """The account that owns a :class:`Repo` — ``(login, kind)``.

    ``login`` is always known offline (it comes straight from the origin remote).
    ``kind`` is an OPTIONAL, lazily-resolved enrichment declared ``compare=False``
    so it is **excluded from equality and hash**: two ``Owner``\\s with the same
    login are the same identity whether or not their kind is known, which is what
    keeps the eval store key stable across kind enrichment.
    """

    login: str
    kind: OwnerKind | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Repo:
    """A GitHub repository as shipit's identity value object — ``(owner, name)``.

    Derived LOCALLY from the origin remote (:func:`resolve_repo`), never an API
    call. The stable key every Repo-scoped join uses — notably the eval store.
    Because :class:`Owner` excludes ``kind`` from equality, a ``Repo`` is the same
    identity before and after its owner's kind is enriched.
    """

    owner: Owner
    name: str

    @property
    def slug(self) -> str:
        """The canonical ``owner/name`` GitHub slug."""
        return f"{self.owner.login}/{self.name}"


@dataclass(frozen=True)
class Revision:
    """The revision half of a :class:`WorkingDir` — ``(branch, commit)``.

    Both are best-effort and may be ``None`` (a detached/unborn HEAD has no branch;
    an unresolvable HEAD has no commit) — a WorkingDir is a *location*, so a missing
    revision never makes it un-constructible.
    """

    branch: str | None = None
    commit: str | None = None


@dataclass(frozen=True)
class WorkingDir:
    """An on-disk checkout embodying a :class:`Repo` at a revision.

    ``(path, repo, revision)`` — the single resolver for "what repo + revision is
    checked out at this path" (:func:`resolve_working_dir`), replacing the
    scattered ``git rev-parse --show-toplevel`` re-derivations. Composition, not
    inheritance: a **Tree** *has* a WorkingDir; the **main checkout** is a
    WorkingDir that is not a Tree. Its :class:`Repo` is the identity — two clones
    of one repo are two WorkingDirs but one Repo.
    """

    path: str
    repo: Repo
    revision: Revision


#: Extracts ``owner`` / ``name`` from an origin remote URL's tail, across every
#: shape ``git remote get-url origin`` emits: HTTPS (``https://github.com/o/n.git``),
#: SCP-style SSH (``git@github.com:o/n.git``), and ``ssh://`` URLs. Anchored to the
#: end so it keys off the ``…/<owner>/<name>`` tail regardless of host/scheme; the
#: ``[:/]`` before ``owner`` matches the SSH ``:`` or the path ``/``; a trailing
#: ``.git`` and/or ``/`` are optional and stripped.
_REMOTE_TAIL = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


def parse_remote_url(url: str) -> tuple[str, str]:
    """``(owner, name)`` parsed from an origin remote ``url`` — a PURE function.

    Handles the HTTPS, SCP-style SSH, and ``ssh://`` forms uniformly (see
    :data:`_REMOTE_TAIL`), stripping any ``.git`` suffix. Raises :class:`ValueError`
    when the URL carries no ``owner/name`` tail, so a malformed remote surfaces
    loudly rather than yielding a bogus identity.
    """
    match = _REMOTE_TAIL.search(url.strip())
    if match is None:
        raise ValueError(f"cannot parse owner/name from remote url: {url!r}")
    return match.group("owner"), match.group("name")


def resolve_repo(cwd: str = ".", *, boundary=gh) -> Repo:
    """The :class:`Repo` checked out at ``cwd`` — derived LOCALLY from origin.

    Reads ``git remote get-url origin`` (via the injected ``boundary``, default
    :mod:`shipit.gh`) and parses its ``owner/name`` tail — offline and Tree-safe,
    deliberately NOT the API-based ``gh.current_repo()``. Raises :class:`shipit.gh.GhError`
    when there is no origin remote and :class:`ValueError` when the URL is
    unparseable.
    """
    url = boundary.git_remote_url(cwd=cwd)
    owner_login, name = parse_remote_url(url)
    return Repo(owner=Owner(login=owner_login), name=name)


def resolve_working_dir(cwd: str = ".", *, boundary=gh) -> WorkingDir:
    """The :class:`WorkingDir` at ``cwd`` — its repo-root path, :class:`Repo`, revision.

    The single resolver replacing the ``git rev-parse --show-toplevel``
    re-derivations: the path is the git toplevel (via the one
    :func:`shipit.gh.repo_root` boundary), falling back to ``cwd`` only when
    ``repo_root`` yields nothing. The :class:`Repo` and :class:`Revision` are read
    against that root, so identity and revision describe the same checkout.

    This REQUIRES a checkout. A :class:`WorkingDir` *has-a* :class:`Repo`, and a
    :class:`Repo` needs an origin remote, so outside a checkout (no origin) this
    raises :class:`shipit.gh.GhError`, propagated from :func:`resolve_repo` — it
    does NOT fabricate an identity-less WorkingDir. Identity resolution is
    local/offline (see :func:`resolve_repo`).
    """
    root = boundary.repo_root(cwd=cwd) or cwd
    repo = resolve_repo(root, boundary=boundary)
    revision = Revision(
        branch=boundary.git_current_branch(cwd=root),
        commit=boundary.git_head_commit(cwd=root),
    )
    return WorkingDir(path=root, repo=repo, revision=revision)


def resolve_owner_kind(repo: Repo, *, boundary=gh) -> OwnerKind:
    """The :class:`OwnerKind` of ``repo``'s owner — the ONE API-touching resolver.

    A lazily-resolved enrichment, NOT part of :class:`Repo` identity: it queries
    the owner's account type via the API (:func:`shipit.gh.owner_kind`) and maps
    the raw ``User`` / ``Organization`` onto the closed :class:`OwnerKind`
    registry. Raises :class:`ValueError` (from :class:`OwnerKind`) on an
    unrecognised type.
    """
    raw = boundary.owner_kind(repo.owner.login)
    return OwnerKind(raw.strip().lower())
