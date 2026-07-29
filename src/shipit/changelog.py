"""The pure core of the language-agnostic release-notes Tool: fragments in, notes and renders out.

See docs/adr/0041-tag-authoritative-version-supplied-not-computed.md.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

CHANGELOG_DIR = "CHANGELOG"
CHANGELOG_FILE = "CHANGELOG.md"

FRAGMENT_PREFIX = "unreleased-"
FRAGMENT_SUFFIX = ".md"

RESERVED_STEMS = frozenset({"README", "legacy"})

RENDER_PREAMBLE = (
    "<!-- generated - do not edit; fragments live in CHANGELOG/ "
    "(`shipit changelog render` regenerates this file) -->"
)


class ChangelogError(RuntimeError):
    """A changelog refusal — empty release, bad version, unsyncable tree."""


_NAT = r"(?:0|[1-9][0-9]*)"
_ALPHANUM = r"(?:[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_IDENT = rf"(?:{_NAT}|{_ALPHANUM})"
SEMVER_RE = re.compile(
    rf"^(?P<major>{_NAT})\.(?P<minor>{_NAT})\.(?P<patch>{_NAT})"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def is_semver(version: str) -> bool:
    """Whether ``version`` is a valid BARE semver (no ``v`` prefix)."""
    return bool(SEMVER_RE.match(version))


def is_prerelease(version: str) -> bool:
    """Whether ``version`` carries a prerelease suffix; a non-semver string is not a prerelease."""
    match = SEMVER_RE.match(version)
    return bool(match and match.group("pre"))


def _prerelease_key(pre: str) -> tuple[tuple[int, object], ...]:
    """A semver §11 sort key for a prerelease suffix: numeric identifiers rank below alphanumeric and compare numerically."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in pre.split(".")
    )


def _version_key(version: str) -> tuple:
    """The full §11 ordering key, a bare release ABOVE its prereleases; raises :class:`ChangelogError` on an invalid version."""
    match = SEMVER_RE.match(version)
    if match is None:
        raise ChangelogError(f"not a valid semver version: {version!r}")
    pre = match.group("pre")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        1 if pre is None else 0,
        _prerelease_key(pre) if pre else (),
    )


def sort_versions_desc(versions: Iterable[str]) -> list[str]:
    """Versions in descending semver §11 order — the render order."""
    return sorted(versions, key=_version_key, reverse=True)


@dataclass(frozen=True)
class Fragment:
    """One unreleased fragment: its ``CHANGELOG/`` filename and markdown body."""

    name: str
    body: str


def is_fragment_name(name: str) -> bool:
    return name.startswith(FRAGMENT_PREFIX) and name.endswith(FRAGMENT_SUFFIX)


@dataclass(frozen=True)
class DirListing:
    """A classified ``CHANGELOG/`` listing (names only); ``invalid`` is every ``.md`` name that is neither a fragment nor a valid ``<semver>.md`` stem, which the caller must refuse loudly."""

    fragments: tuple[str, ...]
    versions: tuple[str, ...]
    invalid: tuple[str, ...]


def classify_dir(names: Iterable[str]) -> DirListing:
    """Classify a ``CHANGELOG/`` listing; non-``.md`` entries and the :data:`RESERVED_STEMS` are ignored, and fragments come back in byte order."""
    fragments: list[str] = []
    versions: list[str] = []
    invalid: list[str] = []
    for name in names:
        if not name.endswith(FRAGMENT_SUFFIX):
            continue
        stem = name[: -len(FRAGMENT_SUFFIX)]
        if stem in RESERVED_STEMS:
            continue
        if is_fragment_name(name):
            fragments.append(name)
        elif is_semver(stem):
            versions.append(stem)
        else:
            invalid.append(name)
    fragments.sort()
    invalid.sort()
    return DirListing(
        fragments=tuple(fragments),
        versions=tuple(versions),
        invalid=tuple(invalid),
    )


def _terminated(text: str) -> str:
    """``text`` with a final newline iff non-empty and not already terminated."""
    if text and not text.endswith("\n"):
        return text + "\n"
    return text


_SECTION_RE = re.compile(r"^###[ \t]+(?P<name>\S.*?)[ \t]*$")

_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")


