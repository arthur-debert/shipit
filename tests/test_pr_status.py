from __future__ import annotations

import json

import pytest

from shipit import cli
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.roster import Roster
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import status as status_verb

REPO = repo_from_slug("owner/repo")

EXPECTED_JSON_FIELDS = {
    "pr",
    "state",
    "next_action",
    "reviewers",
    "open_threads",
    "checks",
    "mergeable",
    "cycles",
    "breaker",
    "reviewer_funnel",
    "degraded",
    "to_request",
}


def _fake_status(pr: int) -> TaskStatus:
    return TaskStatus(
        state=TaskState.READY,
        next_action="run `pr ready` to flip draft->ready",
        pr=pr,
        reviewers={"copilot": "done_clean"},
        open_threads=0,
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
        cycles=1,
        breaker=None,
    )


@pytest.fixture
def patched(monkeypatch):

    def resolve(pr, repo, branch):
        assert repo is not None
        return PrId(repo=repo, number=pr if pr is not None else 42)

    monkeypatch.setattr(status_verb, "resolve_pr", resolve)
    monkeypatch.setattr(status_verb, "gather", lambda target, roster, **kw: target)
    monkeypatch.setattr(status_verb, "load_roster", lambda: Roster())
    monkeypatch.setattr(status_verb, "evaluate", lambda ctx: _fake_status(ctx.number))


def test_pr_group_registered(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "pr" in capsys.readouterr().out


def test_pr_help_lists_status(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "status" in capsys.readouterr().out


def test_status_help(capsys):
    rc = cli.main(["pr", "status", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--json" in out
    assert "next action" in out.lower()


def test_status_json_emits_exact_field_set(patched, capsys):
    rc = cli.main(["pr", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == EXPECTED_JSON_FIELDS
    assert payload["state"] == "ready"
    assert payload["pr"] == 42


def test_status_text_renders_state_and_next_action(patched, capsys):
    rc = cli.main(["pr", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ready" in out
    assert "run `pr ready`" in out


def test_status_gathers_without_observational_flow_events(monkeypatch, capsys):
    monkeypatch.setattr(
        status_verb,
        "resolve_pr",
        lambda pr, repo, branch: PrId(repo=repo, number=42),
    )
    captured: dict = {}

    def gather(target, roster, **kw):
        captured.update(kw)
        return target

    monkeypatch.setattr(status_verb, "gather", gather)
    monkeypatch.setattr(status_verb, "load_roster", lambda: Roster())
    monkeypatch.setattr(status_verb, "evaluate", lambda ctx: _fake_status(ctx.number))

    rc = cli.main(["pr", "status"])

    assert rc == 0
    assert captured == {"emit_events": False}
    assert "ready" in capsys.readouterr().out


def test_format_status_annotates_degraded_on_the_state_line():
    status = TaskStatus(
        state=TaskState.READY,
        next_action="run `pr ready`",
        pr=42,
        reviewers={"copilot": "done_clean", "codex": "not_requested"},
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
        degraded={"codex-local": "failed"},
    )
    out = status_verb.format_status(status)
    assert "ready (degraded: codex-local failed)" in out
    assert "degraded:   codex-local failed" in out


def test_format_status_renders_a_cancelled_checks_verdict():
    status = TaskStatus(
        state=TaskState.BLOCKED,
        next_action="CI run cancelled/superseded — rerun (`gh run rerun ...`)",
        pr=42,
        reviewers={"copilot": "done_clean"},
        checks=ChecksState.CANCELLED,
        mergeable="MERGEABLE",
    )
    out = status_verb.format_status(status)
    assert "checks:     cancelled" in out
    assert "gh run rerun" in out
    assert "failing" not in out


def test_format_status_renders_no_pr_as_the_short_two_line_form():
    from shipit.prstate.state import no_pr

    out = status_verb.format_status(no_pr())
    assert out.startswith("state:  no_pr\nnext:   ")
    assert "reviewers" not in out


def test_status_json_carries_the_structured_degraded_set(capsys):
    from shipit.verbs._render import emit

    status = TaskStatus(
        state=TaskState.READY,
        next_action="run `pr ready`",
        pr=42,
        degraded={"codex-local": "timed_out"},
    )
    emit(status, status_verb.format_status, as_json=True)
    assert json.loads(capsys.readouterr().out)["degraded"] == {
        "codex-local": "timed_out"
    }


def test_status_explicit_pr_argument(patched, capsys):
    rc = cli.main(["pr", "status", "7", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["pr"] == 7


def test_no_pr_is_normal_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(status_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = cli.main(["pr", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "no_pr"
    assert payload["pr"] is None


def test_gh_failure_on_known_pr_is_runtime_tier_error_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(
        status_verb, "resolve_pr", lambda pr, repo, branch: PrId(repo=repo, number=42)
    )

    def boom(target, roster, **kw):
        raise ExecError(["gh"], rc=1, stderr="gh exploded")

    monkeypatch.setattr(status_verb, "gather", boom)
    rc = cli.main(["pr", "status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "gh exploded" in err


def test_gh_failure_during_resolution_is_fatal(monkeypatch, capsys):

    def boom(pr, repo, branch):
        raise ExecError(["gh"], rc=1, stderr="gh auth exploded")

    monkeypatch.setattr(status_verb, "resolve_pr", boom)
    rc = cli.main(["pr", "status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "gh auth exploded" in err


def test_malformed_pr_argument_is_usage_tier_exit_2(capsys):
    rc = cli.main(["pr", "status", "not-a-number"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Usage:" in err
    assert "not-a-number" in err


def test_nonpositive_pr_argument_is_usage_tier_exit_2(capsys):
    rc = cli.main(["pr", "status", "0"])
    assert rc == 2
    assert "Usage:" in capsys.readouterr().err
