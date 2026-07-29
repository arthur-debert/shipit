from __future__ import annotations

import logging

import pytest

from shipit import events
from shipit.prstate.model import FunnelState, ReviewLifecycle
from shipit.prstate.state import (
    ChecksState,
    ReviewerFunnel,
    TaskState,
    TaskStatus,
    reviews_in,
)
from shipit.prstate.wait import (
    POLL_INTERVAL_SECONDS,
    Outcome,
    Until,
    actionable,
    satisfied,
    wait_for,
)


def _funnel(state: FunnelState) -> ReviewerFunnel:
    lifecycle = (
        ReviewLifecycle.DONE_CLEAN
        if state is FunnelState.POSTED
        else ReviewLifecycle.NOT_REQUESTED
    )
    return ReviewerFunnel(lifecycle=lifecycle, state=state)


def _status(
    state: TaskState = TaskState.REVIEWS_PENDING,
    next_action: str = "wait",
    funnel: dict[str, FunnelState] | None = None,
    pr: int = 7,
) -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action=next_action,
        pr=pr,
        checks=ChecksState.GREEN,
        reviewer_funnel={name: _funnel(fs) for name, fs in (funnel or {}).items()},
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.naps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.naps.append(seconds)
        self.now += seconds


def _events(caplog) -> list[str]:
    return [
        name
        for r in caplog.records
        if (name := getattr(r, events.EXTRA_KEY, None)) is not None
    ]


def test_reviews_in_true_when_every_required_reviewer_settled():
    status = _status(
        funnel={"copilot": FunnelState.POSTED, "codex": FunnelState.FAILED}
    )
    assert reviews_in(status, ("copilot", "codex"))


@pytest.mark.parametrize(
    "holding",
    [FunnelState.NEVER_REQUESTED, FunnelState.REQUESTED, FunnelState.IN_FLIGHT],
)
def test_reviews_in_false_while_any_required_reviewer_holds(holding):
    status = _status(funnel={"copilot": FunnelState.POSTED, "codex": holding})
    assert not reviews_in(status, ("copilot", "codex"))


def test_reviews_in_degraded_outcomes_settle():
    status = _status(
        funnel={
            "copilot": FunnelState.EMPTY,
            "codex": FunnelState.TIMED_OUT,
            "agy": FunnelState.FAILED,
        }
    )
    assert reviews_in(status, ("copilot", "codex", "agy"))


def test_reviews_in_missing_required_name_counts_as_holding():
    status = _status(funnel={"copilot": FunnelState.POSTED})
    assert not reviews_in(status, ("copilot", "codex"))


def test_reviews_in_ignores_best_effort_reviewers():
    status = _status(
        funnel={"copilot": FunnelState.POSTED, "gemini": FunnelState.IN_FLIGHT}
    )
    assert reviews_in(status, ("copilot",))


def test_reviews_in_is_not_a_state_check():
    for state in (TaskState.BLOCKED, TaskState.ADDRESSING):
        status = _status(state=state, funnel={"copilot": FunnelState.POSTED})
        assert reviews_in(status, ("copilot",))


def test_satisfied_reviews_in_mode():
    landed = _status(funnel={"copilot": FunnelState.POSTED})
    pending = _status(funnel={"copilot": FunnelState.REQUESTED})
    assert satisfied(Until.REVIEWS_IN, landed, ("copilot",))
    assert not satisfied(Until.REVIEWS_IN, pending, ("copilot",))


def test_satisfied_ready_mode_is_the_engine_verdict():
    ready = _status(state=TaskState.READY, funnel={"copilot": FunnelState.POSTED})
    blocked = _status(state=TaskState.BLOCKED, funnel={"copilot": FunnelState.POSTED})
    assert satisfied(Until.READY, ready, ("copilot",))
    assert not satisfied(Until.READY, blocked, ("copilot",))


def test_ready_mode_does_not_fire_on_reviews_in():
    status = _status(state=TaskState.VALIDATING, funnel={"copilot": FunnelState.POSTED})
    assert satisfied(Until.REVIEWS_IN, status, ("copilot",))
    assert not satisfied(Until.READY, status, ("copilot",))


