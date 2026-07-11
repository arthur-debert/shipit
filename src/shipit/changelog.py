"""changelog — the pure core of the language-agnostic release-notes Tool.

The changelog model (docs/dev/workflows.lex §4, PRD docs/legacy-prd/tol01-ci-tools.md
stories 18/26, TOL01-WS06 #554): release notes accumulate as FRAGMENTS under
``CHANGELOG/unreleased-*.md``, one per feature/fix PR — plain markdown
regardless of Toolchain, so nothing here carries per-language logic. The
committed ``CHANGELOG.md`` is a PROJECTION: rendered from ``CHANGELOG/*``
(fragments + per-version section files), never hand-edited. At cut time the
fragments are coalesced into the new version's section and ONE notes text is
emitted for both the git tag annotation and the GitHub release — release notes
exist exactly once, never re-derived in two places.

This module is the PURE half (PRD implementation decisions: pure cores,
effectful shells): recorded inputs — fragment name/body pairs, version-section
texts, a supplied version string — to asserted outputs. No filesystem, no git,
no clock (the date is an input). The effectful shell lives in
:mod:`shipit.verbs.changelog`, which reads the tree, calls down here, and
writes results; its git read rides the git adapter over the one Exec seam
(ADR-0028).

Three decisions of record bind the shapes here:

* **The version is supplied, never inferred** (ADR-0041): :func:`plan_coalesce`
  takes a bare semver string and validates it; nothing reads a version out of
  fragments or commit history. Bump-word resolution against the latest tag is
  the release pipeline's version resolver (TOL02), not this core.
* **Refuse an empty release** (story 26): zero fragments (and no already-cut
  section to resume from) is a hard :class:`ChangelogError` — an empty release
  is almost always a mistake.
* **Prerelease extracts, final rolls** (the legacy ``roll-changelog.sh``
  behavior, forked by copy per ADR-0001): a prerelease cut (semver-suffix
  detection, ADR-0041) emits the notes WITHOUT consuming the fragments —
  prereleases share their entries with the final they lead to; a final cut
  coalesces the fragments into the version's section and consumes them.

The fragment/render file conventions (``unreleased-*.md`` fragments,
``<semver>.md`` version sections, ``legacy.md`` for pre-model history, byte-order
fragment sorting) are forked by copy from release-core's ``changelog`` console
script — the ancestor the PRD's legacy mapping line ("changelog-check + roll →
``changelog``") names — never depended on.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: The fragment directory (repo-root sibling of the rendered file) and the
#: rendered projection the fragment-sync check compares against.
CHANGELOG_DIR = "CHANGELOG"
CHANGELOG_FILE = "CHANGELOG.md"

#: Fragment filenames are ``unreleased-<slug>.md``; one fragment per PR.
FRAGMENT_PREFIX = "unreleased-"
FRAGMENT_SUFFIX = ".md"

#: ``CHANGELOG/`` stems that are neither fragments nor version sections:
#: ``README`` documents the convention, ``legacy`` carries pre-model history
#: appended verbatim at the render's tail.
RESERVED_STEMS = frozenset({"README", "legacy"})

#: The rendered file's first line: the do-not-edit marker that makes the
#: projection self-describing (the fragment-sync check fails any hand edit
#: anyway; this line says WHY and names the regenerator).
RENDER_PREAMBLE = (
    "<!-- generated - do not edit; fragments live in CHANGELOG/ "
    "(`shipit changelog render` regenerates this file) -->"
)


class ChangelogError(RuntimeError):
    """A changelog refusal — empty release, bad version, unsyncable tree.

    Mapped by the shared CLI error shell (:mod:`shipit.verbs._errors`) to one
    ``error: …`` line + exit 1, the uniform Tool failure surface (story 8).
    """


# --------------------------------------------------------------------------
# Semver — validation, prerelease detection, §11 ordering
# --------------------------------------------------------------------------
#
# Forked by copy from release-core's semver-tool-parity regex (ADR-0001): NAT
# identifiers admit no leading zeros; prerelease identifiers are NAT or
# ALPHANUM. Validation is strict and bare — a leading `v` does NOT validate
# (the tag decorates, the version string does not; ADR-0041). PUBLIC: the ONE
# semver grammar in shipit — the release version resolver
# (shipit.release.version) parses and orders against this same regex, so the
# changelog and the release pipeline can never disagree on what a version is.

_NAT = r"(?:0|[1-9][0-9]*)"
_ALPHANUM = r"(?:[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_IDENT = rf"(?:{_NAT}|{_ALPHANUM})"
SEMVER_RE = re.compile(
    rf"^(?P<major>{_NAT})\.(?P<minor>{_NAT})\.(?P<patch>{_NAT})"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def is_semver(version: str) -> bool:
    """Whether ``version`` is a valid BARE semver (no ``v`` prefix). Pure."""
    return bool(SEMVER_RE.match(version))


def is_prerelease(version: str) -> bool:
    """Whether ``version`` carries a prerelease suffix (``-rc.1``,
    ``-release-rc``, …) — the semver-suffix detection ADR-0041 fixes. Pure.
    A non-semver string is not a prerelease (callers validate first)."""
    match = SEMVER_RE.match(version)
    return bool(match and match.group("pre"))


def _prerelease_key(pre: str) -> tuple[tuple[int, object], ...]:
    """A semver §11 sort key for a prerelease suffix: numeric identifiers rank
    below alphanumeric and compare numerically; fewer identifiers rank lower
    when the shared prefix ties (exactly tuple ordering)."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in pre.split(".")
    )


