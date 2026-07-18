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
            "tree": "/trees/shipit-codex-20260711-121015-019f5115-fb40-7db2-a82f-d2fc02a1da22",
            "backend": "codex",
        },
        {
            "repo": "arthur-debert/shipit",
            "event": "session.started",
            "session": "codex-20260711-121015-73781",
            "tree": "/trees/shipit-codex-20260711-121015-019f5115-fb40-7db2-a82f-d2fc02a1da22",
            "codex_thread": "019f-thread",
        },
    )

    target = resume.resolve("codex-20260711-121015-73781", base_dir=tmp_path)

    assert target == resume.ResumeTarget(
        repo=REPO,
        backend="codex",
        shipit_session_id="codex-20260711-121015-73781",
        native_session_id="019f-thread",
        tree="/trees/shipit-codex-20260711-121015-019f5115-fb40-7db2-a82f-d2fc02a1da22",
    )


def test_resolve_fresh_codex_session_from_sessionstart_record(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "event": "session.started",
            "session": "codex-20260711-121015-73781",
            "tree": "/trees/shipit-codex-20260711-121015-019f5115-fb40-7db2-a82f-d2fc02a1da22",
            "codex_thread": "019f-fresh-thread",
            # ADR-0074: the backend is READ from session.started's `backend` stamp,
            # never reverse-engineered from the session-id prefix (the prefix table
            # is retired). A record without it is not resumable.
            "backend": "codex",
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
            "tree": "/trees/shipit-claude-20260711-140000-7f3c9d20-1a2b-4c3d-8e4f-56789abcdef0",
            "session_id": "claude-native",
            # The claude backend is likewise read from the stamp, not the `sess-` prefix.
            "backend": "claude",
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
            "backend": "claude",
        },
        {
            "repo": "arthur-debert/shipit",
            "session": "codex-2",
            "codex_thread": "new-native",
            "backend": "codex",
        },
    )

    target = resume.resolve(None, repo=REPO, last=True, base_dir=tmp_path)

    assert target.shipit_session_id == "codex-2"
    assert target.native_session_id == "new-native"


def test_resolve_native_id_fails_closed_when_ambiguous_across_repos(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "session": "sess-1",
            "session_id": "same",
            "backend": "claude",
        },
    )
    _log(
        tmp_path,
        "acme/widget",
        {
            "repo": "acme/widget",
            "session": "sess-2",
            "session_id": "same",
            "backend": "claude",
        },
    )

    with pytest.raises(resume.ResumeError, match="ambiguous"):
        resume.resolve("same", base_dir=tmp_path)


def test_resolve_native_id_can_be_scoped_by_repo(tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {
            "repo": "arthur-debert/shipit",
            "session": "sess-1",
            "session_id": "same",
            "backend": "claude",
        },
    )
    _log(
        tmp_path,
        "acme/widget",
        {
            "repo": "acme/widget",
            "session": "sess-2",
            "session_id": "same",
            "backend": "claude",
        },
    )

    target = resume.resolve("same", repo=REPO, base_dir=tmp_path)

    assert target.repo == REPO
    assert target.shipit_session_id == "sess-1"


def test_discover_repos_uses_the_platform_log_base(monkeypatch, tmp_path: Path):
    _log(
        tmp_path,
        "arthur-debert/shipit",
        {"repo": REPO.slug, "session": "sess-1", "session_id": "native"},
    )
    monkeypatch.setattr(
        resume.logsetup,
        "resolve_log_dir",
        lambda repo: tmp_path / repo.owner.login / repo.name,
    )

    assert resume._discover_repos(base_dir=None) == [REPO]


def test_resolve_merges_session_across_rotated_log_boundary(tmp_path: Path):
    active = tmp_path / REPO.owner.login / REPO.name / resume.logsetup.LOG_FILENAME
    active.parent.mkdir(parents=True)
    active.with_name(f"{active.name}.1").write_text(
        json.dumps(
            {
                "repo": REPO.slug,
                "session": "codex-rotated",
                "codex_thread": "019f-rotated",
                # Fields accumulate across a session's records, so the backend stamp
                # in the rotated file carries into the fold with the active file's tree.
                "backend": "codex",
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
        json.dumps(
            {
                "repo": REPO.slug,
                "session": "codex-a",
                "codex_thread": "a",
                "backend": "codex",
            }
        )
        + "\n"
    )
    active.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "repo": REPO.slug,
                    "session": "codex-b",
                    "codex_thread": "b",
                    "backend": "codex",
                },
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


def test_source_checkout_uses_stable_source_root_when_outside_checkout(
    monkeypatch, tmp_path: Path
):
    source_root = tmp_path / "sources"
    source = source_root / "shipit"
    (source / ".git").mkdir(parents=True)
    (source_root / "archive" / "noisy" / ".git").mkdir(parents=True)
    inspected = []

    def resolve_repo(path):
        inspected.append(path)
        assert path == str(source)
        return REPO

    monkeypatch.setattr(resume.git, "repo_root", lambda cwd=None: None)
    monkeypatch.setattr(resume, "DEFAULT_SOURCE_ROOT", source_root)
    monkeypatch.setattr(resume.identity, "resolve_repo", resolve_repo)

    assert resume.source_checkout_for_repo(REPO) == str(source)
    assert inspected == [str(source)]


def test_source_checkout_ignores_transient_trees_and_fails_fast(
    monkeypatch, tmp_path: Path
):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    tree = tmp_path / "trees" / "arthur-debert" / "shipit" / "branches" / "agent-a"
    (tree / ".git").mkdir(parents=True)
    monkeypatch.setattr(resume.git, "repo_root", lambda cwd=None: None)
    monkeypatch.setattr(resume, "DEFAULT_SOURCE_ROOT", source_root)

    with pytest.raises(resume.ResumeError, match="no stable source checkout"):
        resume.source_checkout_for_repo(REPO)
