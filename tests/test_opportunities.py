from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from shipit import config, execrun
from shipit.identity import Owner, Repo, Revision, WorkingDir
from shipit.opportunities import (
    OpportunityCapture,
    OpportunityError,
    OpportunityStoreConfig,
    allocate_inbox_path,
    load_store_config,
    render_opportunity,
    write_to_store,
)
from shipit.verbs import opportunities as opportunities_verb


def _capture(**overrides) -> OpportunityCapture:
    values = {
        "repo": "acme/widget",
        "source": "implementer",
        "tags": ("tests", "cleanup"),
        "observation": "Tests rely on global state",
        "evidence": "tests/test_widget.py mutates os.environ without cleanup",
        "suggested_next_step": "Isolate env mutation behind a fixture",
        "created_at": datetime(2026, 7, 11, 12, 34, 56, tzinfo=UTC),
    }
    values.update(overrides)
    return OpportunityCapture(**values)


def test_render_valid_capture_has_v1_front_matter_and_body_sections():
    rendered = render_opportunity(_capture())
    _, raw_header, body = rendered.split("---", 2)
    header = yaml.safe_load(raw_header)
    assert header == {
        "schema_version": 1,
        "repo": "acme/widget",
        "source": "implementer",
        "tags": ["tests", "cleanup"],
        "status": "inbox",
        "created_at": "2026-07-11T12:34:56Z",
    }
    assert "## Observation\n\nTests rely on global state" in body
    assert "## Evidence\n\ntests/test_widget.py mutates os.environ" in body
    assert "## Suggested next step\n\nIsolate env mutation" in body


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("repo", "", "repo"),
        ("source", "", "source"),
        ("tags", (), "tags"),
        ("tags", ("tests", ""), "tags"),
        ("observation", "", "observation"),
        ("evidence", "", "evidence"),
        ("suggested_next_step", "", "suggested_next_step"),
        ("created_at", datetime(2026, 7, 11, 12, 34, 56), "created_at timezone"),
    ],
)
def test_capture_rejects_missing_required_metadata(field, value, expected):
    with pytest.raises(OpportunityError, match=expected):
        render_opportunity(_capture(**{field: value}))


def test_load_store_config_reads_project_opportunities_repo():
    cfg = {"project": {"opportunities": {"repo": "Arthur-Debert/Opps"}}}
    assert load_store_config(cfg) == OpportunityStoreConfig(repo="arthur-debert/opps")


@pytest.mark.parametrize(
    ("cfg", "message"),
    [
        ({}, r"missing \[project\.opportunities\]"),
        (
            {"project": {"opportunities": "x"}},
            r"\[project\.opportunities\] must be a table",
        ),
        (
            {"project": {"opportunities": {}}},
            r"\[project\.opportunities\]\.repo must be",
        ),
        (
            {"project": {"opportunities": {"repo": "not-a-slug"}}},
            r'must look like "owner/name"',
        ),
        (
            {"project": {"opportunities": {"repo": "bad owner/repo"}}},
            r'must look like "owner/name"',
        ),
        (
            {"project": {"opportunities": {"repo": "owner/re po"}}},
            r'must look like "owner/name"',
        ),
        (
            {"project": {"opportunities": {"repo": "owner/name?x"}}},
            r'must look like "owner/name"',
        ),
    ],
)
def test_load_store_config_reports_missing_or_malformed_config(cfg, message):
    with pytest.raises(config.ConfigError, match=message):
        load_store_config(cfg)


def test_allocate_inbox_path_skips_existing_collision(tmp_path):
    capture = _capture(observation="Tighten flaky test!")
    first = tmp_path / "inbox" / "20260711T123456Z-tighten-flaky-test-aaaaaaaaaaaa.md"
    first.parent.mkdir()
    first.write_text("taken", encoding="utf-8")
    tokens = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    allocated = allocate_inbox_path(
        tmp_path, capture, token_factory=lambda: next(tokens)
    )
    assert allocated.relative_to(tmp_path).as_posix() == (
        "inbox/20260711T123456Z-tighten-flaky-test-bbbbbbbbbbbb.md"
    )


