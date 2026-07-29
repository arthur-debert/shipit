import os
from pathlib import Path

import pytest

from shipit import changelog as core
from shipit import cli, config
from shipit.verbs import changelog as verb


def _no_git(**kwargs):
    return None


def _tree(tmp_path: Path, fragments: dict[str, str] | None = None) -> Path:
    (tmp_path / "CHANGELOG").mkdir()
    for name, body in (fragments or {}).items():
        (tmp_path / "CHANGELOG" / name).write_text(body, encoding="utf-8")
    return tmp_path


def _render_into(root: Path) -> None:
    assert verb.run_render(str(root), repo_root=_no_git) == 0


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
    (root / "CHANGELOG" / "unreleased-more.md").write_text("- more\n")
    capsys.readouterr()
    assert verb.run_check(str(root), repo_root=_no_git) == 1
    out = capsys.readouterr().out
    assert "changelog: FAILED" in out
    assert "+- more" in out
    assert "shipit changelog render" in out


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


def test_render_writes_the_projection(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-b.md": "- b\n", "unreleased-a.md": "- a\n"})
    assert verb.run_render(str(root), repo_root=_no_git) == 0
    text = (root / "CHANGELOG.md").read_text()
    assert text.startswith(core.RENDER_PREAMBLE)
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
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    (root / "CHANGELOG.md").mkdir()
    assert verb.run_render(str(root), repo_root=_no_git) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot write CHANGELOG.md")


def _today() -> str:
    return "2026-07-08"


def test_coalesce_mutation_oserror_is_a_clean_error(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    (root / "CHANGELOG.md").mkdir()
    capsys.readouterr()
    assert verb.run_coalesce("1.2.3", str(root), repo_root=_no_git, today=_today) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot cut 1.2.3")


def test_coalesce_notes_out_parent_needs_execute_permission(tmp_path, capsys):
    if os.name != "posix":
        pytest.skip("directory permission bits are POSIX-only")
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    _render_into(root)
    before = (root / "CHANGELOG.md").read_text()
    nox = tmp_path / "nox"
    nox.mkdir()
    os.chmod(nox, 0o600)
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
        assert (root / "CHANGELOG" / "unreleased-a.md").exists()
        assert (root / "CHANGELOG.md").read_text() == before
    finally:
        os.chmod(nox, 0o700)


def test_coalesce_failed_cut_leaves_no_stray_notes_file(tmp_path, capsys):
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
    section = (root / "CHANGELOG" / "1.2.3.md").read_text()
    assert section == "## 1.2.3 - 2026-07-08\n\n- a\n- b\n"
    assert not (root / "CHANGELOG" / "unreleased-a.md").exists()
    assert not (root / "CHANGELOG" / "unreleased-b.md").exists()
    assert notes_file.read_text() == "- a\n- b\n"
    capsys.readouterr()
    assert verb.run_check(str(root), repo_root=_no_git) == 0


def test_coalesce_bad_notes_out_refuses_without_mutating(tmp_path, capsys):
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
    assert (root / "CHANGELOG" / "unreleased-a.md").exists()
    assert not (root / "CHANGELOG" / "1.2.3.md").exists()
    assert (root / "CHANGELOG.md").read_text() == before


def test_coalesce_notes_out_creates_missing_parent_dirs(tmp_path, capsys):
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
    assert captured.out == "- a\n"
    assert "prerelease 1.2.3-rc.1" in captured.err
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
    root = _tree(tmp_path, {"unreleased-a.md": "- a\n"})
    assert verb.run_coalesce("minor", str(root), repo_root=_no_git) == 1
    assert "must be valid semver" in capsys.readouterr().err
    assert verb.run_coalesce("v1.2.3", str(root), repo_root=_no_git) == 1
    assert "without the 'v' prefix" in capsys.readouterr().err


def test_coalesce_resume_reemits_identical_notes(tmp_path, capsys):
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


def test_changelog_sync_lane_runs_this_verb():
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


def test_render_current_is_none_without_the_fragment_model(tmp_path):
    assert verb.render_current(tmp_path) is None


def test_render_current_is_none_on_unparseable_version_names(tmp_path):
    root = _tree(tmp_path, {"unreleased-x.md": "- x\n"})
    (root / "CHANGELOG" / "not-semver.md").write_text("bad\n", encoding="utf-8")
    assert verb.render_current(root) is None


def test_render_current_matches_the_verb_render(tmp_path):
    root = _tree(tmp_path, {"unreleased-x.md": "- Added the thing\n"})
    rendered = verb.render_current(root)
    _render_into(root)
    assert rendered == (root / core.CHANGELOG_FILE).read_text(encoding="utf-8")
    assert core.sync_diff(rendered, rendered) is None


def _boom_presence() -> bool:
    raise AssertionError("CHANGELOG/ must not be read on an exempt base")


def test_fragment_gate_passes_off_a_pr():
    verdict = verb.decide_fragment_gate(
        base_ref="", has_unreleased_fragment=_boom_presence
    )
    assert verdict.ok
    assert "not a PR" in verdict.message


def test_fragment_gate_passes_on_a_non_main_base():
    verdict = verb.decide_fragment_gate(
        base_ref="ADP02", has_unreleased_fragment=_boom_presence
    )
    assert verdict.ok
    assert "not 'main'" in verdict.message


def test_fragment_gate_presence_thunk_is_lazy_only_read_when_gated():
    calls: list[str] = []

    def _presence() -> bool:
        calls.append("read")
        return True

    verb.decide_fragment_gate(base_ref="", has_unreleased_fragment=_presence)
    verb.decide_fragment_gate(base_ref="ADP02", has_unreleased_fragment=_presence)
    assert calls == []
    verb.decide_fragment_gate(base_ref="main", has_unreleased_fragment=_presence)
    assert calls == ["read"]


def test_fragment_gate_passes_when_a_fragment_is_present():
    verdict = verb.decide_fragment_gate(
        base_ref="main", has_unreleased_fragment=lambda: True
    )
    assert verdict.ok
    assert "OK" in verdict.message


def test_fragment_gate_fails_on_main_with_no_fragment():
    verdict = verb.decide_fragment_gate(
        base_ref="main", has_unreleased_fragment=lambda: False
    )
    assert not verdict.ok
    assert "no CHANGELOG/unreleased-*.md fragment present" in verdict.message
    assert "empty cut" in verdict.message


def test_run_check_fragment_passes_when_changelog_has_a_fragment(tmp_path, capsys):
    root = _tree(tmp_path, {"unreleased-x.md": "- a change\n"})
    rc = verb.run_check_fragment(str(root), base_ref="main")
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_run_check_fragment_fails_on_main_with_an_empty_changelog(tmp_path, capsys):
    root = _tree(tmp_path)
    rc = verb.run_check_fragment(str(root), base_ref="main")
    assert rc == 1
    assert "no CHANGELOG/unreleased-*.md fragment present" in capsys.readouterr().out


def test_run_check_fragment_default_seam_uses_classify_dir_discovery(tmp_path, capsys):
    root = _tree(tmp_path, {"1.0.0.md": "## 1.0.0\n"})
    rc = verb.run_check_fragment(str(root), base_ref="main")
    assert rc == 1
    assert "no CHANGELOG/unreleased-*.md fragment present" in capsys.readouterr().out


def test_run_check_fragment_passes_off_a_pr(tmp_path, capsys):
    root = _tree(tmp_path)
    rc = verb.run_check_fragment(str(root), base_ref="")
    assert rc == 0
    assert "not a PR" in capsys.readouterr().out


def test_run_check_fragment_reads_base_ref_from_env(tmp_path, capsys, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    assert verb.run_check_fragment(str(root)) == 1
    capsys.readouterr()
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    assert verb.run_check_fragment(str(root)) == 0


def test_run_check_fragment_read_tree_seam_is_injectable(tmp_path, capsys):
    stub = verb.ChangelogTree(
        root=tmp_path,
        has_dir=True,
        fragments=(core.Fragment(name="unreleased-x.md", body="- x\n"),),
        sections={},
        legacy=None,
        committed=None,
        invalid=(),
    )
    root = _tree(tmp_path)
    rc = verb.run_check_fragment(str(root), base_ref="main", read_tree=lambda _r: stub)
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_run_check_fragment_does_not_read_the_tree_on_exempt_bases(tmp_path, capsys):
    def _boom(_root):
        raise AssertionError("read_tree must not be invoked on an exempt base")

    root = _tree(tmp_path)
    assert verb.run_check_fragment(str(root), base_ref="", read_tree=_boom) == 0
    assert "not a PR" in capsys.readouterr().out
    assert verb.run_check_fragment(str(root), base_ref="epic", read_tree=_boom) == 0
    assert "not 'main'" in capsys.readouterr().out
