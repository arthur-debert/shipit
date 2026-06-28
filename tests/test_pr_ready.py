"""Tests for `shipit pr ready` — the guarded flip + `--undo` (WS06).

The guarded flip (`guarded_flip`) is exercised with an INJECTED boundary (no
network): it must refuse unless the engine says READY, and flip exactly once when
READY. The CLI `run` is smoke-tested for the refuse / flip / undo paths with the
resolver + flip boundary monkeypatched.
"""

from __future__ import annotations

import pytest

from shipit.prstate import ghapi
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import ready as ready_verb


def _status(state: TaskState, pr: int = 42) -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action="…",
        pr=pr,
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
    )


# --- guarded_flip: the shared re-check ---------------------------------------


def test_guarded_flip_flips_when_ready():
    flips: list[int] = []
    status = ready_verb.guarded_flip(
        42,
        flip=lambda pr: flips.append(pr),
        evaluate_status=lambda pr: _status(TaskState.READY, pr),
    )
    assert flips == [42]
    assert status.state is TaskState.READY


@pytest.mark.parametrize(
    "state",
    [
        TaskState.REVIEWS_PENDING,
        TaskState.ADDRESSING,
        TaskState.REVIEWED,
        TaskState.VALIDATING,
        TaskState.BLOCKED,
        TaskState.NO_PR,
    ],
)
def test_guarded_flip_refuses_when_not_ready(state):
    flips: list[int] = []
    with pytest.raises(ready_verb.NotReady) as exc:
        ready_verb.guarded_flip(
            42,
            flip=lambda pr: flips.append(pr),
            evaluate_status=lambda pr: _status(state),
        )
    assert flips == []  # never flipped
    assert exc.value.status.state is state


# --- the CLI run() shell -----------------------------------------------------


@pytest.fixture
def ready_pr(monkeypatch):
    """resolve → 42; guarded_flip flips a READY status. No network."""
    monkeypatch.setattr(
        ready_verb, "resolve_pr", lambda pr: pr if pr is not None else 42
    )
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda pr: _status(TaskState.READY, pr),
    )


def test_run_flips_when_ready(ready_pr, capsys):
    rc = ready_verb.run()
    assert rc == 0
    assert "flipped draft→ready" in capsys.readouterr().out


def test_run_refuses_when_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr: 42)

    def refuse(pr):
        raise ready_verb.NotReady(_status(TaskState.BLOCKED))

    monkeypatch.setattr(ready_verb, "guarded_flip", refuse)
    rc = ready_verb.run()
    assert rc != 0
    err = capsys.readouterr().err
    assert "refusing to flip" in err
    assert "not Ready" in err


def test_run_undo_always_allowed(monkeypatch, capsys):
    """--undo reverts ready→draft without any readiness check."""
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr: 42)
    undos: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        ready_verb.ghapi,
        "pr_ready",
        lambda pr, *, undo=False: undos.append((pr, undo)),
    )
    # guarded_flip must NOT be consulted on --undo.
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda pr: (_ for _ in ()).throw(AssertionError("undo must not be held")),
    )
    rc = ready_verb.run(undo=True)
    assert rc == 0
    assert undos == [(42, True)]
    assert "reverted ready→draft" in capsys.readouterr().out


def test_run_no_pr_is_error(monkeypatch, capsys):
    """A mutating verb on a branch with no PR is a clean non-zero error."""
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr: None)
    rc = ready_verb.run()
    assert rc != 0
    assert "no PR for this branch" in capsys.readouterr().err


def test_run_gh_failure_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr: 42)

    def boom(pr):
        raise ghapi.GhError("gh boom")

    monkeypatch.setattr(ready_verb, "guarded_flip", boom)
    rc = ready_verb.run()
    assert rc != 0
    assert "gh boom" in capsys.readouterr().err