class _FakeStoreGit:
    def __init__(
        self,
        *,
        push_failures: int = 0,
        push_failure_stderr: str = "non-fast-forward",
    ) -> None:
        self.calls: list[tuple] = []
        self.push_failures = push_failures
        self.push_failure_stderr = push_failure_stderr
        self.captured_text = ""

    def clone(self, url: str, dest: str) -> None:
        self.calls.append(("clone", url, dest))
        Path(dest).mkdir(parents=True)

    def configure_identity(self, name: str, email: str, *, cwd: str) -> None:
        self.calls.append(("configure_identity", name, email))

    def add(self, paths: list[str], *, cwd: str) -> None:
        self.calls.append(("add", tuple(paths)))
        self.captured_text = (Path(cwd) / paths[0]).read_text(encoding="utf-8")

    def commit(self, message: str, paths: list[str], *, cwd: str) -> None:
        self.calls.append(("commit", message, tuple(paths)))

    def pull_rebase(self, branch: str, *, cwd: str, remote: str = "origin") -> None:
        self.calls.append(("pull_rebase", branch, remote))

    def push(self, branch: str, *, cwd: str, remote: str = "origin") -> None:
        self.calls.append(("push", branch, remote))
        if self.push_failures:
            self.push_failures -= 1
            raise execrun.ExecError(
                ["git", "push"],
                rc=1,
                stderr=self.push_failure_stderr,
                duration_ms=1,
            )

    def current_branch(self, *, cwd: str) -> str | None:
        self.calls.append(("current_branch",))
        return "main"


def test_write_to_store_commits_valid_inbox_opportunity_without_github():
    fake = _FakeStoreGit()
    result = write_to_store(
        OpportunityStoreConfig("acme/opportunity-store"),
        _capture(),
        boundary=fake,
        token_factory=lambda: "abc123abc123",
    )
    assert (
        result.path
        == "inbox/20260711T123456Z-tests-rely-on-global-state-abc123abc123.md"
    )
    assert result.commit_message == f"Capture Opportunity: {result.path}"
    assert fake.calls[0][0:2] == (
        "clone",
        "https://github.com/acme/opportunity-store.git",
    )
    assert ("commit", result.commit_message, (result.path,)) in fake.calls
    assert ("push", "main", "origin") in fake.calls
    assert "status: inbox" in fake.captured_text
    # A throwaway clone commits on a runner that may carry no global git
    # identity: the store writer must state a local one before committing.
    op_names = [call[0] for call in fake.calls]
    assert op_names.index("configure_identity") < op_names.index("commit")


def test_write_to_store_rebases_and_retries_failed_push():
    fake = _FakeStoreGit(push_failures=1)
    write_to_store(
        OpportunityStoreConfig("acme/opportunity-store"),
        _capture(),
        boundary=fake,
        token_factory=lambda: "abc123abc123",
    )
    assert [call[0] for call in fake.calls].count("push") == 2
    assert ("pull_rebase", "main", "origin") in fake.calls


def test_write_to_store_does_not_retry_non_race_push_failure():
    fake = _FakeStoreGit(
        push_failures=1, push_failure_stderr="fatal: Authentication failed"
    )
    with pytest.raises(OpportunityError, match=r"after 1 push attempt\(s\)"):
        write_to_store(
            OpportunityStoreConfig("acme/opportunity-store"),
            _capture(),
            boundary=fake,
            token_factory=lambda: "abc123abc123",
        )

    assert [call[0] for call in fake.calls].count("push") == 1
    assert not any(call[0] == "pull_rebase" for call in fake.calls)


def test_write_to_store_reports_store_write_failure():
    fake = _FakeStoreGit(push_failures=2)
    with pytest.raises(OpportunityError, match=r"after 2 push attempt\(s\)"):
        write_to_store(
            OpportunityStoreConfig("acme/opportunity-store"),
            _capture(),
            boundary=fake,
            token_factory=lambda: "abc123abc123",
        )


@dataclass(frozen=True)
class _FakeContext:
    working_dir: WorkingDir

    def require_working_dir(self) -> WorkingDir:
        return self.working_dir


def test_create_reports_missing_store_config(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / ".shipit.toml"
    cfg.write_text("[project]\n", encoding="utf-8")
    wd = WorkingDir(
        path=str(tmp_path),
        repo=Repo(owner=Owner("acme"), name="widget"),
        revision=Revision(branch="main"),
    )
    monkeypatch.setattr(
        opportunities_verb,
        "current_root_context",
        lambda: _FakeContext(working_dir=wd),
    )
    rc = opportunities_verb.run_create(
        config_path=str(cfg),
        source="implementer",
        tags=("tests",),
        observation="Observation",
        evidence="Evidence",
        next_step="Next step",
    )
    assert rc == 1
    assert "missing [project.opportunities]" in capsys.readouterr().err
