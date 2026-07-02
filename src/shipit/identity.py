"""identity — shipit's canonical git-identity value objects + resolvers (ADR-0024).

The **deep module** for shipit's core nouns as identity value objects: **`Repo`**
(`owner`, `name`), **`Owner`** (`login`, `kind`) / **`OwnerKind`**, **`Sha`**
(a full commit object id), and
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
them (``resolve_*``), each taking an injectable ``boundary`` — the git reads
default to the :mod:`shipit.git` adapter, the one API-touching resolver to
:mod:`shipit.gh` — so the module is unit-testable in isolation with a fake
boundary and fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from . import gh, git


class GitBoundary(Protocol):
    """The narrow git surface the identity resolvers depend on — an injected boundary.

    Captures ONLY the methods the resolvers actually call (ADR-0021's
    injected-boundary style), not the whole :mod:`shipit.git` adapter: the four
    local, offline git reads that derive identity/revision. A
    :class:`typing.Protocol` (structural) so both the real :mod:`shipit.git`
    module and a test's fake satisfy it without inheriting anything. The one
    API-touching read used solely by :func:`resolve_owner_kind` lives on its own
    :class:`OwnerKindBoundary` — the gh/git adapter split (PROC02-WS03) keeps
    each resolver honest about which tool it rides.
    """

    def remote_url(self, *, cwd: str, remote: str = "origin") -> str: ...

    def repo_root(self, *, cwd: str | None = None) -> str | None: ...

    def current_branch(self, *, cwd: str) -> str | None: ...

    def head_commit(self, *, cwd: str) -> Sha | None: ...


class OwnerKindBoundary(Protocol):
    """The one API-touching read :func:`resolve_owner_kind` needs (:mod:`shipit.gh`)."""

    def owner_kind(self, login: str) -> str: ...


#: The two full git object-id lengths: SHA-1 (40 hex chars) and SHA-256 (64).
_FULL_SHA_LENGTHS = (40, 64)

#: Lowercase hex — what a normalized sha (or sha prefix) must be made of.
_HEX_RE = re.compile(r"[0-9a-f]+")

#: Git's floor for a usable abbreviated sha (``core.abbrev`` never goes below 4);
#: :meth:`Sha.matches_prefix` refuses anything shorter as too ambiguous to name
#: a commit at all.
_MIN_PREFIX_LEN = 4


@dataclass(frozen=True, eq=False)
class Sha:
    """A commit identity as a value object — a validated FULL git object sha.

    Construction is the validity check (the retired ad-hoc "looks like a sha"
    helpers): ``value`` must be full-length hex (40 chars for SHA-1, 64 for
    SHA-256) and is **lowercase-normalized**, so a case-varying source can never
    mint two identities for one commit. Anything else — empty, abbreviated, or
    non-hex — raises :class:`ValueError` at the boundary instead of flowing on as
    a bogus identity.

    Equality is **full-vs-full only**, and only between ``Sha``\\s. Comparing a
    ``Sha`` against a raw ``str`` raises :class:`TypeError` rather than silently
    returning ``False`` — the silent ``==`` between a short/case-varying string
    and a full sha is exactly the bug that flips review staleness (a review
    reads stale because ``"ABC..." != "abc..."``), so it is impossible by
    construction: a prefix cannot BE a ``Sha``, and a raw string refuses to
    compare. Prefix matching is the explicit :meth:`matches_prefix` ask.

    ``__hash__`` deliberately matches ``hash(self.value)``: a dict/set holding
    ``Sha`` keys probed with an equal raw string lands in the same bucket and
    then fails LOUD in ``__eq__`` — a divergent hash would silently miss instead.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError(f"sha must be a str, got {self.value!r}")
        normalized = self.value.strip().lower()
        if len(normalized) not in _FULL_SHA_LENGTHS or not _HEX_RE.fullmatch(
            normalized
        ):
            raise ValueError(
                f"not a full git object sha (40 or 64 hex chars): {self.value!r}"
            )
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sha):
            return self.value == other.value
        if isinstance(other, str):
            raise TypeError(
                "Sha compared against a raw str — construct a Sha for a full sha, "
                "or use Sha.matches_prefix() for an abbreviated one"
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)

    def matches_prefix(self, prefix: str) -> bool:
        """Whether ``prefix`` abbreviates this sha — the EXPLICIT prefix ask.

        The one sanctioned way to compare an abbreviated sha against a full one
        (``==`` refuses). ``prefix`` is lowercase-normalized like the sha itself,
        must be hex, at least :data:`_MIN_PREFIX_LEN` chars (git's own
        abbreviation floor — shorter is too ambiguous to name a commit), and no
        longer than the full sha; anything else raises :class:`ValueError`
        loudly rather than answering ``False`` for a non-prefix.
        """
        candidate = prefix.strip().lower()
        if (
            len(candidate) < _MIN_PREFIX_LEN
            or len(candidate) > len(self.value)
            or not _HEX_RE.fullmatch(candidate)
        ):
            raise ValueError(f"not a usable sha prefix (4+ hex chars): {prefix!r}")
        return self.value.startswith(candidate)


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

    ``commit`` is a :class:`Sha` — commit identity rides the value object end to
    end (PROC03); callers stringify only at a serialization seam (the eval
    record's JSON stamp). Both fields are best-effort and may be ``None`` (a
    detached/unborn HEAD has no branch; an unresolvable HEAD has no commit) — a
    WorkingDir is a *location*, so a missing revision never makes it
    un-constructible.
    """

    branch: str | None = None
    commit: Sha | None = None


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


def repo_from_slug(slug: str) -> Repo:
    """Parse an ``owner/name`` slug into a :class:`Repo` — THE canonical slug parser.

    The one place an ``owner/name`` string becomes an identity, so every feeder —
    logging setup, the ``logs`` verb, the tree/spawn/hook feeders, the review
    producer — normalizes identically instead of hand-rolling ``partition("/")``.
    Owner and name are **lowercased** to match :func:`resolve_repo`, so a
    :class:`Repo` built from an API-supplied slug shares one identity with the same
    repo resolved locally from origin — mixed-case sources can never split one
    repo's identity across divergent Tree paths or log directories (ADR-0024).
    Raises :class:`ValueError` on a slug that is not ``owner/name`` — a malformed
    slug surfaces loudly rather than yielding a bogus identity.
    """
    owner, sep, name = slug.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"not an owner/name slug: {slug!r}")
    return Repo(owner=Owner(login=owner.lower()), name=name.lower())


def resolve_repo(cwd: str = ".", *, boundary: GitBoundary = git) -> Repo:
    """The :class:`Repo` checked out at ``cwd`` — derived LOCALLY from origin.

    Reads ``git remote get-url origin`` (via the injected ``boundary``, default
    the :mod:`shipit.git` adapter) and parses its ``owner/name`` tail — offline and Tree-safe,
    deliberately NOT the API-based ``gh.current_repo()``. Raises :class:`shipit.execrun.ExecError`
    when there is no origin remote and :class:`ValueError` when the URL is
    unparseable.

    Owner and name are **lowercased** to their canonical form: GitHub owner logins
    and repo names are case-INSENSITIVE (``Acme/Widget`` and ``acme/widget`` are one
    repo, and the lowercased slug still resolves via the API), but origin URLs vary
    in case between clones. Normalising HERE makes the :class:`Repo` identity itself
    case-insensitive, so EVERY Repo-keyed join (most load-bearingly the eval store)
    is stable across case-varying origins — the fix belongs at the identity, not at
    each key site.
    """
    url = boundary.remote_url(cwd=cwd)
    owner_login, name = parse_remote_url(url)
    return Repo(owner=Owner(login=owner_login.lower()), name=name.lower())


def resolve_working_dir(cwd: str = ".", *, boundary: GitBoundary = git) -> WorkingDir:
    """The :class:`WorkingDir` at ``cwd`` — its repo-root path, :class:`Repo`, revision.

    The single resolver replacing the ``git rev-parse --show-toplevel``
    re-derivations: the path is the git toplevel (via the one
    :func:`shipit.git.repo_root` boundary), falling back to ``cwd`` only when
    ``repo_root`` yields nothing. The :class:`Repo` and :class:`Revision` are read
    against that root, so identity and revision describe the same checkout.

    This REQUIRES a checkout. A :class:`WorkingDir` *has-a* :class:`Repo`, and a
    :class:`Repo` needs an origin remote, so outside a checkout (no origin) this
    raises :class:`shipit.execrun.ExecError`, propagated from :func:`resolve_repo` — it
    does NOT fabricate an identity-less WorkingDir. Identity resolution is
    local/offline (see :func:`resolve_repo`).
    """
    root = boundary.repo_root(cwd=cwd) or cwd
    repo = resolve_repo(root, boundary=boundary)
    revision = Revision(
        branch=boundary.current_branch(cwd=root),
        commit=boundary.head_commit(cwd=root),
    )
    return WorkingDir(path=root, repo=repo, revision=revision)


def resolve_owner_kind(repo: Repo, *, boundary: OwnerKindBoundary = gh) -> OwnerKind:
    """The :class:`OwnerKind` of ``repo``'s owner — the ONE API-touching resolver.

    A lazily-resolved enrichment, NOT part of :class:`Repo` identity: it queries
    the owner's account type via the API (:func:`shipit.gh.owner_kind`) and maps
    the raw ``User`` / ``Organization`` onto the closed :class:`OwnerKind`
    registry. Raises :class:`ValueError` (from :class:`OwnerKind`) on an
    unrecognised type.
    """
    raw = boundary.owner_kind(repo.owner.login)
    return OwnerKind(raw.strip().lower())
