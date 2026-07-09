"""Unit tests for the changelog core (TOL01-WS06 #554) — fixture-driven, pure.

Recorded inputs (fragment name/body pairs, version-section texts, a supplied
version string) to asserted outputs: the coalesce plan, the rendered
projection, the sync diff. No filesystem, no git, no clock — the shell's
boundary is tested separately in test_changelog_verb.py.
"""

import pytest

from shipit import changelog as core

# --------------------------------------------------------------------------
# Semver — validation, prerelease detection, §11 ordering
# --------------------------------------------------------------------------


def test_is_semver_accepts_bare_versions():
    assert core.is_semver("1.2.3")
    assert core.is_semver("0.0.1")
    assert core.is_semver("10.20.30")
    assert core.is_semver("1.2.3-rc.1")
    assert core.is_semver("1.2.3-release-rc")
    assert core.is_semver("1.2.3+build.7")


def test_is_semver_rejects_prefixed_and_malformed():
    assert not core.is_semver("v1.2.3")  # the tag decorates, the version doesn't
    assert not core.is_semver("1.2")
    assert not core.is_semver("1.02.3")  # NAT: no leading zeros
    assert not core.is_semver("1.2.3-01")  # numeric prerelease ident, no leading 0
    assert not core.is_semver("")
    assert not core.is_semver("minor")  # bump words resolve in TOL02, not here


def test_is_prerelease_is_semver_suffix_detection():
    # ADR-0041: prerelease detection stays semver-suffix.
    assert core.is_prerelease("1.2.3-rc.1")
    assert core.is_prerelease("3.0.0-release-rc")
    assert not core.is_prerelease("1.2.3")
    assert not core.is_prerelease("1.2.3+build")  # build metadata is not a prerelease


def test_sort_versions_desc_newest_first_release_above_prereleases():
    ordered = core.sort_versions_desc(
        ["1.0.0", "1.2.0-rc.2", "1.2.0", "0.9.9", "1.2.0-rc.10", "1.10.0"]
    )
    assert ordered == [
        "1.10.0",
        "1.2.0",  # the bare release outranks its own prereleases
        "1.2.0-rc.10",  # numeric prerelease idents compare numerically (10 > 2)
        "1.2.0-rc.2",
        "1.0.0",
        "0.9.9",
    ]


def test_sort_versions_rejects_invalid_loudly():
    with pytest.raises(core.ChangelogError, match="not a valid semver"):
        core.sort_versions_desc(["1.2.3", "not-a-version"])


# --------------------------------------------------------------------------
# CHANGELOG/ classification
# --------------------------------------------------------------------------


def test_classify_dir_buckets_fragments_versions_invalid():
    listing = core.classify_dir(
        [
            "unreleased-fix-a.md",
            "unreleased-pr-12.md",
            "1.2.3.md",
            "1.2.3-rc.1.md",
            "v2.0.0.md",  # v-prefixed stem: invalid, must be surfaced
            "notes.md",  # not a fragment, not a semver stem
            "README.txt",  # non-.md: ignored
            "README.md",  # reserved
            "legacy.md",  # reserved (the render tail)
        ]
    )
    assert listing.fragments == ("unreleased-fix-a.md", "unreleased-pr-12.md")
    assert set(listing.versions) == {"1.2.3", "1.2.3-rc.1"}
    assert listing.invalid == ("notes.md", "v2.0.0.md")


def test_classify_dir_fragments_in_byte_order():
    listing = core.classify_dir(
        ["unreleased-z.md", "unreleased-a.md", "unreleased-M.md"]
    )
    # LC_ALL=C byte order: uppercase before lowercase.
    assert listing.fragments == (
        "unreleased-M.md",
        "unreleased-a.md",
        "unreleased-z.md",
    )


# --------------------------------------------------------------------------
# Coalescing — notes text, section, the plan
# --------------------------------------------------------------------------


def _frags(*bodies: str) -> tuple[core.Fragment, ...]:
    return tuple(
        core.Fragment(name=f"unreleased-{i}.md", body=body)
        for i, body in enumerate(bodies)
    )


def test_notes_text_concatenates_newline_terminated():
    frags = _frags("- fix the thing", "- add the other\n")
    assert core.notes_text(frags) == "- fix the thing\n- add the other\n"


def test_coalesce_section_is_header_plus_notes():
    frags = _frags("- a\n", "- b\n")
    section = core.coalesce_section("1.2.3", frags, date="2026-07-08")
    assert section == "## 1.2.3 - 2026-07-08\n\n- a\n- b\n"


def test_section_notes_inverts_coalesce_section():
    # The resume path re-emits the IDENTICAL notes from an already-cut section.
    frags = _frags("- a\n", "- b\n")
    section = core.coalesce_section("1.2.3", frags, date="2026-07-08")
    assert core.section_notes(section) == core.notes_text(frags)