def test_dead_run_times_out_with_the_rerun_advice():
    dead = _status(
        state=TaskState.BLOCKED,
        next_action=(
            "CI run cancelled/superseded, nothing still running — rerun the "
            "workflow on this head (`gh run rerun <run-id> --failed`)"
        ),
        funnel={"copilot": FunnelState.POSTED},
    )
    assert not satisfied(Until.READY, dead, ("copilot",))
    result, _ = _run([dead] * 10, Until.READY, timeout=120.0, poll=60.0)
    assert result.outcome is Outcome.TIMED_OUT
    assert "gh run rerun" in result.status.next_action


def test_actionable_fires_only_for_ready_on_addressing():
    addressing = _status(
        state=TaskState.ADDRESSING, funnel={"copilot": FunnelState.POSTED}
    )
    assert actionable(Until.READY, addressing)
    assert not actionable(Until.REVIEWS_IN, addressing)


@pytest.mark.parametrize(
    "state",
    [
        TaskState.REVIEWS_PENDING,
        TaskState.REVIEWED,
        TaskState.VALIDATING,
        TaskState.BLOCKED,
        TaskState.READY,
    ],
)
def test_actionable_is_false_for_pass_through_states(state):
    status = _status(state=state, funnel={"copilot": FunnelState.POSTED})
    assert not actionable(Until.READY, status)


def test_ready_wait_stops_early_on_addressing():
    pending = _status(funnel={"copilot": FunnelState.REQUESTED})
    addressing = _status(
        state=TaskState.ADDRESSING,
        next_action="classify 1 finding(s)",
        funnel={"copilot": FunnelState.POSTED},
    )
    result, clock = _run([pending, addressing], Until.READY, timeout=1800.0, poll=60.0)
    assert result.outcome is Outcome.ACTIONABLE
    assert result.ticks == 2
    assert clock.naps == [60.0]
    assert result.status.state is TaskState.ADDRESSING
    assert result.status.next_action == "classify 1 finding(s)"


def test_reviews_in_wait_fires_on_an_addressing_snapshot():
    addressing = _status(
        state=TaskState.ADDRESSING, funnel={"copilot": FunnelState.POSTED}
    )
    result, _ = _run([addressing], Until.REVIEWS_IN)
    assert result.outcome is Outcome.FIRED


def test_flow_log_actionable_event(caplog):
    pending = _status(funnel={"copilot": FunnelState.REQUESTED})
    addressing = _status(
        state=TaskState.ADDRESSING,
        next_action="classify 1 finding(s)",
        funnel={"copilot": FunnelState.POSTED},
    )
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        result, _ = _run([pending, addressing], Until.READY)
    assert result.outcome is Outcome.ACTIONABLE
    names = _events(caplog)
    assert names[-1] == "wait.actionable"
    assert "wait.fired" not in names
    assert "wait.timed_out" not in names
    record = [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == "wait.actionable"
    ][0]
    assert record.pr == 7
    assert record.state == "addressing"


