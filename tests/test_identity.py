"""identity — value objects + resolvers, unit-tested in isolation (ADR-0024).

Covers the three load-bearing properties: (1) `Repo` identity derives LOCALLY from
the origin remote and works with NO network (an injected fake boundary that would
raise on any API call); (2) `OwnerKind` is an optional enrichment EXCLUDED from
`Repo` equality/hash; (3) the resolvers are free functions over a git boundary,
injectable so the module needs neither git nor `gh` to test.
"""

from __future__ import annotations

import pytest

from shipit import identity
from shipit.identity import (
    Owner,
    OwnerKind,
    Repo,
    Revision,
    WorkingDir,
    parse_remote_url,
    resolve_owner_kind,
    resolve_repo,
    resolve_working_dir,
)


class FakeGit:
    """A stand-in git boundary — no subprocess, no network.

    ``owner_kind`` defaults to raising, so any resolver that touches it without the
    test opting in fails loudly — proving identity resolution never needs the API.
    """

    def __init__(
        self,
        *,
        remote_url="git@github.com:acme/widget.git",
        toplevel="/checkout/widget",
        branch="main",
        commit="deadbeef",
        owner_type=None,
    ):
        self._remote_url = remote_url
        self._toplevel = toplevel
        self._branch = branch
        self._commit = commit
        self._owner_type = owner_type
        self.remote_url_cwds: list[str] = []
        self.toplevel_cwds: list[str] = []

    def git_remote_url(self, *, cwd, remote="origin"):
        self.remote_url_cwds.append(cwd)
        return self._remote_url

    def repo_root(self, *, cwd=None):
        self.toplevel_cwds.append(cwd)
        return self._toplevel

    def git_current_branch(self, *, cwd):
        return self._branch

    def git_head_commit(self, *, cwd):
        return self._commit

    def owner_kind(self, login):
        if self._owner_type is None:
            raise AssertionError(
                "owner_kind must not be called for identity resolution"
            )
        return self._owner_type


# ---------------------------------------------------------------------------
# parse_remote_url — the pure parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:acme/widget.git", ("acme", "widget")),
        ("git@github.com:acme/widget", ("acme", "widget")),
        ("https://github.com/acme/widget.git", ("acme", "widget")),
        ("https://github.com/acme/widget", ("acme", "widget")),
        ("ssh://git@github.com/acme/widget.git", ("acme", "widget")),
        ("https://github.com/acme/widget/", ("acme", "widget")),
        ("  git@github.com:acme/widget.git\n", ("acme", "widget")),
        # A dotted repo name keeps its dots; only a trailing `.git` is stripped.
        ("https://github.com/arthur-debert/repo.js.git", ("arthur-debert", "repo.js")),
    ],
)
def test_parse_remote_url_across_shapes(url, expected):
    assert parse_remote_url(url) == expected


def test_parse_remote_url_rejects_a_urlless_string():
    with pytest.raises(ValueError):
        parse_remote_url("not-a-remote")


# ---------------------------------------------------------------------------
# Value objects — identity, equality, composition
# ---------------------------------------------------------------------------


def test_ownerkind_is_excluded_from_owner_equality_and_hash():
    bare = Owner(login="acme")
    enriched = Owner(login="acme", kind=OwnerKind.ORGANIZATION)
    other_kind = Owner(login="acme", kind=OwnerKind.USER)
    # Same login → same identity regardless of kind (equality AND hash).
    assert bare == enriched == other_kind
    assert hash(bare) == hash(enriched) == hash(other_kind)
    assert len({bare, enriched, other_kind}) == 1


def test_repo_identity_ignores_owner_kind():
    # A Repo composes an Owner, so kind enrichment must not move Repo identity.
    bare = Repo(owner=Owner("acme"), name="widget")
    enriched = Repo(owner=Owner("acme", OwnerKind.ORGANIZATION), name="widget")
    assert bare == enriched
    assert hash(bare) == hash(enriched)


