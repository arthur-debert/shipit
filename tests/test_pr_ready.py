"""Smoke tests for `shipit pr ready` — glue + renderers (CLI01-WS03).

The guarded flip itself is the engine's (`shipit.prstate.flip`, unit-tested in
test_prstate_flip.py); these prove the WIRING: resolve → flip/undo → render
through the seam, with the refuse / no-PR / gh-failure paths surfacing as the
one uniform ``error: …`` + exit 1 via the shared error shell. The boundary
(resolver + flip service + adapter) is monkeypatched — no network.
"""

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
    """resolve → the typed #42 target; guarded_flip flips a READY status.
    No network — `run` is driven directly with the repo injected as a value
    (the prstate style: typed value in, exit code out)."""
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
    """The engine's NotReady refusal reaches the shared error shell: one uniform
    `error: …` stderr line carrying the refusal wording, exit 1."""
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
    """--undo reverts ready→draft without any readiness check — routed through
    the engine's `undo_flip` seam (LOG04-WS02), never the guarded flip."""
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    undone: list[PrId] = []
    monkeypatch.setattr(ready_verb, "undo_flip", lambda target: undone.append(target))
    # guarded_flip must NOT be consulted on --undo.
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda target: (_ for _ in ()).throw(AssertionError("undo must not be held")),
    )
    rc = ready_verb.run(undo=True, repo=REPO)
    assert rc == 0
    # The engine seam received the TYPED target — the repo rides on the identity.
    assert undone == [TARGET]
    assert "reverted ready→draft" in capsys.readouterr().out


def test_run_no_pr_is_error(monkeypatch, capsys):
    """A mutating verb on a branch with no PR is a clean non-zero error — the
    per-verb refusal wording survives as the exception message (ADR-0030)."""
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
    """No injected repo + no click context + no ambient repo consultation here:
    driving `run` directly with no repo resolves through the (empty) root
    context and surfaces the ONE uniform outside-a-checkout error (ADR-0030)."""
    rc = ready_verb.run(42)
    assert rc == 1
    assert "not inside a repository checkout" in capsys.readouterr().err


def test_format_flipped_and_undone_are_pure_renderers():
    """The verb's renderers are plain string functions (the render seam)."""
    assert ready_verb.format_flipped(_status(TaskState.READY)) == (
        "PR #42: flipped draft→ready — …"
    )
    assert ready_verb.format_undone(TARGET) == "PR #42: reverted ready→draft"