def _run(statuses, until, timeout=600.0, poll=60.0, on_change=None):
    clock = Clock()
    feed = iter(statuses)
    result = wait_for(
        lambda: next(feed),
        pr=7,
        until=until,
        required_names=("copilot",),
        timeout_seconds=timeout,
        poll_seconds=poll,
        on_change=on_change,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return result, clock


def test_fires_immediately_when_condition_already_holds():
    result, clock = _run(
        [_status(state=TaskState.READY, funnel={"copilot": FunnelState.POSTED})],
        Until.READY,
    )
    assert result.outcome is Outcome.FIRED
    assert result.ticks == 1
    assert clock.naps == []


def test_polls_at_the_fixed_interval_until_fired():
    pending = _status(funnel={"copilot": FunnelState.REQUESTED})
    landed = _status(
        next_action="triage threads", funnel={"copilot": FunnelState.POSTED}
    )
    result, clock = _run([pending, pending, landed], Until.REVIEWS_IN, poll=60.0)
    assert result.outcome is Outcome.FIRED
    assert result.until is Until.REVIEWS_IN
    assert result.ticks == 3
    assert clock.naps == [60.0, 60.0]
    assert result.waited_seconds == 120.0


def test_timeout_returns_the_distinct_outcome_with_the_last_status():
    pending = _status(
        next_action="waiting on required review(s): copilot",
        funnel={"copilot": FunnelState.REQUESTED},
    )
    result, clock = _run([pending] * 10, Until.REVIEWS_IN, timeout=150.0, poll=60.0)
    assert result.outcome is Outcome.TIMED_OUT
    assert "copilot" in result.status.next_action
    assert clock.naps == [60.0, 60.0, 30.0]
    assert result.ticks == 4
    assert result.waited_seconds == 150.0


def test_condition_at_the_deadline_still_counts_as_fired():
    pending = _status(funnel={"copilot": FunnelState.REQUESTED})
    landed = _status(funnel={"copilot": FunnelState.POSTED})
    result, _ = _run([pending, landed], Until.REVIEWS_IN, timeout=60.0, poll=60.0)
    assert result.outcome is Outcome.FIRED


def test_on_change_fires_per_change_not_per_tick():
    pending = _status(next_action="wait for copilot")
    still_pending = _status(next_action="wait for copilot")
    moved = _status(
        state=TaskState.VALIDATING,
        next_action="reviews done; CI running",
        funnel={"copilot": FunnelState.POSTED},
    )
    seen: list[str] = []
    result, _ = _run(
        [pending, still_pending, still_pending, moved],
        Until.REVIEWS_IN,
        on_change=lambda s: seen.append(s.state.value),
    )
    assert result.outcome is Outcome.FIRED
    assert seen == ["reviews_pending", "validating"]


def test_next_action_movement_counts_as_a_change():
    first = _status(next_action="waiting on: copilot, codex")
    second = _status(next_action="waiting on: codex")
    fired = _status(funnel={"copilot": FunnelState.POSTED})
    seen: list[str] = []
    result, _ = _run(
        [first, second, fired],
        Until.REVIEWS_IN,
        on_change=lambda s: seen.append(s.next_action),
    )
    assert result.outcome is Outcome.FIRED
    assert seen[:2] == ["waiting on: copilot, codex", "waiting on: codex"]


def test_flow_log_events_started_changed_fired(caplog):
    pending = _status(next_action="wait for copilot")
    landed = _status(funnel={"copilot": FunnelState.POSTED})
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        result, _ = _run([pending, pending, landed], Until.REVIEWS_IN)
    assert result.outcome is Outcome.FIRED
    names = _events(caplog)
    assert names[0] == "wait.started"
    assert names[-1] == "wait.fired"
    assert names.count("wait.state_changed") == 2
    fired = [
        r for r in caplog.records if getattr(r, events.EXTRA_KEY, None) == "wait.fired"
    ]
    assert fired[0].pr == 7
    assert fired[0].until == "reviews-in"
    assert fired[0].ticks == 3


def test_flow_log_timeout_event(caplog):
    pending = _status(
        next_action="waiting on required review(s): copilot",
        funnel={"copilot": FunnelState.REQUESTED},
    )
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        result, _ = _run([pending] * 5, Until.READY, timeout=100.0, poll=60.0)
    assert result.outcome is Outcome.TIMED_OUT
    names = _events(caplog)
    assert names[-1] == "wait.timed_out"
    assert "wait.fired" not in names
    timed_out = [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == "wait.timed_out"
    ]
    message = timed_out[0].getMessage()
    assert message.endswith("— waiting on required review(s): copilot")
    assert "waiting on required review(s): waiting on" not in message


def test_wait_event_names_are_registered():
    for name in (
        "wait.started",
        "wait.state_changed",
        "wait.fired",
        "wait.timed_out",
        "wait.actionable",
    ):
        assert name in events.EVENT_NAMES


def test_poll_source_errors_propagate():
    def boom():
        raise RuntimeError("gh exploded")

    clock = Clock()
    with pytest.raises(RuntimeError, match="gh exploded"):
        wait_for(
            boom,
            pr=7,
            until=Until.READY,
            required_names=("copilot",),
            timeout_seconds=60.0,
            poll_seconds=60.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )


def test_shipped_default_interval_is_documented_60s():
    assert POLL_INTERVAL_SECONDS == 60


def test_result_to_dict_is_the_json_surface():
    landed = _status(funnel={"copilot": FunnelState.POSTED})
    result, _ = _run([landed], Until.REVIEWS_IN)
    payload = result.to_dict()
    assert payload["outcome"] == "fired"
    assert payload["until"] == "reviews-in"
    assert payload["ticks"] == 1
    assert payload["status"]["state"] == "reviews_pending"
