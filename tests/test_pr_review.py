from __future__ import annotations

import pytest

from shipit import cli
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.request import RequestResult, ReviewerOutcome
from shipit.prstate.roster import Roster
from shipit.verbs.pr import review as review_verb

REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=7)


def test_local_agent_request_detaches_in_flight(monkeypatch, capsys):
    from shipit.review import service

    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    detached: list = []
    monkeypatch.setattr(
        service,
        "start_detached_review",
        lambda backend, pr, **kw: detached.append((backend.funnel_agent, pr)) or True,
    )
    rc = review_verb.run(7, reviewer="codex", repo=REPO)
    assert rc == 0
    assert detached == [("codex", TARGET)]
    assert "review in flight: codex on #7" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["codex-local", "agy-local"])
def test_local_agent_spec_alias_detaches(monkeypatch, capsys, name):
    from shipit.review import service

    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    monkeypatch.setattr(
        service, "start_detached_review", lambda backend, pr, **kw: True
    )
    rc = review_verb.run(7, reviewer=name, repo=REPO)
    assert rc == 0
    out = capsys.readouterr().out
    assert "review in flight:" in out


def test_local_alias_does_not_match_app_reviewer(capsys):
    rc = review_verb.run(7, reviewer="copilot-local", repo=REPO)
    assert rc == 1
    assert "unknown reviewer" in capsys.readouterr().err


def test_unknown_reviewer_is_rejected(capsys):
    rc = review_verb.run(7, reviewer="copliot", repo=REPO)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "unknown reviewer" in err
    assert "copilot" in err


def test_no_pr_for_branch_is_fatal(monkeypatch, capsys):
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = review_verb.run(None, reviewer="copilot", repo=REPO)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no PR" in err


def test_gh_failure_resolving_is_fatal(monkeypatch, capsys):

    def boom(pr, repo, branch):
        raise ExecError(["gh"], rc=1, stderr="gh auth exploded")

    monkeypatch.setattr(review_verb, "resolve_pr", boom)
    rc = review_verb.run(None, reviewer="copilot", repo=REPO)
    assert rc == 1
    assert "gh auth exploded" in capsys.readouterr().err


def test_verb_renders_verified(monkeypatch, capsys):
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    monkeypatch.setattr(
        review_verb,
        "request_reviewers",
        lambda pr, adapters, roster, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "verified")]
        ),
    )
    monkeypatch.setattr(review_verb, "required_adapters", lambda roster: [object()])
    monkeypatch.setattr(review_verb, "load_roster", lambda: Roster())
    rc = review_verb.run(7, repo=REPO)
    assert rc == 0
    assert "verified: copilot" in capsys.readouterr().out


def test_dropped_request_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    monkeypatch.setattr(
        review_verb,
        "request_reviewers",
        lambda pr, adapters, roster, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "dropped")]
        ),
    )
    monkeypatch.setattr(review_verb, "required_adapters", lambda roster: [object()])
    monkeypatch.setattr(review_verb, "load_roster", lambda: Roster())
    rc = review_verb.run(7, repo=REPO)
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "dropped by GitHub" in captured.err
    assert "copilot" in captured.err


def test_format_request_renders_each_outcome_and_the_all_skipped_note():
    result = RequestResult(
        outcomes=[
            ReviewerOutcome("copilot", "verified"),
            ReviewerOutcome("codex", "in_flight"),
            ReviewerOutcome("gemini", "no_op"),
        ]
    )
    out = review_verb.format_request(7, result)
    assert "verified: copilot request attached on #7" in out
    assert "review in flight: codex on #7" in out
    assert "gemini: auto-triggers, no request mechanism — no-op" in out

    all_skipped = RequestResult(outcomes=[ReviewerOutcome("copilot", "skipped")])
    out = review_verb.format_request(7, all_skipped)
    assert "copilot: already reviewed #7 (review-once) — skip" in out
    assert "nothing to request" in out


def test_pr_review_subgroup_registered(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "review" in capsys.readouterr().out


def test_pr_review_lists_request(capsys):
    rc = cli.main(["pr", "review", "--help"])
    assert rc == 0
    assert "request" in capsys.readouterr().out


def test_pr_review_request_help(capsys):
    rc = cli.main(["pr", "review", "request", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--reviewer" in out


def test_pr_review_run_is_hidden(capsys):
    rc = cli.main(["pr", "review", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "request" in out
    assert "_run" not in out


def test_pr_review_run_invokes_detached_child(monkeypatch):
    from shipit.review import service

    captured: dict = {}

    def fake_child(backend, pr, **kw):
        captured.update({"backend": backend, "pr": pr, **kw})
        return {}

    monkeypatch.setattr(service, "run_detached_review", fake_child)
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "owner/repo",
            "--run-id",
            "555",
        ]
    )
    assert rc == 0
    from shipit.agent import backend as agent_backend

    assert captured["backend"] is agent_backend.CODEX
    assert captured["pr"] == PrId(repo=repo_from_slug("owner/repo"), number=5)
    assert "repo" not in captured
    assert captured["run_id"] == 555
    assert captured["as_app"] is True


def test_pr_review_run_reconstructs_the_fanout_config(monkeypatch):
    from shipit.review import service

    captured: dict = {}

    def fake_child(backend, pr, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(service, "run_detached_review", fake_child)
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "owner/repo",
            "--dimensions",
            "correctness,test-quality",
            "--nit-cap",
            "0",
            "--calibrator-backend",
            "claude",
            "--calibrator-reasoning",
            "medium",
        ]
    )
    assert rc == 0
    assert captured["dimensions"] == ("correctness", "test-quality")
    assert captured["nit_cap"] == 0
    calibrator = captured["calibrator"]
    assert calibrator.backend == "claude"
    assert calibrator.reasoning == "medium"
    assert calibrator.timeout == "600s"


def test_pr_review_run_defaults_leave_the_fanout_config_unset(monkeypatch):
    from shipit.review import service

    captured: dict = {}
    monkeypatch.setattr(
        service, "run_detached_review", lambda backend, pr, **kw: captured.update(kw)
    )
    rc = cli.main(
        ["pr", "review", "_run", "--agent", "codex", "--pr", "5", "--repo", "o/r"]
    )
    assert rc == 0
    assert captured["dimensions"] is None
    assert captured["calibrator"] is None
    assert captured["nit_cap"] is None


def test_pr_review_run_rejects_a_bad_calibrator_cleanly(monkeypatch, capsys):
    from shipit.review import service

    ran: list = []
    monkeypatch.setattr(service, "run_detached_review", lambda *a, **k: ran.append(1))
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "o/r",
            "--calibrator-backend",
            "gpt-cli",
        ]
    )
    assert rc == 1
    assert "calibrator" in capsys.readouterr().err
    assert ran == []


def test_pr_review_run_rejects_a_negative_nit_cap_cleanly(monkeypatch, capsys):
    from shipit.review import service

    ran: list = []
    monkeypatch.setattr(service, "run_detached_review", lambda *a, **k: ran.append(1))
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "o/r",
            "--nit-cap",
            "-1",
        ]
    )
    assert rc == 1
    assert "nit-cap" in capsys.readouterr().err
    assert ran == []