def test_plan_coalesce_final_rolls_and_consumes():
    frags = _frags("- a\n", "- b\n")
    plan = core.plan_coalesce("1.2.3", frags, date="2026-07-08")
    assert plan.version == "1.2.3"
    assert plan.prerelease is False
    assert plan.mutates is True
    assert plan.section == "## 1.2.3 - 2026-07-08\n\n- a\n- b\n"
    assert plan.consumed == ("unreleased-0.md", "unreleased-1.md")
    # ONE text (story 26): the notes ARE the section body — a single function
    # output for both the tag annotation and the GH release, never two renders.
    assert plan.notes == "- a\n- b\n"
    assert plan.section.endswith(plan.notes)


def test_plan_coalesce_prerelease_extracts_without_consuming():
    # The legacy roll's extract-vs-roll distinction: an rc cut must NOT consume
    # the fragments — they belong to the final it leads to.
    frags = _frags("- a\n")
    plan = core.plan_coalesce("1.2.3-rc.1", frags, date="2026-07-08")
    assert plan.prerelease is True
    assert plan.mutates is False
    assert plan.section is None
    assert plan.consumed == ()
    assert plan.notes == "- a\n"


def test_plan_coalesce_refuses_empty_release():
    # Story 26: zero fragments -> hard refusal (an empty release is a mistake).
    with pytest.raises(core.ChangelogError, match="refusing an empty release"):
        core.plan_coalesce("1.2.3", (), date="2026-07-08")
    with pytest.raises(core.ChangelogError, match="refusing an empty release"):
        core.plan_coalesce("1.2.3-rc.1", (), date="2026-07-08")


def test_plan_coalesce_version_is_required_and_validated():
    # ADR-0041: the version is SUPPLIED; none/invalid/v-prefixed are errors —
    # the core never reads a version out of fragments or history.
    frags = _frags("- a\n")
    with pytest.raises(core.ChangelogError, match="version is required"):
        core.plan_coalesce("", frags, date="2026-07-08")
    with pytest.raises(core.ChangelogError, match="without the 'v' prefix"):
        core.plan_coalesce("v1.2.3", frags, date="2026-07-08")
    with pytest.raises(core.ChangelogError, match="must be valid semver"):
        core.plan_coalesce("minor", frags, date="2026-07-08")


def test_plan_coalesce_resumes_an_already_cut_version():
    # ADR-0009 resumability: the cut already happened (tag exists) — re-emit
    # the identical notes from the committed section, mutate nothing.
    section = "## 1.2.3 - 2026-07-01\n\n- shipped earlier\n"
    plan = core.plan_coalesce("1.2.3", (), date="2026-07-08", existing_section=section)
    assert plan.mutates is False
    assert plan.consumed == ()
    assert plan.notes == "- shipped earlier\n"


def test_plan_coalesce_refuses_overwriting_a_cut_section():
    frags = _frags("- new work\n")
    with pytest.raises(core.ChangelogError, match="refusing to overwrite"):
        core.plan_coalesce(
            "1.2.3",
            frags,
            date="2026-07-08",
            existing_section="## 1.2.3 - 2026-07-01\n\n- old\n",
        )


# --------------------------------------------------------------------------
# Rendering and the sync diff
# --------------------------------------------------------------------------


def test_render_shape_preamble_unreleased_versions_desc_legacy():
    frags = _frags("- pending\n")
    sections = {
        "1.0.0": "## 1.0.0 - 2026-01-01\n\n- first\n",
        "1.1.0": "## 1.1.0 - 2026-02-01\n\n- second\n",
    }
    text = core.render(frags, sections, legacy="# Old history\n")
    assert text == (
        f"{core.RENDER_PREAMBLE}\n"
        "\n"
        "# Changelog\n"
        "\n"
        "## Unreleased\n"
        "\n"
        "- pending\n"
        "\n"
        "## 1.1.0 - 2026-02-01\n"
        "\n"
        "- second\n"
        "\n"
        "## 1.0.0 - 2026-01-01\n"
        "\n"
        "- first\n"
        "\n"
        "# Old history\n"
    )


def test_render_no_fragments_no_legacy():
    text = core.render((), {"0.1.0": "## 0.1.0 - 2026-01-01\n\n- x\n"})
    assert "## Unreleased\n\n## 0.1.0" in text
    assert text.endswith("- x\n\n")


def test_render_is_deterministic():
    frags = _frags("- a\n")
    sections = {"1.0.0": "## 1.0.0 - 2026-01-01\n\n- first\n"}
    assert core.render(frags, sections) == core.render(frags, sections)


def test_sync_diff_none_when_in_sync():
    frags = _frags("- a\n")
    rendered = core.render(frags, {})
    assert core.sync_diff(rendered, rendered) is None


def test_sync_diff_surfaces_divergence():
    # Story 18, both directions: a hand-edited CHANGELOG.md (no fragment) and a
    # fragment added without re-rendering are the SAME comparison failing.
    rendered = core.render(_frags("- a\n"), {})
    edited = rendered.replace("- a", "- a (hand-edited)")
    diff = core.sync_diff(rendered, edited)
    assert diff is not None
    assert "(committed)" in diff and "(rendered from CHANGELOG/)" in diff
    assert "-- a (hand-edited)" in diff
    assert "+- a" in diff


def test_sync_diff_missing_committed_file_fails_loud():
    rendered = core.render(_frags("- a\n"), {})
    diff = core.sync_diff(rendered, None)
    assert diff is not None
    assert "+# Changelog" in diff
