"""Tests for the `shipit changelog` verb shell (TOL01-WS06 #554).

The verb's effectful layer over real temp trees: root resolution (the git read
injected at the seam, ADR-0028 — no subprocess runs here), CHANGELOG/ reads,
the projection/section/notes writes, and the uniform exit/reporting contract
(story 8: 0 ok, 1 refusal-or-failing-check via one `error: …` line or the
check report + diff).
"""

import os
from pathlib import Path

import pytest

from shipit import changelog as core
from shipit import cli, config
from shipit.verbs import changelog as verb


# A repo_root seam standing in for the git adapter (no test execs git): the
# not-a-checkout answer, for trees that carry their own CHANGELOG/.
def _no_git(**kwargs):
    return None


def _tree(tmp_path: Path, fragments: dict[str, str] | None = None) -> Path:
    (tmp_path / "CHANGELOG").mkdir()
    for name, body in (fragments or {}).items():
        (tmp_path / "CHANGELOG" / name).write_text(body, encoding="utf-8")
    return tmp_path


def _render_into(root: Path) -> None:
    assert verb.run_render(str(root), repo_root=_no_git) == 0


# --------------------------------------------------------------------------
# check — the fragment-sync verdict (the changelog-sync lane's run)
# --------------------------------------------------------------------------