def _version_key(version: str) -> tuple:
    """The full §11 ordering key. A bare release ranks ABOVE its prereleases;
    build metadata is ignored for precedence. Callers validate first — an
    invalid version raises :class:`ChangelogError` (never a silent mis-sort)."""
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
    """Versions in descending semver §11 order (newest first — the render
    order), a bare release above its own prereleases. Pure."""
    return sorted(versions, key=_version_key, reverse=True)


# --------------------------------------------------------------------------
# Fragments and CHANGELOG/ classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fragment:
    """One unreleased fragment: its ``CHANGELOG/`` filename and markdown body."""

    name: str
    body: str


def is_fragment_name(name: str) -> bool:
    """Whether a ``CHANGELOG/`` filename is an unreleased fragment. Pure."""
    return name.startswith(FRAGMENT_PREFIX) and name.endswith(FRAGMENT_SUFFIX)


@dataclass(frozen=True)
class DirListing:
    """A classified ``CHANGELOG/`` directory listing (names only, no bodies).

    ``fragments`` are the ``unreleased-*.md`` names in byte order (the stable
    coalesce/render order); ``versions`` the valid ``<semver>.md`` stems;
    ``invalid`` every ``.md`` name that is neither — a ``v``-prefixed or
    otherwise unparseable stem the caller must refuse loudly (a mis-named
    section would silently vanish from the render)."""

    fragments: tuple[str, ...]
    versions: tuple[str, ...]
    invalid: tuple[str, ...]


def classify_dir(names: Iterable[str]) -> DirListing:
    """Classify a ``CHANGELOG/`` listing into fragments / version sections /
    invalid names. Pure over the name list; non-``.md`` entries and the
    :data:`RESERVED_STEMS` (``README.*``, ``legacy.md``) are ignored."""
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
    # Byte order (ASCII names: codepoint sort == LC_ALL=C) — the one stable,
    # locale-independent fragment order, kept from the legacy renderer.
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


#: A fragment's section heading — the Keep-a-Changelog ``### Added`` /
#: ``### Changed`` level. Exactly three ``#`` (``####`` is nested content);
#: leading and trailing spaces or tabs around the name normalize away for
#: grouping.
_SECTION_RE = re.compile(r"^###[ \t]+(?P<name>\S.*?)[ \t]*$")

#: A code-fence delimiter line (CommonMark §4.5): 0-3 leading spaces then a run
#: of three-or-more backticks or tildes. A fenced block may quote ``### …``
#: lines that must not be mistaken for section headings, so the parser opens on
#: the first such marker and closes only on a MATCHING one — same character, at
#: least as long, with nothing but trailing whitespace after it (an info-string
#: line such as ``` ```python ``` is content, not a close) — leaving the other
#: marker as content in between.
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")


def _split_sections(body: str) -> list[tuple[str | None, str]]:
    """Split one fragment body into ``(section name, chunk)`` blocks. Pure.

    Content before the first ``### <name>`` heading gets section ``None``;
    each heading starts a new block carrying the lines under it (the heading
    line itself is NOT in the chunk — the merger re-emits it canonically).
    ``### …`` lines inside fenced code blocks are content, not headings — a
    fence may be indented up to three spaces and only closes on a matching
    marker (:data:`_FENCE_RE`). ``body`` arrives LF-normalized: fragment bodies
    are decoded with universal newlines, so CRLF and CR endings collapse to LF
    before parsing.
    """
    blocks: list[tuple[str | None, list[str]]] = []
    name: str | None = None
    lines: list[str] = []
    fence: str | None = None  # open fence marker, or None outside any fence
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
    """A section chunk normalized for merging: edge blank lines stripped
    (only newlines — a trailing hard-break space survives), re-terminated."""
    return _terminated(chunk.strip("\n"))


def notes_text(fragments: Sequence[Fragment]) -> str:
    """The coalesced notes body, same-name sections merged. Pure.

    Each fragment carries its own ``### Added`` / ``### Changed`` (etc.)
    headings, so verbatim concatenation would repeat a heading once per
    fragment (#599). Instead, entries group by section name — each heading
    emitted ONCE, sections in first-seen order, entries in fragment order
    within a section, one blank line between sections. Content that carries no
    section heading keeps fragment order ahead of the grouped sections; when
    NO fragment has a section heading the result is the plain concatenation of
    the bodies, each newline-terminated.

    This is THE one text (story 26): the same string feeds the git tag
    annotation and the GitHub release notes, and it is byte-identical to the
    body of the version section a final cut writes (:func:`coalesce_section`
    is header + this), so no consumer ever re-derives notes from a render.
    """
    unheaded: list[str] = []
    groups: dict[str, list[str]] = {}  # insertion-ordered: first-seen section order
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
    """The new ``CHANGELOG/<version>.md`` content: the ``## <version> - <date>``
    heading, a blank line, then :func:`notes_text`. Pure — ``date`` is an input
    (``YYYY-MM-DD``), never read from a clock here."""
    return f"## {version} - {date}\n\n" + notes_text(fragments)


