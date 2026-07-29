from __future__ import annotations

import json

import pytest

from shipit import buildid
from shipit.identity import Sha

FULL_SHA = "d" * 40


def test_direct_url_vcs_record_yields_the_commit():
    text = json.dumps(
        {
            "url": "https://github.com/arthur-debert/shipit",
            "vcs_info": {"vcs": "git", "commit_id": FULL_SHA},
        }
    )
    assert buildid.sha_from_direct_url(text) == Sha(FULL_SHA)


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        json.dumps({"url": "file:///src/shipit", "dir_info": {"editable": True}}),
        json.dumps({"url": "https://example.com/shipit.whl"}),
        json.dumps({"vcs_info": {"vcs": "git"}}),
        json.dumps({"vcs_info": {"vcs": "git", "commit_id": "abc"}}),
        json.dumps({"vcs_info": "not-a-table"}),
        json.dumps(None),
    ],
    ids=[
        "malformed-json",
        "editable-dir-install",
        "archive-install",
        "no-commit-id",
        "short-sha",
        "mistyped-vcs-info",
        "null",
    ],
)
def test_non_vcs_or_malformed_direct_url_is_none(text: str):
    assert buildid.sha_from_direct_url(text) is None


def test_direct_url_wins_over_embed_and_checkout(monkeypatch):
    monkeypatch.setattr(buildid, "_direct_url_sha", lambda: Sha("a" * 40))
    monkeypatch.setattr(buildid, "_embedded_sha", lambda: Sha("b" * 40))
    monkeypatch.setattr(buildid, "_checkout_sha", lambda: Sha("c" * 40))
    assert buildid.build_sha() == Sha("a" * 40)


def test_embed_wins_over_checkout(monkeypatch):
    monkeypatch.setattr(buildid, "_direct_url_sha", lambda: None)
    monkeypatch.setattr(buildid, "_embedded_sha", lambda: Sha("b" * 40))
    monkeypatch.setattr(buildid, "_checkout_sha", lambda: Sha("c" * 40))
    assert buildid.build_sha() == Sha("b" * 40)


def test_all_sources_empty_is_none(monkeypatch):
    monkeypatch.setattr(buildid, "_direct_url_sha", lambda: None)
    monkeypatch.setattr(buildid, "_embedded_sha", lambda: None)
    monkeypatch.setattr(buildid, "_checkout_sha", lambda: None)
    assert buildid.build_sha() is None


def test_embedded_sha_reads_the_wheel_data_file(tmp_path, monkeypatch):
    pkg = tmp_path / "shipit"
    (pkg / "data").mkdir(parents=True)
    (pkg / "data" / "build-sha").write_text(FULL_SHA + "\n")
    monkeypatch.setattr(buildid, "_package_dir", lambda: pkg)
    assert buildid._embedded_sha() == Sha(FULL_SHA)


def test_embedded_sha_absent_or_invalid_is_none(tmp_path, monkeypatch):
    pkg = tmp_path / "shipit"
    (pkg / "data").mkdir(parents=True)
    monkeypatch.setattr(buildid, "_package_dir", lambda: pkg)
    assert buildid._embedded_sha() is None
    (pkg / "data" / "build-sha").write_text("not-a-sha\n")
    assert buildid._embedded_sha() is None


def test_checkout_probe_resolves_this_repos_head():
    sha = buildid._checkout_sha()
    assert sha is not None
    assert len(sha.value) in (40, 64)


def test_build_sha_resolves_in_the_dev_checkout():
    assert buildid.build_sha() is not None