def test_check_passes_a_synced_tree(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-fix.md": "- fixed a thing\n"})
    _render_into(root)
    capsys.readouterr()
    assert verb.run_check(str(root), repo_root=_no_git) == 0
    out = capsys.readouterr().out
    assert "changelog: OK" in out


def test_check_fails_fragment_added_without_render(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-fix.md": "- fixed a thing\n"})
    _render_into(root)
    # A later PR adds a fragment but forgets to re-render.
    (root / "CHANGELOG" / "unreleased-more.md").write_text("- more\n")
    capsys.readouterr()
    assert verb.run_check(str(root), repo_root=_no_git) == 1
    out = capsys.readouterr().out
    assert "changelog: FAILED" in out
    assert "+- more" in out  # the diff is surfaced
    assert "shipit changelog render" in out  # with the remediation


def test_check_fails_changelog_edited_without_fragment(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-fix.md": "- fixed a thing\n"})
    _render_into(root)
    committed = (root / "CHANGELOG.md").read_text()
    (root / "CHANGELOG.md").write_text(
        committed.replace("- fixed a thing", "- fixed a thing (hand edit)")
    )
    capsys.readouterr()
    assert verb.run_check(str(root), repo_root=_no_git) == 1
    out = capsys.readouterr().out
    assert "-- fixed a thing (hand edit)" in out


def test_check_missing_changelog_dir_is_a_refusal(tmp_path, capsys):
    # Fragments are the DECLARED model: no CHANGELOG/ is a hard error naming
    # the adoption step, never the legacy skip-when-missing nicety.
    assert verb.run_check(str(tmp_path), repo_root=_no_git) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: no CHANGELOG/ directory")


def test_check_invalid_version_filename_is_a_refusal(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-fix.md": "- x\n"})
    (root / "CHANGELOG" / "v1.2.3.md").write_text("## v1.2.3\n")
    assert verb.run_check(str(root), repo_root=_no_git) == 1
    err = capsys.readouterr().err
    assert "unparseable version filename" in err
    assert "v1.2.3.md" in err


def test_root_resolution_walks_up_and_falls_back_to_git(tmp_path, capsys):
    # From a subdirectory the ancestor walk finds the CHANGELOG/ root without
    # consulting git…
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _tree(repo, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)

    def _boom(**kwargs):
        raise AssertionError("git must not be consulted when CHANGELOG/ is found")

    capsys.readouterr()
    assert verb.run_check(str(sub), repo_root=_boom) == 0
    # …and with no CHANGELOG/ anywhere the git seam supplies the root, so the
    # refusal names the repo root, not the cwd (the one git read, injected).
    bare = tmp_path / "bare" / "inner"
    bare.mkdir(parents=True)
    calls: list[str] = []

    def _repo_root(*, cwd: str) -> str:
        calls.append(cwd)
        return str(tmp_path / "bare")

    capsys.readouterr()
    assert verb.run_check(str(bare), repo_root=_repo_root) == 1
    assert calls == [str(bare)]
    assert str(tmp_path / "bare") in capsys.readouterr().err


# --------------------------------------------------------------------------
# render — the projection write
# --------------------------------------------------------------------------


def test_render_writes_the_projection(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-b.md": "- b\n", "unreleased-a.md": "- a\n"})
    assert verb.run_render(str(root), repo_root=_no_git) == 0
    text = (root / "CHANGELOG.md").read_text()
    assert text.startswith(core.RENDER_PREAMBLE)
    # Byte-order fragment order: a before b.
    assert text.index("- a") < text.index("- b")
    assert "rendered CHANGELOG.md" in capsys.readouterr().out


def test_render_includes_versions_and_legacy_tail(tmp_path):
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    (root / "CHANGELOG" / "1.0.0.md").write_text("## 1.0.0 - 2026-01-01\n\n- old\n")
    (root / "CHANGELOG" / "legacy.md").write_text("# Ancient history\n")
    assert verb.run_render(str(root), repo_root=_no_git) == 0
    text = (root / "CHANGELOG.md").read_text()
    assert "## 1.0.0 - 2026-01-01" in text
    assert text.endswith("# Ancient history\n")


def test_render_unwritable_target_is_a_clean_error(tmp_path, capsys):
    # A write failure (here: CHANGELOG.md is a directory) maps to the uniform
    # `error: …` surface / exit 1, not a raw OSError traceback (ADR-0030).
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    (root / "CHANGELOG.md").mkdir()
    assert verb.run_render(str(root), repo_root=_no_git) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot write CHANGELOG.md")


# --------------------------------------------------------------------------
# coalesce — the cut-time face
# --------------------------------------------------------------------------


def _today() -> str:
    return "2026-07-08"


def test_coalesce_mutation_oserror_is_a_clean_error(tmp_path, capsys):
    # An OSError inside the cut's mutation block (here: the re-render target
    # CHANGELOG.md is a directory) maps to `error: …` / exit 1, not a traceback.
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    (root / "CHANGELOG.md").mkdir()
    capsys.readouterr()
    assert verb.run_coalesce("1.2.3", str(root), repo_root=_no_git, today=_today) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot cut 1.2.3")


def test_coalesce_notes_out_parent_needs_execute_permission(tmp_path, capsys):
    # Creating a file in a directory needs write AND execute (search) on the
    # dir; a parent with write but no execute is refused BEFORE mutation, not
    # after a later write failure once the cut has landed.
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    before = (root / "CHANGELOG.md").read_text()
    nox = tmp_path / "nox"
    nox.mkdir()
    os.chmod(nox, 0o600)  # rw-, no execute → cannot create files inside
    capsys.readouterr()
    try:
        assert (
            verb.run_coalesce(
                "1.2.3",
                str(root),
                notes_out=str(nox / "notes.md"),
                repo_root=_no_git,
                today=_today,
            )
            == 1
        )
        assert "error" in capsys.readouterr().err.lower()
        # Untouched: the refusal came before the cut.
        assert (root / "CHANGELOG" / "unreleased-a.md").exists()
        assert (root / "CHANGELOG.md").read_text() == before
    finally:
        os.chmod(nox, 0o700)  # restore so pytest can clean up the tmp tree


def test_coalesce_failed_cut_leaves_no_stray_notes_file(tmp_path, capsys):
    # The writability preflight must NOT pre-create --notes-out: a cut that
    # fails after it leaves no empty notes artifact (automation keys off the
    # file's existence). Here the re-render write fails (CHANGELOG.md is a dir).
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    (root / "CHANGELOG.md").mkdir()
    notes_file = tmp_path / "notes.md"
    capsys.readouterr()
    assert (
        verb.run_coalesce(
            "1.2.3",
            str(root),
            notes_out=str(notes_file),
            repo_root=_no_git,
            today=_today,
        )
        == 1
    )
    assert not notes_file.exists()


def test_coalesce_final_rolls_consumes_and_rerenders(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n", "unreleased-b.md": "- b\n"})
    _render_into(root)
    capsys.readouterr()
    notes_file = tmp_path / "notes.md"
    assert (
        verb.run_coalesce(
            "1.2.3",
            str(root),
            notes_out=str(notes_file),
            repo_root=_no_git,
            today=_today,
        )
        == 0
    )
    # The section was written, the fragments consumed…
    section = (root / "CHANGELOG" / "1.2.3.md").read_text()
    assert section == "## 1.2.3 - 2026-07-08\n\n- a\n- b\n"
    assert not (root / "CHANGELOG" / "unreleased-a.md").exists()
    assert not (root / "CHANGELOG" / "unreleased-b.md").exists()
    # …the ONE notes text is the section body byte-for-byte (story 26)…
    assert notes_file.read_text() == "- a\n- b\n"
    # …and the projection moved in the same step: the sync check stays green.
    capsys.readouterr()
    assert verb.run_check(str(root), repo_root=_no_git) == 0


def test_coalesce_bad_notes_out_refuses_without_mutating(tmp_path, capsys):
    # A --notes-out that cannot be written (here: a directory) is a refusal
    # BEFORE the tree is touched — the cut must never land with no notes.
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    before = (root / "CHANGELOG.md").read_text()
    capsys.readouterr()
    notes_dir = tmp_path / "notes-as-dir"
    notes_dir.mkdir()
    assert (
        verb.run_coalesce(
            "1.2.3",
            str(root),
            notes_out=str(notes_dir),
            repo_root=_no_git,
            today=_today,
        )
        == 1
    )
    assert "error" in capsys.readouterr().err.lower()
    # Nothing mutated: fragment kept, no section written, projection untouched.
    assert (root / "CHANGELOG" / "unreleased-a.md").exists()
    assert not (root / "CHANGELOG" / "1.2.3.md").exists()
    assert (root / "CHANGELOG.md").read_text() == before


def test_coalesce_notes_out_creates_missing_parent_dirs(tmp_path, capsys):
    # A --notes-out under a not-yet-existing directory is created, not a failure.
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    capsys.readouterr()
    notes_file = tmp_path / "nested" / "dir" / "notes.md"
    assert (
        verb.run_coalesce(
            "1.2.3",
            str(root),
            notes_out=str(notes_file),
            repo_root=_no_git,
            today=_today,
        )
        == 0
    )
    assert notes_file.read_text() == "- a\n"


def test_coalesce_unwritable_notes_parent_refuses_without_mutating(tmp_path, capsys):
    # The writability preflight also catches an unusable parent (here: a file
    # where a directory is needed) BEFORE the cut lands — no partial mutation.
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    before = (root / "CHANGELOG.md").read_text()
    (tmp_path / "afile").write_text("not a dir\n")
    capsys.readouterr()
    notes_out = tmp_path / "afile" / "notes.md"
    assert (
        verb.run_coalesce(
            "1.2.3",
            str(root),
            notes_out=str(notes_out),
            repo_root=_no_git,
            today=_today,
        )
        == 1
    )
    assert "error" in capsys.readouterr().err.lower()
    assert (root / "CHANGELOG" / "unreleased-a.md").exists()
    assert not (root / "CHANGELOG" / "1.2.3.md").exists()
    assert (root / "CHANGELOG.md").read_text() == before


def test_coalesce_prerelease_extracts_and_keeps_fragments(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    before = (root / "CHANGELOG.md").read_text()
    capsys.readouterr()
    assert (
        verb.run_coalesce("1.2.3-rc.1", str(root), repo_root=_no_git, today=_today) == 0
    )
    captured = capsys.readouterr()
    # Without --notes-out the notes ARE stdout (pipe-able); the report is stderr.
    assert captured.out == "- a\n"
    assert "prerelease 1.2.3-rc.1" in captured.err
    # Nothing mutated: fragments kept for the final, projection untouched.
    assert (root / "CHANGELOG" / "unreleased-a.md").exists()
    assert not (root / "CHANGELOG" / "1.2.3-rc.1.md").exists()
    assert (root / "CHANGELOG.md").read_text() == before


def test_coalesce_empty_release_refused(tmp_path, capsys):
    root = _tree(tmp_path)
    _render_into(root)
    capsys.readouterr()
    assert verb.run_coalesce("1.2.3", str(root), repo_root=_no_git) == 1
    assert "refusing an empty release" in capsys.readouterr().err


def test_coalesce_requires_a_valid_supplied_version(tmp_path, capsys):
    # ADR-0041 at the verb surface: a bump word or v-prefix is refused; the
    # version is never inferred here.
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    assert verb.run_coalesce("minor", str(root), repo_root=_no_git) == 1
    assert "must be valid semver" in capsys.readouterr().err
    assert verb.run_coalesce("v1.2.3", str(root), repo_root=_no_git) == 1
    assert "without the 'v' prefix" in capsys.readouterr().err


def test_coalesce_resume_reemits_identical_notes(tmp_path, capsys):
    # ADR-0009: after a cut (tag exists), re-running coalesce re-emits the SAME
    # notes text from the committed section — no fragments needed, no mutation.
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    first = tmp_path / "first.md"
    verb.run_coalesce(
        "1.2.3", str(root), notes_out=str(first), repo_root=_no_git, today=_today
    )
    again = tmp_path / "again.md"
    capsys.readouterr()
    assert (
        verb.run_coalesce(
            "1.2.3", str(root), notes_out=str(again), repo_root=_no_git, today=_today
        )
        == 0
    )
    assert again.read_text() == first.read_text()
    assert "already cut" in capsys.readouterr().out


def test_coalesce_refuses_new_fragments_over_a_cut_section(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    verb.run_coalesce("1.2.3", str(root), repo_root=_no_git, today=_today)
    (root / "CHANGELOG" / "unreleased-late.md").write_text("- late\n")
    capsys.readouterr()
    assert verb.run_coalesce("1.2.3", str(root), repo_root=_no_git) == 1
    assert "refusing to overwrite" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The lane and the CLI wiring — one definition, laptop and CI identical
# --------------------------------------------------------------------------


def test_changelog_sync_lane_runs_this_verb():
    # The declared Lane's `run` is the exact `shipit changelog check`
    # invocation (story 18): what WS05's planner routes in CI is what a laptop
    # runs — one definition. The scaffold constant and a consumer's own
    # `[lanes]` declaration parse to the same typed Lane.
    lane = config.CHANGELOG_SYNC_LANE
    assert lane.run == "changelog check"
    assert lane.trigger == "pr"
    assert lane.required is True
    tool, subcommand = lane.run.split()
    assert tool == "changelog"
    assert subcommand in {c for c in verb.changelog.commands}


def test_cli_wires_the_changelog_group(capsys):
    assert cli.main(["changelog", "--help"]) == 0
    out = capsys.readouterr().out
    assert "check" in out and "render" in out and "coalesce" in out


def test_cli_check_end_to_end(tmp_path, capsys, monkeypatch):
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    monkeypatch.chdir(root)
    capsys.readouterr()
    assert cli.main(["changelog", "check"]) == 0
    assert "changelog: OK" in capsys.readouterr().out
