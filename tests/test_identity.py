from __future__ import annotations

import dataclasses

import pytest

from shipit import identity
from shipit.identity import (
    Owner,
    OwnerKind,
    Repo,
    Revision,
    Sha,
    WorkingDir,
    parse_remote_url,
    repo_from_slug,
    resolve_owner_kind,
    resolve_repo,
    resolve_working_dir,
)


class FakeGit:
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

    def remote_url(self, *, cwd, remote="origin"):
        self.remote_url_cwds.append(cwd)
        return self._remote_url

    def repo_root(self, *, cwd=None):
        self.toplevel_cwds.append(cwd)
        return self._toplevel

    def current_branch(self, *, cwd):
        return self._branch

    def head_commit(self, *, cwd):
        return self._commit

    def owner_kind(self, login):
        if self._owner_type is None:
            raise AssertionError(
                "owner_kind must not be called for identity resolution"
            )
        return self._owner_type


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
        ("https://github.com/arthur-debert/repo.js.git", ("arthur-debert", "repo.js")),
    ],
)
def test_parse_remote_url_across_shapes(url, expected):
    assert parse_remote_url(url) == expected


def test_parse_remote_url_rejects_a_urlless_string():
    with pytest.raises(ValueError):
        parse_remote_url("not-a-remote")


def test_ownerkind_is_excluded_from_owner_equality_and_hash():
    bare = Owner(login="acme")
    enriched = Owner(login="acme", kind=OwnerKind.ORGANIZATION)
    other_kind = Owner(login="acme", kind=OwnerKind.USER)
    assert bare == enriched == other_kind
    assert hash(bare) == hash(enriched) == hash(other_kind)
    assert len({bare, enriched, other_kind}) == 1


def test_repo_identity_ignores_owner_kind():
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
    with pytest.raises(dataclasses.FrozenInstanceError):
        repo.name = "other"  # type: ignore[misc]


def test_resolve_repo_derives_identity_locally_offline():
    git = FakeGit(remote_url="git@github.com:acme/widget.git")
    repo = resolve_repo("/checkout/widget/src/deep", boundary=git)
    assert repo == Repo(owner=Owner("acme"), name="widget")
    assert repo.owner.kind is None
    assert git.remote_url_cwds == ["/checkout/widget/src/deep"]


def test_resolve_repo_is_case_insensitive_like_github():
    mixed = resolve_repo(
        "/checkout", boundary=FakeGit(remote_url="git@github.com:Acme/Widget.git")
    )
    lower = resolve_repo(
        "/checkout", boundary=FakeGit(remote_url="https://github.com/acme/widget")
    )
    assert mixed == lower == Repo(owner=Owner("acme"), name="widget")
    assert hash(mixed) == hash(lower)


def test_resolve_working_dir_composes_path_repo_and_revision():
    git = FakeGit(
        toplevel="/checkout/widget",
        branch="COR01/WS01",
        commit=Sha("cafe1234" + "0" * 32),
    )
    wd = resolve_working_dir("/checkout/widget/src", boundary=git)
    assert wd == WorkingDir(
        path="/checkout/widget",
        repo=Repo(owner=Owner("acme"), name="widget"),
        revision=Revision(branch="COR01/WS01", commit=Sha("cafe1234" + "0" * 32)),
    )
    assert git.toplevel_cwds == ["/checkout/widget/src"]
    assert git.remote_url_cwds == ["/checkout/widget"]


def test_resolve_working_dir_falls_back_to_cwd_when_no_toplevel():
    git = FakeGit(toplevel=None)
    wd = resolve_working_dir("/some/dir", boundary=git)
    assert wd.path == "/some/dir"
    assert git.remote_url_cwds == ["/some/dir"]


def test_repo_from_slug_matches_local_identity():
    assert repo_from_slug("Octocat/Hello-World") == Repo(
        owner=Owner(login="octocat"), name="hello-world"
    )
    assert repo_from_slug("Octocat/Hello-World").slug == "octocat/hello-world"


def test_repo_from_slug_agrees_with_resolve_repo_across_case_variants():
    git = FakeGit(remote_url="git@github.com:AcMe/WiDgEt.git")
    assert resolve_repo("/checkout", boundary=git) == repo_from_slug("ACME/Widget")


@pytest.mark.parametrize("bad", ["", "noslash", "owner/", "/name", "a/b/c"])
def test_repo_from_slug_rejects_malformed(bad):
    with pytest.raises(ValueError):
        repo_from_slug(bad)


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


def test_default_boundaries_are_the_tool_adapters():
    import inspect

    for resolver in (resolve_repo, resolve_working_dir):
        assert (
            inspect.signature(resolver).parameters["boundary"].default is identity.git
        )
    assert (
        inspect.signature(resolve_owner_kind).parameters["boundary"].default
        is identity.gh
    )
