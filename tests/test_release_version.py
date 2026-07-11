"""The version resolver's fixture tests (TOL02-WS01, PRD Testing Decisions).

Pure core, full unit coverage: spec parsing (bump words, explicit semver, the
usage-tier rejections), bump-word resolution against the latest tag,
prerelease suffix detection (``-rc.N``, ``-release-rc``), and resume
detection (ADR-0041/0009).
"""

import pytest

from shipit.release import version as v

# --------------------------------------------------------------------------
# parse_spec — the click boundary's parser (usage tier, ADR-0030)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["major", "minor", "patch"])
def test_parse_spec_bump_words(word):
    assert v.parse_spec(word) == v.VersionSpec(bump=word)


@pytest.mark.parametrize("raw", ["1.2.3", "0.0.1", "1.2.3-rc.1", "10.20.30-release-rc"])
def test_parse_spec_explicit_semver(raw):
    assert v.parse_spec(raw) == v.VersionSpec(semver=raw)


@pytest.mark.parametrize("raw", ["v1.2.3", "V1.2.3", "v1.2.3-rc.1"])
def test_parse_spec_rejects_leading_v(raw):
    """The tag decorates; the version string never carries the prefix (ADR-0041)."""
    with pytest.raises(ValueError, match="without the 'v' prefix"):
        v.parse_spec(raw)


@pytest.mark.parametrize("raw", ["1.2.3+build", "1.2.3-rc.1+abc.7"])
def test_parse_spec_rejects_build_metadata(raw):
    with pytest.raises(ValueError, match="build metadata"):
        v.parse_spec(raw)


@pytest.mark.parametrize("raw", ["", "1.2", "latest", "1.2.3.4", "Major", "01.2.3"])
def test_parse_spec_rejects_garbage(raw):
    with pytest.raises(ValueError, match="bare semver .* or a bump word"):
        v.parse_spec(raw)


# --------------------------------------------------------------------------
# version_tags — tag-name filtering and ordering
# --------------------------------------------------------------------------


def test_version_tags_filters_and_orders():
    tags = ["v1.2.3", "deploy-2024", "v1.10.0", "tip", "v1.2.4-rc.1", "1.9.9"]
    assert v.version_tags(tags) == ["1.10.0", "1.2.4-rc.1", "1.2.3"]


def test_version_tags_release_ranks_above_its_prereleases():
    assert v.version_tags(["v1.2.3-rc.2", "v1.2.3"]) == ["1.2.3", "1.2.3-rc.2"]


# --------------------------------------------------------------------------
# resolve — bump words, prerelease flags, resume detection
# --------------------------------------------------------------------------

_TAGS = ["v1.2.3", "v1.2.2", "v0.9.0", "not-a-version"]


@pytest.mark.parametrize(
    ("word", "expected"),
    [("major", "2.0.0"), ("minor", "1.3.0"), ("patch", "1.2.4")],
)
def test_resolve_bump_words_against_latest_tag(word, expected):
    resolved = v.resolve(v.VersionSpec(bump=word), _TAGS)
    assert resolved.version == expected
    assert resolved.tag == f"v{expected}"
    assert not resolved.prerelease
    assert not resolved.resume


@pytest.mark.parametrize(
    ("word", "expected"),
    [("major", "1.0.0"), ("minor", "0.1.0"), ("patch", "0.0.1")],
)
def test_resolve_bump_words_with_no_tags(word, expected):
    assert v.resolve(v.VersionSpec(bump=word), []).version == expected


def test_resolve_patch_closes_a_prerelease():
    """``patch`` on a prerelease latest resolves to the final it led to."""
    resolved = v.resolve(v.VersionSpec(bump="patch"), ["v1.2.3-rc.2", "v1.2.2"])
    assert resolved.version == "1.2.3"


def test_resolve_minor_from_a_prerelease_latest():
    assert v.resolve(v.VersionSpec(bump="minor"), ["v1.2.3-rc.1"]).version == "1.3.0"


def test_resolve_explicit_semver_passes_through():
    resolved = v.resolve(v.VersionSpec(semver="3.0.2"), _TAGS)
    assert resolved.version == "3.0.2"
    assert resolved.tag == "v3.0.2"
    assert not (resolved.prerelease or resolved.tag_only or resolved.resume)


def test_resolve_detects_rc_prerelease():
    resolved = v.resolve(v.VersionSpec(semver="1.3.0-rc.1"), _TAGS)
    assert resolved.prerelease
    assert not resolved.tag_only


def test_resolve_detects_release_rc_as_tag_only_prerelease():
    """The reserved live-fire suffix: prerelease AND tag-only (release#663)."""
    resolved = v.resolve(v.VersionSpec(semver="1.3.0-release-rc"), _TAGS)
    assert resolved.prerelease
    assert resolved.tag_only


def test_resolve_detects_resume_when_tag_exists():
    """Tag exists → prepare skips the bump and re-emits the SHA (ADR-0009)."""
    resolved = v.resolve(v.VersionSpec(semver="1.2.3"), _TAGS)
    assert resolved.resume


def test_resolve_resume_via_bump_word():
    """A bump word that lands on an existing tag is a resume too."""
    resolved = v.resolve(v.VersionSpec(bump="patch"), ["v1.2.3", "v1.2.4"])
    # latest is 1.2.4; patch -> 1.2.5, which does not exist
    assert resolved.version == "1.2.5"
    assert not resolved.resume
    # but an explicit re-run of the existing version resumes
    assert v.resolve(v.VersionSpec(semver="1.2.4"), ["v1.2.3", "v1.2.4"]).resume
