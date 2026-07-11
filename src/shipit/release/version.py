"""The version resolver — the Release pipeline's pure core (ADR-0041, PRD 21/23).

The caller SUPPLIES the version — an explicit bare semver, or a bump word
(``major`` / ``minor`` / ``patch``) resolved against the latest existing
version tag. Nothing here infers a version from fragments or commit messages
(ADR-0041: bump-level inference has real ambiguity, and nothing in the fleet
asks for it). Two pure steps, two callers:

- :func:`parse_spec` — argv string → :class:`VersionSpec`, at the CLICK
  boundary (ADR-0030): a leading ``v``, build metadata, or a string that is
  neither semver nor a bump word raises :class:`ValueError` there, so a
  malformed argument dies as a usage error (exit 2) and never reaches a verb
  body.
- :func:`resolve` — (:class:`VersionSpec`, the repo's existing tag names) →
  :class:`ResolvedVersion`: the concrete version, its ``v<version>`` tag, the
  semver-suffix prerelease flag, the ``-release-rc`` live-fire (tag-only)
  flag, and the RESUME verdict — tag already exists → prepare skips the bump
  entirely and re-emits the tag's SHA (ADR-0009's resumability, ADR-0041
  consequence).

Fixture-tested pure core (PRD Testing Decisions); the effectful shell that
feeds it real tags and acts on the verdict is ``shipit release prepare``
(:mod:`shipit.verbs.release`).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..changelog import SEMVER_RE, is_prerelease, sort_versions_desc

#: The bump-word vocabulary (PRD story 21) — resolved against the latest tag.
BUMP_WORDS: tuple[str, ...] = ("major", "minor", "patch")

#: The reserved live-fire prerelease suffix (legacy release#663 verify mode):
#: a ``<semver>-release-rc`` cut is a prerelease whose bump commit travels on
#: the TAG ONLY — the branch ref is never advanced — so pipeline verification
#: cuts leave the branch's version line clean.
RELEASE_RC_PRE: str = "release-rc"

#: The tag prefix — the ONE place ``v`` decorates a version (ADR-0041: the tag
#: decorates, the version string never carries it).
TAG_PREFIX: str = "v"

#: The bump-word base when the repo has no version tag yet: the triple bumps
#: resolve against ``0.0.0`` (so a first ``patch`` cut is ``0.0.1``).
_ZERO: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True)
class VersionSpec:
    """A PARSED version argument — exactly one of the two shapes (story 21).

    ``semver`` is the explicit bare version (validated by :func:`parse_spec`:
    no leading ``v``, no build metadata); ``bump`` is one of
    :data:`BUMP_WORDS`. Construction happens only through :func:`parse_spec`,
    at the click boundary.
    """

    semver: str | None = None
    bump: str | None = None


@dataclass(frozen=True)
class ResolvedVersion:
    """The resolver's verdict — everything prepare branches on, decided pure.

    ``version`` is the concrete bare semver; ``tag`` its ``v``-prefixed tag
    name; ``prerelease`` the semver-suffix detection (``-rc.N``,
    ``-release-rc`` — ADR-0041); ``tag_only`` the ``-release-rc`` live-fire
    contract (push the tag, never advance the branch ref); ``resume`` whether
    the tag ALREADY exists — prepare then skips bump/commit/push and re-emits
    the tag's SHA (ADR-0009).
    """

    version: str
    tag: str
    prerelease: bool
    tag_only: bool
    resume: bool


def parse_spec(raw: str) -> VersionSpec:
    """Parse a version argument into a :class:`VersionSpec`. Pure.

    Accepts a bump word (:data:`BUMP_WORDS`) or a bare semver. Rejections are
    :class:`ValueError` — the click boundary turns each into a usage error
    (exit 2, ADR-0030): a leading ``v`` (the tag decorates, the version string
    does not), build metadata (``+…`` — a release version is exactly what the
    tag names, never annotated), and anything that is neither shape.
    """
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
    """The BARE versions of the ``v<semver>`` tags in ``tags``, descending
    semver order (newest first). Pure.

    Non-version tags (no ``v`` prefix, or an invalid semver after it) are
    ignored — a repo's odd tags (``deploy-2024``, ``tip``) never poison the
    latest-tag resolution.
    """
    versions = [
        tag[len(TAG_PREFIX) :]
        for tag in tags
        if tag.startswith(TAG_PREFIX) and SEMVER_RE.match(tag[len(TAG_PREFIX) :])
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
    """Apply ``word`` to the latest version (``None`` → :data:`_ZERO`). Pure.

    Standard semver increment semantics (the semver crate's): ``major`` →
    ``X+1.0.0``, ``minor`` → ``X.Y+1.0`` — and ``patch`` on a PRERELEASE
    closes it to its own triple (``1.2.3-rc.1`` → ``1.2.3``, the final the rc
    led to), else ``Z+1``.
    """
    if latest is None:
        major, minor, patch = _ZERO
        pre = False
    else:
        major, minor, patch = _triple(latest)
        pre = is_prerelease(latest)
    if word == "major":
        return f"{major + 1}.0.0"
    if word == "minor":
        return f"{major}.{minor + 1}.0"
    if pre:
        return f"{major}.{minor}.{patch}"
    return f"{major}.{minor}.{patch + 1}"


def resolve(spec: VersionSpec, tags: list[str] | tuple[str, ...]) -> ResolvedVersion:
    """Resolve ``spec`` against the repo's existing ``tags``. Pure.

    An explicit semver passes through; a bump word resolves against the
    LATEST version tag (semver §11 order over the ``v<semver>`` tags; no tags
    → ``0.0.0``). ``resume`` is set when the resolved version's tag already
    exists — the caller re-emits that tag's SHA instead of bumping anything
    (ADR-0009/0041). Prerelease stays semver-suffix detection; the reserved
    ``-release-rc`` suffix additionally marks the cut tag-only.
    """
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
