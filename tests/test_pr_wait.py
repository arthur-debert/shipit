"""Smoke tests for `shipit pr wait` CLI wiring — glue + renderers (ADR-0034).

Proves the verb registers on the `pr` group and that its run shell — resolve →
loop(gather → evaluate) → render — wires the engine's `wait_for` with the
config-owned poll interval, the stderr progress line, and the exit-code
contract (0 fired / 1 error / 2 usage / 3 timeout / 4 caller-actionable stop,
#583). The loop's own semantics
(conditions, clamped deadline, event emission) are the engine's and are
unit-tested in test_prstate_wait.py; here the boundary (resolver / gather /
evaluate / load_roster) is monkeypatched — no network, no real time.
"""

from __future__ import annotations

import json

import pytest

from shipit import cli
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.model import FunnelState, ReviewLifecycle
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import ReviewerFunnel, TaskState, TaskStatus
from shipit.prstate.wait import Until
from shipit.verbs.pr import wait as wait_verb

REPO = repo_from_slug("owner/repo")


def _status(state: TaskState, pr: int = 42, next_action: str = "do x") -> TaskStatus:
    settled = state is not TaskState.REVIEWS_PENDING
    return TaskStatus(
        state=state,
        next_action=next_action,
        pr=pr,
        reviewer_funnel={
            "copilot": ReviewerFunnel(
                lifecycle=ReviewLifecycle.DONE_CLEAN
                if settled
                else ReviewLifecycle.REQUESTED,
                state=FunnelState.POSTED if settled else FunnelState.REQUESTED,
            )
        },
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


@pytest.fixture
def patched_wait(monkeypatch):
    """resolve → the typed PrId target (#42); gather passes the target through;
    the roster is the copilot-required default shape. Tests script `evaluate`."""
    monkeypatch.setattr(
        wait_verb,
        "resolve_pr",
        lambda pr, repo, branch: PrId(repo=repo, number=pr if pr is not None else 42),
    )
    monkeypatch.setattr(wait_verb, "gather", lambda target, roster, **kw: target)
    monkeypatch.setattr(
        wait_verb,
        "load_roster",
        lambda: Roster((RosterEntry(name="copilot", required=True),)),
    )


# --- wiring -------------------------------------------------------------------


def test_pr_help_lists_wait(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "wait" in capsys.readouterr().out


def test_wait_help_documents_the_surface(capsys):
    rc = cli.main(["pr", "wait", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--until" in out
    assert "--timeout" in out
    assert "30m" in out  # the documented default deadline
    assert "--json" in out


def test_until_is_required_usage_tier(capsys):
    rc = cli.main(["pr", "wait"])
    assert rc == 2
    assert "--until" in capsys.readouterr().err


def test_bad_until_value_is_usage_tier(capsys):
    rc = cli.main(["pr", "wait", "--until", "merged"])
    assert rc == 2


def test_malformed_timeout_is_usage_tier(capsys):
    rc = cli.main(["pr", "wait", "--until", "ready", "--timeout", "soonish"])
    assert rc == 2
    assert "Usage:" in capsys.readouterr().err


# --- run shell ----------------------------------------------------------------


def test_fires_exit_zero_and_renders_the_status(patched_wait, monkeypatch, capsys):
    monkeypatch.setattr(
        wait_verb, "evaluate", lambda ctx: _status(TaskState.READY, ctx.number)
    )
    rc = cli.main(["pr", "wait", "--until", "ready"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wait: ready fired" in out
    assert "state:      ready" in out


def test_json_carries_outcome_and_status(patched_wait, monkeypatch, capsys):
    monkeypatch.setattr(
        wait_verb, "evaluate", lambda ctx: _status(TaskState.ADDRESSING, ctx.number)
    )
    rc = cli.main(["pr", "wait", "--until", "reviews-in", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "fired"
    assert payload["until"] == "reviews-in"
    assert payload["status"]["state"] == "addressing"


def test_no_pr_is_a_refusal_exit_1(monkeypatch, capsys):
    # Unlike `pr status` (a normal no_pr report), a wait with no PR would poll
    # a nonexistent target until its deadline — refuse loud instead.
    monkeypatch.setattr(wait_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = cli.main(["pr", "wait", "--until", "ready"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no PR" in err


def test_timeout_is_the_distinct_exit_code_with_a_state_report(
    patched_wait, monkeypatch, capsys
):
    monkeypatch.setattr(
        wait_verb,
        "evaluate",
        lambda ctx: _status(
            TaskState.REVIEWS_PENDING, ctx.number, "waiting on: copilot re-review"
        ),
    )
    clock = Clock()
    rc = wait_verb.run(
        42,
        until=Until.READY,
        timeout_seconds=90.0,
        repo=REPO,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert rc == wait_verb.EXIT_TIMEOUT == 3
    out = capsys.readouterr().out
    assert "timed out" in out
    # The headline carries the engine's next-action line verbatim as the state
    # report — no "still waiting on:" prefix duplicating next_action's own
    # "waiting on …" lead.
    assert "— waiting on: copilot re-review" in out
    assert "waiting on: waiting on:" not in out


def test_addressing_stops_a_ready_wait_with_the_distinct_exit_code(
    patched_wait, monkeypatch, capsys
):
    # The #583 deadlock guard: `--until ready` observing `addressing` — a state
    # only the waiting caller can clear — exits promptly with the DISTINCT code
    # 4 and the engine's next-action line, instead of polling to the deadline.
    monkeypatch.setattr(
        wait_verb,
        "evaluate",
        lambda ctx: _status(TaskState.ADDRESSING, ctx.number, "classify 1 finding(s)"),
    )
    clock = Clock()
    rc = wait_verb.run(
        42,
        until=Until.READY,
        timeout_seconds=1800.0,
        repo=REPO,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert rc == wait_verb.EXIT_ACTIONABLE == 4
    assert clock.naps == []  # the first observation already stops the wait
    out = capsys.readouterr().out
    assert "addressing" in out
    assert "classify 1 finding(s)" in out


def test_actionable_json_carries_the_distinct_outcome(
    patched_wait, monkeypatch, capsys
):
    monkeypatch.setattr(
        wait_verb, "evaluate", lambda ctx: _status(TaskState.ADDRESSING, ctx.number)
    )
    rc = cli.main(["pr", "wait", "--until", "ready", "--json"])
    assert rc == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "actionable"
    assert payload["until"] == "ready"
    assert payload["status"]["state"] == "addressing"


def test_progress_lines_go_to_stderr_on_state_change(patched_wait, monkeypatch, capsys):
    states = iter(
        [
            _status(TaskState.REVIEWS_PENDING, 42, "waiting on: copilot"),
            _status(TaskState.REVIEWS_PENDING, 42, "waiting on: copilot"),
            _status(TaskState.READY, 42, "flip it"),
        ]
    )
    monkeypatch.setattr(wait_verb, "evaluate", lambda ctx: next(states))
    clock = Clock()
    rc = wait_verb.run(
        42,
        until=Until.READY,
        timeout_seconds=600.0,
        repo=REPO,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert rc == 0
    captured = capsys.readouterr()
    # One stderr line per CHANGE (first observation + the move), not per tick.
    progress = [line for line in captured.err.splitlines() if "pr#42 wait:" in line]
    assert len(progress) == 2
    assert "reviews_pending" in progress[0]
    assert "ready" in progress[1]
    # stdout stays the typed result only.
    assert "pr#42 wait:" not in captured.out


def test_poll_interval_comes_from_the_roster_config(patched_wait, monkeypatch):
    # ADR-0034: cadence is tool-owned config (`[reviewers].poll_interval`),
    # never a per-call flag — the verb reads it off the ONE loaded Roster.
    monkeypatch.setattr(
        wait_verb,
        "load_roster",
        lambda: Roster((RosterEntry(name="copilot", required=True),), poll_interval=5),
    )
    states = iter(
        [
            _status(TaskState.REVIEWS_PENDING, 42),
            _status(TaskState.READY, 42),
        ]
    )
    monkeypatch.setattr(wait_verb, "evaluate", lambda ctx: next(states))
    clock = Clock()
    rc = wait_verb.run(
        42,
        until=Until.READY,
        timeout_seconds=600.0,
        repo=REPO,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert rc == 0
    assert clock.naps == [5.0]


def test_poll_interval_defaults_to_the_documented_60s(patched_wait, monkeypatch):
    states = iter(
        [
            _status(TaskState.REVIEWS_PENDING, 42),
            _status(TaskState.READY, 42),
        ]
    )
    monkeypatch.setattr(wait_verb, "evaluate", lambda ctx: next(states))
    clock = Clock()
    rc = wait_verb.run(
        42,
        until=Until.READY,
        timeout_seconds=600.0,
        repo=REPO,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert rc == 0
    assert clock.naps == [60.0]


def test_gh_failure_mid_wait_is_uniform_error_exit_1(patched_wait, monkeypatch, capsys):
    # A real boundary failure on a poll tick must surface through the error
    # shell, never be silently retried until the deadline.
    from shipit.execrun import ExecError

    def boom(ctx):
        raise ExecError(["gh", "pr", "view"], rc=1, stderr="auth failed")

    monkeypatch.setattr(wait_verb, "evaluate", boom)
    rc = cli.main(["pr", "wait", "--until", "ready"])
    assert rc == 1
    err = capsys.readouterr().err
    # The error contract is ONE `error: …` LINE on stderr — which the wait
    # verb deliberately shares with its progress lines (and, depending on the
    # sink config, log records: the `wait.started` event lands there in CI),
    # so assert on the line, not on the stream's first bytes.
    assert any(line.startswith("error: ") for line in err.splitlines())
    assert "gh pr view failed" in err
