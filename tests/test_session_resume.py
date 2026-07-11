import json
from pathlib import Path

import pytest

from shipit.identity import repo_from_slug
from shipit.session import resume

REPO = repo_from_slug("arthur-debert/shipit")


def _log(base: Path, slug: str, *records: dict) -> None:
    repo = repo_from_slug(slug)
    path = base / repo.owner.login / repo.name / "shipit.log"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def test_resolve_shipit_codex_session_to_native_thread(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "session": "codex-20260711-121015-73781",
            "tree": "/trees/shipit/ephemeral/codex-20260711-121015-73781",
            "backend": "codex",
        },
        {
            "repo": "arthur-debert/shipit",
            "event": "session.started",
            "session": "codex-20260711-121015-73781",
            "tree": "/trees/shipit/ephemeral/codex-20260711-121015-73781",
            "codex_thread": "019f-thread",
        },
    )

    target = resume.resolve("codex-20260711-121015-73781", base_dir=tmp_path)

    assert target == resume.ResumeTarget(
        repo=REPO,
        backend="codex",
        shipit_session_id="codex-20260711-121015-73781",
        native_session_id="019f-thread",
        tree="/trees/shipit/ephemeral/codex-20260711-121015-73781",
    )


def test_resolve_fresh_codex_session_from_sessionstart_record(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "event": "session.started",
            "session": "codex-20260711-121015-73781",
            "tree": "/trees/shipit/ephemeral/codex-20260711-121015-73781",
            "codex_thread": "019f-fresh-thread",
        },
    )

    target = resume.resolve("codex-20260711-121015-73781", base_dir=tmp_path)

    assert target.backend == "codex"
    assert target.native_session_id == "019f-fresh-thread"


def test_resolve_shipit_claude_session_from_sessionstart_record(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "event": "session.started",
            "session": "sess-20260711-140000-12345",
            "tree": "/trees/shipit/ephemeral/sess-20260711-140000-12345",
            "session_id": "claude-native",
        },
    )

    target = resume.resolve("sess-20260711-140000-12345", base_dir=tmp_path)

    assert target.backend == "claude"
    assert target.native_session_id == "claude-native"
    assert target.repo == REPO


def test_resolve_last_requires_repo_and_picks_latest_complete_session(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "session": "sess-1",
            "session_id": "old-native",
        },
        {
            "repo": "arthur-debert/shipit",
            "session": "codex-2",
            "codex_thread": "new-native",
        },
    )

    target = resume.resolve(None, repo=REPO, last=True, base_dir=tmp_path)

    assert target.shipit_session_id == "codex-2"
    assert target.native_session_id == "new-native"


def test_resolve_native_id_fails_closed_when_ambiguous_across_repos(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {"repo": "arthur-debert/shipit", "session": "sess-1", "session_id": "same"},
    )
    _log(
        tmp_path,
        "acme/widget",
        {"repo": "acme/widget", "session": "sess-2", "session_id": "same"},
    )

    with pytest.raises(resume.ResumeError, match="ambiguous"):
        resume.resolve("same", base_dir=tmp_path)


def test_resolve_native_id_can_be_scoped_by_repo(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {"repo": "arthur-debert/shipit", "session": "sess-1", "session_id": "same"},
    )
    _log(
        tmp_path,
        "acme/widget",
        {"repo": "acme/widget", "session": "sess-2", "session_id": "same"},
    )

    target = resume.resolve("same", repo=REPO, base_dir=tmp_path)

    assert target.repo == REPO
    assert target.shipit_session_id == "sess-1"


def test_resolve_merges_session_across_rotated_log_boundary(tmp_path: Path):
    active = tmp_path / REPO.owner.login / REPO.name / resume.logsetup.LOG_FILENAME
    active.parent.mkdir(parents=True)
    active.with_name(f"{active.name}.1").write_text(
        json.dumps(
            {
                "repo": REPO.slug,
                "session": "codex-rotated",
                "codex_thread": "019f-rotated",
            }
        )
        + "\n"
    )
    active.write_text(
        json.dumps(
            {
                "repo": REPO.slug,
                "session": "codex-rotated",
                "tree": "/fresh/tree",
            }
        )
        + "\n"
    )

    target = resume.resolve("codex-rotated", repo=REPO, base_dir=tmp_path)

    assert target.native_session_id == "019f-rotated"
    assert target.tree == "/fresh/tree"


def test_resolve_last_uses_the_session_with_the_newest_record(tmp_path: Path):
    active = tmp_path / REPO.owner.login / REPO.name / resume.logsetup.LOG_FILENAME
    active.parent.mkdir(parents=True)
    active.with_name(f"{active.name}.1").write_text(
        json.dumps({"repo": REPO.slug, "session": "codex-a", "codex_thread": "a"})
        + "\n"
    )
    active.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"repo": REPO.slug, "session": "codex-b", "codex_thread": "b"},
                {"repo": REPO.slug, "session": "codex-a", "tree": "/newest"},
            )
        )
        + "\n"
    )

    target = resume.resolve(None, repo=REPO, last=True, base_dir=tmp_path)

    assert target.shipit_session_id == "codex-a"
    assert target.tree == "/newest"


def test_source_checkout_prefers_matching_ambient_checkout(monkeypatch, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(resume.git, "repo_root", lambda cwd=None: str(source))
    monkeypatch.setattr(resume.identity, "resolve_repo", lambda path: REPO)

    assert resume.source_checkout_for_repo(REPO) == str(source)


def test_source_checkout_scans_central_tree_root_when_outside_checkout(
    monkeypatch, tmp_path: Path
):
    tree = tmp_path / "trees" / "arthur-debert" / "shipit" / "ephemeral" / "sess-1"
    (tree / ".git").mkdir(parents=True)
    monkeypatch.setattr(resume.git, "repo_root", lambda cwd=None: None)
    monkeypatch.setattr(resume.layout, "central_root", lambda: tmp_path / "trees")
    monkeypatch.setattr(resume.identity, "resolve_repo", lambda path: REPO)

    assert resume.source_checkout_for_repo(REPO) == str(tree)
