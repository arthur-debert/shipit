"""Tests for `shipit pr ready` — the guarded flip + `--undo` (WS06).

The guarded flip (`guarded_flip`) is exercised with an INJECTED boundary (no
network): it must refuse unless the engine says READY, and flip exactly once when
READY. The CLI `run` is smoke-tested for the refuse / flip / undo paths with the
resolver + flip boundary monkeypatched.
"""

from __future__ import annotations

import pytest

from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import ready as ready_verb
from shipit.execrun import ExecError

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


# --- guarded_flip: the shared re-check ---------------------------------------


def test_guarded_flip_flips_when_ready():
    # Typed in, typed out (ADR-0030): the guard takes the PrId target and both
    # injected boundaries receive the SAME identity — repo + number travel
    # together through re-check and flip.
    flips: list[PrId] = []
    status = ready_verb.guarded_flip(
        TARGET,
        flip=lambda target: flips.append(target),
        evaluate_status=lambda target: _status(TaskState.READY, target.number),
    )
    assert flips == [TARGET]
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
    flips: list[PrId] = []
    with pytest.raises(ready_verb.NotReady) as exc:
        ready_verb.guarded_flip(
            TARGET,
            flip=lambda target: flips.append(target),
            evaluate_status=lambda target: _status(state),
        )
    assert flips == []  # never flipped
    assert exc.value.status.state is state


# --- the CLI run() shell -----------------------------------------------------


@pytest.fixture
def ready_pr(monkeypatch):
    """resolve → the typed #42 target; guarded_flip flips a READY status.
    No network — `run` is driven directly with the repo injected as a value
    (the prstate style: typed value in, exit code out)."""
    monkeypatch.setattr(
        ready_verb,
        "resolve_pr",
        lambda pr, repo: PrId(repo=repo, number=pr if pr is not None else 42),
    )
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda target: _status(TaskState.READY, target.number),
    )


def test_run_flips_when_ready(ready_pr, capsys):
    rc = ready_verb.run(repo=REPO)
    assert rc == 0
    assert "flipped draft→ready" in capsys.readouterr().out


def test_run_refuses_when_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: TARGET)

    def refuse(target):
        raise ready_verb.NotReady(_status(TaskState.BLOCKED))

    monkeypatch.setattr(ready_verb, "guarded_flip", refuse)
    rc = ready_verb.run(repo=REPO)
    assert rc != 0
    err = capsys.readouterr().err
    assert "refusing to flip" in err
    assert "not Ready" in err


def test_run_undo_always_allowed(monkeypatch, capsys):
    """--undo reverts ready→draft without any readiness check."""
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: TARGET)
    undos: list[tuple[PrId, bool]] = []
    monkeypatch.setattr(
        ready_verb.gh,
        "pr_ready",
        lambda target, *, undo=False: undos.append((target, undo)),
    )
    # guarded_flip must NOT be consulted on --undo.
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda target: (_ for _ in ()).throw(AssertionError("undo must not be held")),
    )
    rc = ready_verb.run(undo=True, repo=REPO)
    assert rc == 0
    # The adapter received the TYPED target — the repo rides on the identity.
    assert undos == [(TARGET, True)]
    assert "reverted ready→draft" in capsys.readouterr().out


def test_run_no_pr_is_error(monkeypatch, capsys):
    """A mutating verb on a branch with no PR is a clean non-zero error."""
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: None)
    rc = ready_verb.run(repo=REPO)
    assert rc != 0
    assert "no PR for this branch" in capsys.readouterr().err


def test_run_gh_failure_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: TARGET)

    def boom(target):
        raise ExecError(["gh"], rc=1, stderr="gh boom")

    monkeypatch.setattr(ready_verb, "guarded_flip", boom)
    rc = ready_verb.run(repo=REPO)
    assert rc != 0
    assert "gh boom" in capsys.readouterr().err


def test_run_outside_a_checkout_is_the_uniform_refusal(capsys):
    """No injected repo + no click context + no ambient repo consultation here:
    driving `run` directly with no repo resolves through the (empty) root
    context and surfaces the ONE uniform outside-a-checkout error (ADR-0030)."""
    rc = ready_verb.run(42)
    assert rc == 1
    assert "not inside a repository checkout" in capsys.readouterr().err
