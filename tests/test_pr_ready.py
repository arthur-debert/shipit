from __future__ import annotations

from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import ready as ready_verb

REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=42)


def _status(state: TaskState, pr: int = 42) -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action="…",
        pr=pr,
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
    )


def _ready_pr(monkeypatch):
    monkeypatch.setattr(
        ready_verb,
        "resolve_pr",
        lambda pr, repo, branch: PrId(repo=repo, number=pr if pr is not None else 42),
    )
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda target: _status(TaskState.READY, target.number),
    )


def test_run_flips_when_ready(monkeypatch, capsys):
    _ready_pr(monkeypatch)
    rc = ready_verb.run(repo=REPO)
    assert rc == 0
    assert "flipped draft→ready" in capsys.readouterr().out


def test_run_refuses_when_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo, branch: TARGET)

    def refuse(target):
        raise ready_verb.NotReady(_status(TaskState.BLOCKED))

    monkeypatch.setattr(ready_verb, "guarded_flip", refuse)
    rc = ready_verb.run(repo=REPO)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "not Ready" in err


def test_run_undo_always_allowed(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    undone: list[PrId] = []
    monkeypatch.setattr(ready_verb, "undo_flip", lambda target: undone.append(target))
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda target: (_ for _ in ()).throw(AssertionError("undo must not be held")),
    )
    rc = ready_verb.run(undo=True, repo=REPO)
    assert rc == 0
    assert undone == [TARGET]
    assert "reverted ready→draft" in capsys.readouterr().out


def test_run_no_pr_is_error(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = ready_verb.run(repo=REPO)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no PR for this branch" in err


def test_run_gh_failure_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo, branch: TARGET)

    def boom(target):
        raise ExecError(["gh"], rc=1, stderr="gh boom")

    monkeypatch.setattr(ready_verb, "guarded_flip", boom)
    rc = ready_verb.run(repo=REPO)
    assert rc == 1
    assert "gh boom" in capsys.readouterr().err


def test_run_outside_a_checkout_is_the_uniform_refusal(capsys):
    rc = ready_verb.run(42)
    assert rc == 1
    assert "not inside a repository checkout" in capsys.readouterr().err


def test_format_flipped_and_undone_are_pure_renderers():
    assert ready_verb.format_flipped(_status(TaskState.READY)) == (
        "PR #42: flipped draft→ready — ready for human validation"
    )
    assert ready_verb.format_undone(TARGET) == "PR #42: reverted ready→draft"
