"""shipit's canonical git-identity value objects (Repo, Owner, Sha, WorkingDir) and their resolvers.
See docs/adr/0024-core-identities-repo-workingdir-pr.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from . import gh, git


class GitBoundary(Protocol):
    """The narrow git surface the identity resolvers depend on — an injected boundary."""

    def remote_url(self, *, cwd: str, remote: str = "origin") -> str: ...

    def repo_root(self, *, cwd: str | None = None) -> str | None: ...

    def current_branch(self, *, cwd: str) -> str | None: ...

    def head_commit(self, *, cwd: str) -> Sha | None: ...


class OwnerKindBoundary(Protocol):
    def owner_kind(self, login: str) -> str: ...


_FULL_SHA_LENGTHS = (40, 64)

_HEX_RE = re.compile(r"[0-9a-f]+")

_MIN_PREFIX_LEN = 4


@dataclass(frozen=True, eq=False)
class Sha:
    """A validated FULL git object sha, lowercase-normalized; equality is full-vs-full and comparing against a raw ``str`` raises."""

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
        """Whether ``prefix`` abbreviates this sha; a prefix under 4 hex chars or over full length raises."""
        candidate = prefix.strip().lower()
        if (
            len(candidate) < _MIN_PREFIX_LEN
            or len(candidate) > len(self.value)
            or not _HEX_RE.fullmatch(candidate)
        ):
            raise ValueError(f"not a usable sha prefix (4+ hex chars): {prefix!r}")
        return self.value.startswith(candidate)


class OwnerKind(Enum):
    USER = "user"
    ORGANIZATION = "organization"


@dataclass(frozen=True)
class Owner:
    """The account that owns a :class:`Repo`; ``kind`` is an optional enrichment EXCLUDED from equality and hash."""

    login: str
    kind: OwnerKind | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Repo:
    """A GitHub repository as an identity value object, derived locally from the origin remote."""

    owner: Owner
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner.login}/{self.name}"


@dataclass(frozen=True)
class Revision:
    """The revision half of a :class:`WorkingDir`; both fields are best-effort and may be ``None``."""

    branch: str | None = None
    commit: Sha | None = None


@dataclass(frozen=True)
class WorkingDir:
    path: str
    repo: Repo
    revision: Revision


_REMOTE_TAIL = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


def parse_remote_url(url: str) -> tuple[str, str]:
    """``(owner, name)`` from an origin remote URL — HTTPS, SCP-style SSH and ``ssh://`` alike; raises on a tail-less URL."""
    match = _REMOTE_TAIL.search(url.strip())
    if match is None:
        raise ValueError(f"cannot parse owner/name from remote url: {url!r}")
    return match.group("owner"), match.group("name")


def repo_from_slug(slug: str) -> Repo:
    """THE canonical ``owner/name`` slug parser; owner and name are lowercased so one repo is one identity."""
    owner, sep, name = slug.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"not an owner/name slug: {slug!r}")
    return Repo(owner=Owner(login=owner.lower()), name=name.lower())


def resolve_repo(cwd: str = ".", *, boundary: GitBoundary = git) -> Repo:
    """The :class:`Repo` checked out at ``cwd``, derived LOCALLY from origin and lowercased; no API call."""
    url = boundary.remote_url(cwd=cwd)
    owner_login, name = parse_remote_url(url)
    return Repo(owner=Owner(login=owner_login.lower()), name=name.lower())


def resolve_working_dir(cwd: str = ".", *, boundary: GitBoundary = git) -> WorkingDir:
    """The :class:`WorkingDir` at ``cwd``; REQUIRES a checkout, raising rather than fabricating an identity-less one."""
    root = boundary.repo_root(cwd=cwd) or cwd
    repo = resolve_repo(root, boundary=boundary)
    revision = Revision(
        branch=boundary.current_branch(cwd=root),
        commit=boundary.head_commit(cwd=root),
    )
    return WorkingDir(path=root, repo=repo, revision=revision)


def resolve_owner_kind(repo: Repo, *, boundary: OwnerKindBoundary = gh) -> OwnerKind:
    """The :class:`OwnerKind` of ``repo``'s owner — the ONE API-touching resolver, and an enrichment, not identity."""
    raw = boundary.owner_kind(repo.owner.login)
    return OwnerKind(raw.strip().lower())
