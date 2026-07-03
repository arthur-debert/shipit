"""Unit tests for the guarded draft→ready flip (`shipit.prstate.flip`).

The guard at its domain home (CLI01-WS03 promoted it out of ``verbs/pr/``),
exercised prstate-style with INJECTED boundaries (no network): it must refuse
unless the engine says READY, and flip exactly once when READY. The durable
log twins (LOG02 / ADR-0029) are pinned here too — the flip INFO milestone and
the refusal WARNING live with the service, plus the gh-adapter milestone at
the boundary that performs the flag flip.
"""

from __future__ import annotations

import logging

import pytest

from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.flip import NotReady, guarded_flip
from shipit.prstate.state import ChecksState, TaskState, TaskStatus

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


def test_guarded_flip_flips_when_ready():
    # Typed in, typed out (ADR-0030): the guard takes the PrId target and both
    # injected boundaries receive the SAME identity — repo + number travel
    # together through re-check and flip.
    flips: list[PrId] = []
    status = guarded_flip(
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
    with pytest.raises(NotReady) as exc:
        guarded_flip(
            TARGET,
            flip=lambda target: flips.append(target),
            evaluate_status=lambda target: _status(state),
        )
    assert flips == []  # never flipped
    assert exc.value.status.state is state


def test_not_ready_message_names_the_state_and_next_action():
    """The refusal wording rides the exception (ADR-0030: per-verb refusal
    wording survives as exception messages, rendered by the one error shell)."""
    exc = NotReady(_status(TaskState.VALIDATING))
    assert "PR #42 is not Ready" in str(exc)
    assert "validating" in str(exc)


# --- the durable log twins (LOG02 / ADR-0029) ----------------------------------


def _pr_records(caplog, level: int):
    return [
        r
        for r in caplog.records
        if r.name == "shipit.prstate" and r.levelno == level and getattr(r, "pr", None)
    ]


def test_flip_is_an_info_milestone_with_the_pr_key(caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        guarded_flip(
            TARGET,
            flip=lambda target: None,
            evaluate_status=lambda target: _status(TaskState.READY, target.number),
        )
    milestones = _pr_records(caplog, logging.INFO)
    assert len(milestones) == 1
    assert milestones[0].pr == 42


def test_refused_flip_is_a_warning_with_the_pr_key(caplog):
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        with pytest.raises(NotReady):
            guarded_flip(
                TARGET,
                flip=lambda target: None,
                evaluate_status=lambda target: _status(TaskState.VALIDATING),
            )
    assert not _pr_records(caplog, logging.INFO)  # nothing flipped, no milestone
    warnings = _pr_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].pr == 42


def test_gh_adapter_flip_leaves_a_durable_milestone(monkeypatch, caplog):
    """The boundary that PERFORMS the flip records it (before #285 its only
    record was the Exec runner's DEBUG line). This adapter milestone is also
    the `--undo` path's durable record — the verb no longer logs."""
    from shipit import gh

    monkeypatch.setattr(gh, "_run", lambda args, **k: "")
    with caplog.at_level(logging.INFO, logger="shipit.gh"):
        gh.pr_ready(PrId(repo=REPO, number=7))
    milestones = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and getattr(r, "pr", None) == 7
    ]
    assert len(milestones) == 1
    assert milestones[0].repo == "owner/repo"
