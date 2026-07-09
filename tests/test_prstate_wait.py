"""Unit tests for the engine's blocking wait loop (`shipit.prstate.wait`,
RVW02-WS01 / ADR-0034) and the `reviews_in` predicate it reads.

The loop is a pure composition over injected seams — a scripted status source,
a fake clock, a recording sleep — so every behavior is proved without a
network or real time: both `--until` conditions, the hard-deadline timeout
path (with the final nap clamped to the deadline), state-change emission
(one line + one ``wait.state_changed`` per CHANGE, never per tick), and the
``wait.started`` / ``wait.fired`` / ``wait.timed_out`` flow-log records.
"""

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
    """A fake monotonic clock + recording sleep: sleeping advances time."""

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


# --- reviews_in: the structured predicate ------------------------------------


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
    # ADR-0006: failed / empty / timed-out are terminal — the review "landed"
    # as a recorded non-delivery; the round is not held open by a broken
    # reviewer.
    status = _status(
        funnel={
            "copilot": FunnelState.EMPTY,
            "codex": FunnelState.TIMED_OUT,
            "agy": FunnelState.FAILED,
        }
    )
    assert reviews_in(status, ("copilot", "codex", "agy"))


def test_reviews_in_missing_required_name_counts_as_holding():
    # Absence of evidence is not a landed review: a snapshot that never
    # evaluated a required reviewer must not read as reviews-in.
    status = _status(funnel={"copilot": FunnelState.POSTED})
    assert not reviews_in(status, ("copilot", "codex"))


def test_reviews_in_ignores_best_effort_reviewers():
    # Only the REQUIRED set is consulted; a best-effort reviewer still in
    # flight never delays the round.
    status = _status(
        funnel={"copilot": FunnelState.POSTED, "gemini": FunnelState.IN_FLIGHT}
    )
    assert reviews_in(status, ("copilot",))


def test_reviews_in_is_not_a_state_check():
    # A red-checks PR with its reviews landed ranks BLOCKED (#352) and an
    # unclassified round ranks ADDRESSING (#423) — both are reviews-in; the
    # predicate reads the funnel, never TaskState.
    for state in (TaskState.BLOCKED, TaskState.ADDRESSING):
        status = _status(state=state, funnel={"copilot": FunnelState.POSTED})
        assert reviews_in(status, ("copilot",))


# --- satisfied: the --until vocabulary ----------------------------------------


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
    # reviews landed but CI still running: reviews-in fires, ready does not.
    status = _status(state=TaskState.VALIDATING, funnel={"copilot": FunnelState.POSTED})
    assert satisfied(Until.REVIEWS_IN, status, ("copilot",))
    assert not satisfied(Until.READY, status, ("copilot",))


def test_dead_run_times_out_actionable_with_the_rerun_advice():
    # The #621 hang guard, wait-surface half: the engine ranks a cancelled
    # (dead) run BLOCKED with RERUN advice — NOT VALIDATING — so a
    # `--until ready` wait on it never idles as "CI running"; it expires on
    # the hard deadline carrying the actionable rerun line for the supervisor.
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


# --- wait_for: the loop --------------------------------------------------------


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
    assert clock.naps == []  # no sleep before the first poll


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
    # The state report rides the result: the caller renders the next-action line.
    assert "copilot" in result.status.next_action
    # The final nap is CLAMPED to the remaining deadline (60, 60, then 30) so
    # expiry is prompt — never up to a full interval late.
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
    # First observation is a change from nothing; the two unchanged re-reads
    # emit nothing; the move emits once.
    assert seen == ["reviews_pending", "validating"]


def test_next_action_movement_counts_as_a_change():
    # The lifecycle state can stay put while a reviewer lands — the engine's
    # next-action line moves, and the tailer must see it.
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
    # Two observed states (pending, then landed) → exactly two change events;
    # the unchanged middle tick leaves no record.
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
    # The event message carries the next-action state report, with no
    # "still waiting on:" prefix duplicating its own "waiting on …" lead.
    message = timed_out[0].getMessage()
    assert message.endswith("— waiting on required review(s): copilot")
    assert "waiting on required review(s): waiting on" not in message


def test_wait_event_names_are_registered():
    # The closed vocabulary (ADR-0032) carries the waiter's four names — a
    # typo'd emit would raise, so pin the registration.
    for name in ("wait.started", "wait.state_changed", "wait.fired", "wait.timed_out"):
        assert name in events.EVENT_NAMES


def test_poll_source_errors_propagate():
    # A real gh/auth failure must NOT be retried until the deadline: it is an
    # error, not a state to wait through.
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
    # ADR-0034: the documented default the config override replaces.
    assert POLL_INTERVAL_SECONDS == 60


def test_result_to_dict_is_the_json_surface():
    landed = _status(funnel={"copilot": FunnelState.POSTED})
    result, _ = _run([landed], Until.REVIEWS_IN)
    payload = result.to_dict()
    assert payload["outcome"] == "fired"
    assert payload["until"] == "reviews-in"
    assert payload["ticks"] == 1
    assert payload["status"]["state"] == "reviews_pending"
