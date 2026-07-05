"""Tests for ``shipit.buildid`` — the build's own commit identity (ADR-0033).

The resolver's three sources are each covered at their seam: the PEP 610
``direct_url.json`` parse is pure (table-tested on record text), the wheel
embed reads a data file next to the package, and the dev-checkout probe rides
``git.head_commit``. The composition — most authoritative first, ``None`` when
everything comes up empty — is driven with the seams patched.
"""

from __future__ import annotations

import json

import pytest

from shipit import buildid
from shipit.identity import Sha

FULL_SHA = "d" * 40


# --------------------------------------------------------------------------
# The pure direct_url.json parse
# --------------------------------------------------------------------------


def test_direct_url_vcs_record_yields_the_commit():
    # The uv/pip git-install shape: vcs_info carries the resolved commit.
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
        json.dumps({"url": "https://example.com/shipit.whl"}),  # plain archive
        json.dumps({"vcs_info": {"vcs": "git"}}),  # no commit_id
        json.dumps({"vcs_info": {"vcs": "git", "commit_id": "abc"}}),  # short
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
    # Every degenerate record degrades to None — the next resolver's turn,
    # never an exception out of the identity probe.
    assert buildid.sha_from_direct_url(text) is None


# --------------------------------------------------------------------------
# The resolution order (seams patched)
# --------------------------------------------------------------------------


def test_direct_url_wins_over_embed_and_checkout(monkeypatch):
    # The install record is the most authoritative: immune to ambient-git
    # confusion (an env living inside some OTHER repo must never stamp that
    # repo's HEAD), so it is consulted first.
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


# --------------------------------------------------------------------------
# The embed and checkout probes
# --------------------------------------------------------------------------


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
    assert buildid._embedded_sha() is None  # absent (the source-checkout norm)
    (pkg / "data" / "build-sha").write_text("not-a-sha\n")
    assert buildid._embedded_sha() is None  # invalid identity


def test_checkout_probe_resolves_this_repos_head():
    # The dev-checkout case, live: this test suite runs from a shipit checkout,
    # so the package directory's repo HEAD resolves to a full sha.
    sha = buildid._checkout_sha()
    assert sha is not None
    assert len(sha.value) in (40, 64)


def test_build_sha_resolves_in_the_dev_checkout():
    # End to end, unpatched: whatever source wins here, the composed resolver
    # yields a FULL validated sha in a dev checkout — the identity `shipit
    # install` stamps as the pin.
    assert buildid.build_sha() is not None