def _split_sections(body: str) -> list[tuple[str | None, str]]:
    """Split an LF-normalized fragment body into ``(section name, chunk)`` blocks; content before the first ``### <name>`` gets ``None``, the heading line is not in the chunk, and ``###`` inside a code fence is content."""
    blocks: list[tuple[str | None, list[str]]] = []
    name: str | None = None
    lines: list[str] = []
    fence: str | None = None
    for line in body.splitlines(keepends=True):
        marker = _FENCE_RE.match(line)
        if marker:
            token = marker.group("marker")
            if fence is None:
                fence = token
            elif (
                token[0] == fence[0]
                and len(token) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = None
        match = None if fence is not None else _SECTION_RE.match(line)
        if match:
            if name is not None or lines:
                blocks.append((name, lines))
            name = match.group("name")
            lines = []
        else:
            lines.append(line)
    if name is not None or lines:
        blocks.append((name, lines))
    return [(block_name, "".join(block_lines)) for block_name, block_lines in blocks]


def _entry(chunk: str) -> str:
    return _terminated(chunk.strip("\n"))


def notes_text(fragments: Sequence[Fragment]) -> str:
    """The coalesced notes body, same-name sections merged: each heading once, sections in first-seen order, entries in fragment order. THE one notes text — tag annotation, GitHub release and the written section are byte-identical to it."""
    unheaded: list[str] = []
    groups: dict[str, list[str]] = {}
    for fragment in fragments:
        for name, chunk in _split_sections(_terminated(fragment.body)):
            if name is None:
                unheaded.append(chunk)
            else:
                groups.setdefault(name, []).append(chunk)
    if not groups:
        return "".join(unheaded)
    sections = []
    for name, chunks in groups.items():
        entries = "".join(_entry(chunk) for chunk in chunks)
        sections.append(f"### {name}\n\n{entries}" if entries else f"### {name}\n")
    lead = "".join(unheaded).strip("\n")
    prefix = _terminated(lead) + "\n" if lead else ""
    return prefix + "\n".join(sections)


def coalesce_section(version: str, fragments: Sequence[Fragment], *, date: str) -> str:
    """The new ``CHANGELOG/<version>.md`` content: the ``## <version> - <date>`` heading, a blank line, then :func:`notes_text`. ``date`` is an input, never a clock read."""
    return f"## {version} - {date}\n\n" + notes_text(fragments)


def section_notes(section: str) -> str:
    """The notes body of an already-cut version section — the inverse of :func:`coalesce_section`, used to re-emit identical notes on a resume."""
    lines = section.splitlines(keepends=True)
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
        if lines and lines[0].strip() == "":
            lines = lines[1:]
    return "".join(lines)


def render(
    fragments: Sequence[Fragment],
    sections: Mapping[str, str],
    *,
    legacy: str | None = None,
) -> str:
    """The full ``CHANGELOG.md`` text — preamble, ``# Changelog``, coalesced fragments, every ``sections`` version newest-first, then any legacy tail — deterministic to exactly one trailing newline, which is what lets :func:`sync_diff` compare it to the committed file."""
    parts: list[str] = [RENDER_PREAMBLE, "\n\n# Changelog\n\n## Unreleased\n\n"]
    unreleased = notes_text(fragments)
    if unreleased:
        parts.append(unreleased)
        parts.append("\n")
    for version in sort_versions_desc(sections):
        parts.append(_terminated(sections[version]))
        parts.append("\n")
    if legacy is not None:
        parts.append(legacy)
    return "".join(parts).rstrip("\n") + "\n"


def sync_diff(rendered: str, committed: str | None) -> str | None:
    """``None`` when ``committed`` matches ``rendered``, else the unified diff; a missing committed file diffs against empty."""
    actual = committed if committed is not None else ""
    if actual == rendered:
        return None
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        rendered.splitlines(keepends=True),
        fromfile=f"{CHANGELOG_FILE} (committed)",
        tofile=f"{CHANGELOG_FILE} (rendered from {CHANGELOG_DIR}/)",
    )
    return "".join(diff)


@dataclass(frozen=True)
class CoalescePlan:
    """What a cut does, computed pure — the shell only executes it. ``section`` is ``None`` when nothing is written, and ``consumed`` is empty on a prerelease, whose fragments stay for the final they lead to."""

    version: str
    prerelease: bool
    notes: str
    section: str | None
    consumed: tuple[str, ...]

    @property
    def mutates(self) -> bool:
        return self.section is not None


def plan_coalesce(
    version: str,
    fragments: Sequence[Fragment],
    *,
    date: str,
    existing_section: str | None = None,
) -> CoalescePlan:
    """Plan the cut for ``version`` (a SUPPLIED bare semver) over the unreleased ``fragments``.

    ``existing_section`` is the current ``CHANGELOG/<version>.md`` content when that
    file exists: with no fragments the cut resumes from it, with fragments it refuses.
    A prerelease extracts without consuming; a final rolls. An invalid version, and
    having neither fragments nor a section to resume, raise :class:`ChangelogError`.
    """
    if not version:
        raise ChangelogError(
            "a version is required (a bare semver, e.g. 1.2.3 — ADR-0041: "
            "the version is supplied, never inferred from fragments)"
        )
    if version[:1] in ("v", "V") and is_semver(version[1:]):
        raise ChangelogError(
            f"version must be bare semver without the 'v' prefix (got: {version})"
        )
    if not is_semver(version):
        raise ChangelogError(f"version must be valid semver (got: {version})")

    if existing_section is not None:
        if fragments:
            names = ", ".join(f.name for f in fragments)
            raise ChangelogError(
                f"{CHANGELOG_DIR}/{version}{FRAGMENT_SUFFIX} already exists but "
                f"unreleased fragments remain ({names}); refusing to overwrite "
                "an already-cut section — cut a new version for new fragments"
            )
        return CoalescePlan(
            version=version,
            prerelease=is_prerelease(version),
            notes=section_notes(existing_section),
            section=None,
            consumed=(),
        )

    if not fragments:
        raise ChangelogError(
            f"no {CHANGELOG_DIR}/{FRAGMENT_PREFIX}*{FRAGMENT_SUFFIX} fragments — "
            "refusing an empty release (add a fragment per feature/fix PR)"
        )

    if is_prerelease(version):
        return CoalescePlan(
            version=version,
            prerelease=True,
            notes=notes_text(fragments),
            section=None,
            consumed=(),
        )

    return CoalescePlan(
        version=version,
        prerelease=False,
        notes=notes_text(fragments),
        section=coalesce_section(version, fragments, date=date),
        consumed=tuple(f.name for f in fragments),
    )