def section_notes(section: str) -> str:
    """The notes body of an already-cut version section: the section text minus
    its leading ``## …`` heading line and the blank line after it. Pure.

    The resume path's inverse of :func:`coalesce_section` — when the cut
    already happened (tag exists, prepare re-runs; ADR-0009 resumability) the
    identical notes text is re-emitted from the committed section instead of
    from fragments that no longer exist.
    """
    lines = section.splitlines(keepends=True)
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
        if lines and lines[0].strip() == "":
            lines = lines[1:]
    return "".join(lines)


# --------------------------------------------------------------------------
# Rendering — CHANGELOG.md as a pure projection of CHANGELOG/*
# --------------------------------------------------------------------------


def render(
    fragments: Sequence[Fragment],
    sections: Mapping[str, str],
    *,
    legacy: str | None = None,
) -> str:
    """The full ``CHANGELOG.md`` text: preamble, ``# Changelog``, the
    ``## Unreleased`` fragments coalesced (same-name ``###`` sections merged,
    :func:`notes_text`), every version section newest-first
    (semver §11 via :func:`sort_versions_desc`), then any ``legacy.md`` tail,
    the whole normalized to exactly one trailing newline. Pure.

    ``sections`` maps each version stem to its ``CHANGELOG/<version>.md``
    content (the section carries its own ``## <version> - <date>`` heading).
    Deterministic: the same tree renders the same bytes anywhere, which is
    exactly what lets the fragment-sync check (:func:`sync_diff`) compare a
    re-render against the committed file.
    """
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
    # Exactly one trailing newline: the loop's unconditional "\n" separator would
    # otherwise leave a blank-line tail (two newlines) when no legacy follows,
    # tripping markdown linters (MD012) and diverging from what a formatter would
    # commit — a spurious sync-check (:func:`sync_diff`) failure. Strip only
    # newlines, not all whitespace, so a significant trailing space (a markdown
    # hard line break at end-of-file, from a section or ``legacy.md``) survives.
    return "".join(parts).rstrip("\n") + "\n"


def sync_diff(rendered: str, committed: str | None) -> str | None:
    """``None`` when the committed ``CHANGELOG.md`` matches a re-render of the
    fragments; otherwise the unified diff (committed → rendered). Pure.

    The fragment-sync check's verdict (story 18): a PR that hand-edits the
    changelog without a fragment, or adds a fragment without re-rendering,
    diverges here and fails BEFORE merge — with the diff surfaced so the fix
    (``shipit changelog render``, commit the result) is obvious. A missing
    committed file diffs against empty, the same loud failure.
    """
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


# --------------------------------------------------------------------------
# Coalesce — the cut-time API (TOL02 prepare's consumer surface)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoalescePlan:
    """What a cut does, computed pure — the shell only executes it.

    ``notes`` is THE one coalesced text (story 26): the tag-annotation and the
    GH-release consumers both take exactly this string. ``section`` is the
    ``CHANGELOG/<version>.md`` content to write on a final cut (``None`` when
    nothing is written: a prerelease extract, or a resume of an already-cut
    version). ``consumed`` names the fragment files a final cut removes —
    empty on a prerelease, whose entries stay for the final they lead to.
    """

    version: str
    prerelease: bool
    notes: str
    section: str | None
    consumed: tuple[str, ...]

    @property
    def mutates(self) -> bool:
        """Whether executing this plan changes the tree (write + re-render)."""
        return self.section is not None


def plan_coalesce(
    version: str,
    fragments: Sequence[Fragment],
    *,
    date: str,
    existing_section: str | None = None,
) -> CoalescePlan:
    """Plan the cut for ``version`` over the unreleased ``fragments``. Pure.

    ``version`` is a SUPPLIED bare semver string (ADR-0041 — never inferred
    from fragments or history; bump words resolve in TOL02's version resolver
    before reaching here). ``existing_section`` is the current
    ``CHANGELOG/<version>.md`` content when that file already exists.

    Outcomes:

    * invalid/empty/``v``-prefixed version → :class:`ChangelogError`;
    * already-cut (``existing_section`` given) with NO fragments → resume:
      re-emit the identical notes from the section, mutate nothing (ADR-0009);
    * already-cut WITH fragments → refuse (overwriting a cut section would
      silently drop released notes — ambiguous, so loud);
    * prerelease + fragments → extract: notes from the fragments, nothing
      written, nothing consumed (the entries belong to the coming final);
    * final + fragments → roll: write the section, consume the fragments;
    * no fragments and nothing to resume → the empty-release refusal.
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
        # Resume (ADR-0009): the cut already happened (tag exists, prepare
        # re-ran) — re-emit the SAME notes text from the committed section.
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
        # Extract, don't roll: a prerelease shares its entries with the final
        # it leads to, so the fragments stay and nothing is written.
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