def test_repo_slug_is_owner_slash_name():
    assert (
        Repo(owner=Owner("arthur-debert"), name="shipit").slug == "arthur-debert/shipit"
    )


def test_value_objects_are_frozen():
    repo = Repo(owner=Owner("acme"), name="widget")
    with pytest.raises(Exception):
        repo.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Resolvers — free functions over an injected boundary
# ---------------------------------------------------------------------------


def test_resolve_repo_derives_identity_locally_offline():
    # No network: FakeGit.owner_kind raises, so a passing resolve proves identity
    # comes purely from the LOCAL origin remote, never an API call.
    git = FakeGit(remote_url="git@github.com:acme/widget.git")
    repo = resolve_repo("/checkout/widget/src/deep", boundary=git)
    assert repo == Repo(owner=Owner("acme"), name="widget")
    assert repo.owner.kind is None  # kind is not resolved during identity
    assert git.remote_url_cwds == ["/checkout/widget/src/deep"]


def test_resolve_repo_is_case_insensitive_like_github():
    # GitHub owner/repo are case-INSENSITIVE, but origin URLs vary in case between
    # clones (`Acme/Widget` vs `acme/widget`). resolve_repo lowercases to the
    # canonical form so both clones yield the SAME Repo identity — otherwise the
    # single-store goal fragments per case, the same class as the `-` collision.
    mixed = resolve_repo(
        "/checkout", boundary=FakeGit(remote_url="git@github.com:Acme/Widget.git")
    )
    lower = resolve_repo(
        "/checkout", boundary=FakeGit(remote_url="https://github.com/acme/widget")
    )
    assert mixed == lower == Repo(owner=Owner("acme"), name="widget")
    assert hash(mixed) == hash(lower)


def test_resolve_working_dir_composes_path_repo_and_revision():
    git = FakeGit(toplevel="/checkout/widget", branch="COR01/WS01", commit="cafe1234")
    wd = resolve_working_dir("/checkout/widget/src", boundary=git)
    assert wd == WorkingDir(
        path="/checkout/widget",
        repo=Repo(owner=Owner("acme"), name="widget"),
        revision=Revision(branch="COR01/WS01", commit="cafe1234"),
    )
    # The toplevel is resolved from the given cwd; identity/revision read the ROOT.
    assert git.toplevel_cwds == ["/checkout/widget/src"]
    assert git.remote_url_cwds == ["/checkout/widget"]


def test_resolve_working_dir_falls_back_to_cwd_when_no_toplevel():
    git = FakeGit(toplevel=None)
    wd = resolve_working_dir("/some/dir", boundary=git)
    assert wd.path == "/some/dir"
    assert git.remote_url_cwds == ["/some/dir"]


def test_resolve_owner_kind_is_the_only_api_touching_resolver():
    git = FakeGit(owner_type="Organization")
    repo = Repo(owner=Owner("acme"), name="widget")
    assert resolve_owner_kind(repo, boundary=git) == OwnerKind.ORGANIZATION


def test_resolve_owner_kind_maps_user():
    git = FakeGit(owner_type="User")
    repo = Repo(owner=Owner("someone"), name="dotfiles")
    assert resolve_owner_kind(repo, boundary=git) == OwnerKind.USER


def test_resolve_owner_kind_rejects_unknown_type():
    git = FakeGit(owner_type="Bot")
    repo = Repo(owner=Owner("acme"), name="widget")
    with pytest.raises(ValueError):
        resolve_owner_kind(repo, boundary=git)


def test_default_boundary_is_the_gh_module():
    # The resolvers default their boundary to `shipit.gh`, so production callers get
    # the real git/gh implementation without threading it through every call site.
    import inspect

    for resolver in (resolve_repo, resolve_working_dir, resolve_owner_kind):
        assert inspect.signature(resolver).parameters["boundary"].default is identity.gh
