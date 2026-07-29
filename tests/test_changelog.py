import pytest

from shipit import changelog as core


def test_is_semver_accepts_bare_versions():
    assert core.is_semver("1.2.3")
    assert core.is_semver("0.0.1")
    assert core.is_semver("10.20.30")
    assert core.is_semver("1.2.3-rc.1")
    assert core.is_semver("1.2.3-release-rc")
    assert core.is_semver("1.2.3+build.7")


def test_is_semver_rejects_prefixed_and_malformed():
    assert not core.is_semver("v1.2.3")
    assert not core.is_semver("1.2")
    assert not core.is_semver("1.02.3")
    assert not core.is_semver("1.2.3-01")
    assert not core.is_semver("")
    assert not core.is_semver("minor")


def test_is_prerelease_is_semver_suffix_detection():
    assert core.is_prerelease("1.2.3-rc.1")
    assert core.is_prerelease("3.0.0-release-rc")
    assert not core.is_prerelease("1.2.3")
    assert not core.is_prerelease("1.2.3+build")


def test_sort_versions_desc_newest_first_release_above_prereleases():
    ordered = core.sort_versions_desc(
        ["1.0.0", "1.2.0-rc.2", "1.2.0", "0.9.9", "1.2.0-rc.10", "1.10.0"]
    )
    assert ordered == [
        "1.10.0",
        "1.2.0",
        "1.2.0-rc.10",
        "1.2.0-rc.2",
        "1.0.0",
        "0.9.9",
    ]


def test_sort_versions_rejects_invalid_loudly():
    with pytest.raises(core.ChangelogError, match="not a valid semver"):
        core.sort_versions_desc(["1.2.3", "not-a-version"])


def test_classify_dir_buckets_fragments_versions_invalid():
    listing = core.classify_dir(
        [
            "unreleased-fix-a.md",
            "unreleased-pr-12.md",
            "1.2.3.md",
            "1.2.3-rc.1.md",
            "v2.0.0.md",
            "notes.md",
            "README.txt",
            "README.md",
            "legacy.md",
        ]
    )
    assert listing.fragments == ("unreleased-fix-a.md", "unreleased-pr-12.md")
    assert set(listing.versions) == {"1.2.3", "1.2.3-rc.1"}
    assert listing.invalid == ("notes.md", "v2.0.0.md")


def test_classify_dir_fragments_in_byte_order():
    listing = core.classify_dir(
        ["unreleased-z.md", "unreleased-a.md", "unreleased-M.md"]
    )
    assert listing.fragments == (
        "unreleased-M.md",
        "unreleased-a.md",
        "unreleased-z.md",
    )


def _frags(*bodies: str) -> tuple[core.Fragment, ...]:
    return tuple(
        core.Fragment(name=f"unreleased-{i}.md", body=body)
        for i, body in enumerate(bodies)
    )


def test_notes_text_concatenates_newline_terminated():
    frags = _frags("- fix the thing", "- add the other\n")
    assert core.notes_text(frags) == "- fix the thing\n- add the other\n"


def test_notes_text_merges_same_name_sections_across_fragments():
    frags = _frags("### Changed\n\n- a\n", "### Changed\n\n- b\n")
    assert core.notes_text(frags) == "### Changed\n\n- a\n- b\n"


def test_notes_text_groups_sections_in_first_seen_order():
    frags = _frags(
        "### Changed\n\n- a\n",
        "### Fixed\n\n- f\n",
        "### Changed\n\n- b\n",
    )
    assert core.notes_text(frags) == ("### Changed\n\n- a\n- b\n\n### Fixed\n\n- f\n")


def test_notes_text_unheaded_content_precedes_sections():
    frags = _frags("- bare\n", "### Fixed\n\n- f\n")
    assert core.notes_text(frags) == "- bare\n\n### Fixed\n\n- f\n"


def test_notes_text_grouping_normalizes_heading_whitespace():
    frags = _frags("###  Changed \n\n- a\n", "### Changed\n\n- b\n")
    assert core.notes_text(frags) == "### Changed\n\n- a\n- b\n"


def test_notes_text_ignores_headings_inside_code_fences():
    frags = _frags(
        "### Notes\n\n```\n### Changed\n```\n",
        "### Notes\n\n- more\n",
    )
    assert core.notes_text(frags) == ("### Notes\n\n```\n### Changed\n```\n- more\n")


def test_notes_text_indented_code_fence_hides_headings():
    frags = _frags(
        "### Notes\n\n   ```\n### Changed\n   ```\n",
        "### Notes\n\n- more\n",
    )
    assert core.notes_text(frags) == (
        "### Notes\n\n   ```\n### Changed\n   ```\n- more\n"
    )


