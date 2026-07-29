"""The version resolver: the caller SUPPLIES the version, nothing infers it.

An explicit bare semver, or a bump word resolved against the latest version
tag. See docs/adr/0041-tag-authoritative-version-supplied-not-computed.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..changelog import SEMVER_RE, is_prerelease, sort_versions_desc

BUMP_WORDS: tuple[str, ...] = ("major", "minor", "patch")

#: The reserved live-fire suffix: its bump commit travels on the TAG ONLY, so
#: a pipeline-verification cut leaves the branch's version line clean.
RELEASE_RC_PRE: str = "release-rc"

TAG_PREFIX: str = "v"

_ZERO: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True)
class VersionSpec:
    semver: str | None = None
    bump: str | None = None


@dataclass(frozen=True)
class ResolvedVersion:
    """The resolver's verdict: everything prepare branches on, decided pure."""

    version: str
    tag: str
    prerelease: bool
    tag_only: bool
    resume: bool


def parse_spec(raw: str) -> VersionSpec:
    if raw in BUMP_WORDS:
        return VersionSpec(bump=raw)
    if raw[:1] in ("v", "V") and SEMVER_RE.match(raw[1:]):
        raise ValueError(
            f"version must be bare semver without the 'v' prefix (got: {raw}; "
            "the tag decorates, the version string does not — ADR-0041)"
        )
    match = SEMVER_RE.match(raw)
    if match is None:
        words = " | ".join(BUMP_WORDS)
        raise ValueError(
            f"expected a bare semver (e.g. 1.2.3) or a bump word ({words}), got: {raw}"
        )
    if "+" in raw:
        raise ValueError(
            f"build metadata is not allowed in a release version (got: {raw}); "
            "the version is exactly what the tag names"
        )
    return VersionSpec(semver=raw)


def version_tags(tags: list[str] | tuple[str, ...]) -> list[str]:
    """The BARE versions of the ``v<semver>`` tags, newest first; ``+`` disqualifies."""
    versions = [
        tail
        for tag in tags
        if tag.startswith(TAG_PREFIX)
        and "+" not in (tail := tag[len(TAG_PREFIX) :])
        and SEMVER_RE.match(tail)
    ]
    return sort_versions_desc(versions)


def _triple(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(version)
    assert match is not None  # callers pass validated versions
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _bump(word: str, latest: str | None) -> str:
    """Apply ``word`` to the latest version; on a matching PRERELEASE it finalizes."""
    if latest is None:
        major, minor, patch = _ZERO
        pre = False
    else:
        major, minor, patch = _triple(latest)
        pre = is_prerelease(latest)
    if word == "major":
        if pre and minor == 0 and patch == 0:
            return f"{major}.0.0"
        return f"{major + 1}.0.0"
    if word == "minor":
        if pre and patch == 0:
            return f"{major}.{minor}.0"
        return f"{major}.{minor + 1}.0"
    if pre:
        return f"{major}.{minor}.{patch}"
    return f"{major}.{minor}.{patch + 1}"


def resolve(spec: VersionSpec, tags: list[str] | tuple[str, ...]) -> ResolvedVersion:
    """Resolve ``spec`` against the repo's ``tags``; ``resume`` is set when its tag exists."""
    existing = version_tags(tags)
    if spec.semver is not None:
        version = spec.semver
    else:
        assert spec.bump is not None  # parse_spec admits exactly one shape
        version = _bump(spec.bump, existing[0] if existing else None)
    match = SEMVER_RE.match(version)
    pre = match.group("pre") if match else None
    return ResolvedVersion(
        version=version,
        tag=f"{TAG_PREFIX}{version}",
        prerelease=pre is not None,
        tag_only=pre == RELEASE_RC_PRE,
        resume=version in existing,
    )