def test_notes_text_mismatched_fence_marker_stays_in_fence():
    frags = _frags(
        "### Notes\n\n```\n~~~\n### Changed\n```\n",
        "### Notes\n\n- more\n",
    )
    assert core.notes_text(frags) == (
        "### Notes\n\n```\n~~~\n### Changed\n```\n- more\n"
    )


def test_notes_text_info_string_line_does_not_close_fence():
    frags = _frags(
        "### Notes\n\n```\n```python\n### Changed\n```\n",
        "### Notes\n\n- more\n",
    )
    assert core.notes_text(frags) == (
        "### Notes\n\n```\n```python\n### Changed\n```\n- more\n"
    )


def test_notes_text_grouping_normalizes_tab_after_hashes():
    frags = _frags("###\tChanged\n\n- a\n", "### Changed\n\n- b\n")
    assert core.notes_text(frags) == "### Changed\n\n- a\n- b\n"


def test_notes_text_deeper_headings_stay_within_their_section():
    frags = _frags("### Changed\n\n#### details\n\n- a\n", "### Changed\n\n- b\n")
    assert core.notes_text(frags) == "### Changed\n\n#### details\n\n- a\n- b\n"


def test_plan_coalesce_section_merges_same_name_sections():
    frags = _frags("### Changed\n\n- a\n", "### Changed\n\n- b\n")
    plan = core.plan_coalesce("1.2.3", frags, date="2026-07-08")
    assert plan.notes == "### Changed\n\n- a\n- b\n"
    assert plan.section == "## 1.2.3 - 2026-07-08\n\n### Changed\n\n- a\n- b\n"
    assert plan.section.endswith(plan.notes)


def test_render_unreleased_merges_same_name_sections():
    frags = _frags("### Changed\n\n- a\n", "### Changed\n\n- b\n")
    text = core.render(frags, {})
    assert text.count("### Changed") == 1
    assert "## Unreleased\n\n### Changed\n\n- a\n- b\n" in text


def test_coalesce_section_is_header_plus_notes():
    frags = _frags("- a\n", "- b\n")
    section = core.coalesce_section("1.2.3", frags, date="2026-07-08")
    assert section == "## 1.2.3 - 2026-07-08\n\n- a\n- b\n"


def test_section_notes_inverts_coalesce_section():
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
    assert plan.notes == "- a\n- b\n"
    assert plan.section.endswith(plan.notes)


def test_plan_coalesce_prerelease_extracts_without_consuming():
    frags = _frags("- a\n")
    plan = core.plan_coalesce("1.2.3-rc.1", frags, date="2026-07-08")
    assert plan.prerelease is True
    assert plan.mutates is False
    assert plan.section is None
    assert plan.consumed == ()
    assert plan.notes == "- a\n"


def test_plan_coalesce_refuses_empty_release():
    with pytest.raises(core.ChangelogError, match="refusing an empty release"):
        core.plan_coalesce("1.2.3", (), date="2026-07-08")
    with pytest.raises(core.ChangelogError, match="refusing an empty release"):
        core.plan_coalesce("1.2.3-rc.1", (), date="2026-07-08")


def test_plan_coalesce_version_is_required_and_validated():
    frags = _frags("- a\n")
    with pytest.raises(core.ChangelogError, match="version is required"):
        core.plan_coalesce("", frags, date="2026-07-08")
    with pytest.raises(core.ChangelogError, match="without the 'v' prefix"):
        core.plan_coalesce("v1.2.3", frags, date="2026-07-08")
    with pytest.raises(core.ChangelogError, match="must be valid semver"):
        core.plan_coalesce("minor", frags, date="2026-07-08")


def test_plan_coalesce_resumes_an_already_cut_version():
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
    assert text.endswith("- x\n")
    assert not text.endswith("- x\n\n")


def test_render_preserves_significant_trailing_whitespace():
    text = core.render((), {"0.1.0": "## 0.1.0 - 2026-01-01\n\n- x  \n"}, legacy=None)
    assert text.endswith("- x  \n")


def test_render_is_deterministic():
    frags = _frags("- a\n")
    sections = {"1.0.0": "## 1.0.0 - 2026-01-01\n\n- first\n"}
    assert core.render(frags, sections) == core.render(frags, sections)


def test_sync_diff_none_when_in_sync():
    frags = _frags("- a\n")
    rendered = core.render(frags, {})
    assert core.sync_diff(rendered, rendered) is None


def test_sync_diff_surfaces_divergence():
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
