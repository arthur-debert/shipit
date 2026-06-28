"""OBS04-WS01 — the snapshot carries structured funnel state + an injected "now".

These tests pin the data-carrying foundation the later OBS04 workstreams read:

  (a) a fixed "now" + a recorded snapshot is deterministic (the engine reads
      "now" off the snapshot, never a wall clock);
  (b) `TaskStatus` carries structured per-reviewer funnel data (lifecycle paired
      with the OBS02/ADR-0005 funnel check-run breadcrumb), consumable WITHOUT
      parsing `next_action` prose;
  (c) the `review: <agent>-local` funnel check runs do NOT corrupt the CI
      `classify_checks` verdict — a failed local review must never read the CI
      checks as FAILING.

WS01 only CARRIES the data; the readiness pillars redefinition (WS02), the wait-window
timeout (WS03), and the dispatcher rewrite (WS04) are out of scope here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import DEFAULT_NOW, load_context
from shipit.prstate.model import FunnelState, ReviewFunnelCheck
from shipit.prstate.reviewers import by_name
from shipit.prstate.state import ChecksState, ReviewLifecycle, evaluate


# --- "now" is injected, not read from a clock ------------------------------


def test_snapshot_carries_injected_now(context):
    """The fixture's `now` rides onto the snapshot verbatim (tz-aware UTC)."""
    ctx = context("local_funnel_failed_ci_green")
    assert ctx.now == datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    assert ctx.now.tzinfo is not None


def test_default_now_when_fixture_omits_it(context):
    """A fixture with no `now` field gets the fixed default — still deterministic."""
    assert context("ready_checks_green").now == DEFAULT_NOW


def test_fixed_now_plus_recorded_snapshot_is_deterministic(context):
    """A fixed "now" + a recorded snapshot → a byte-identical engine result.

    Built twice from the same fixture and "now"; the two `to_dict()`s match, so
    the engine read no wall clock between the calls."""
    first = evaluate(context("local_funnel_failed_ci_green")).to_dict()
    second = evaluate(context("local_funnel_failed_ci_green")).to_dict()
    assert first == second


def test_now_ages_an_inflight_reviewer_past_its_window(context):
    """WS03 consumes the injected "now": the SAME recorded snapshot reads an
    in-flight reviewer IN_FLIGHT at a "now" within its wait window and TIMED_OUT at
    one well past it — with no wall clock. The fixture's agy-local run is IN_PROGRESS
    (started 00:25); at the fixture's own now (00:30, +5m) it holds, and at a now
    decades later it has aged out. This is the whole point of the injected clock:
    the window is a pure function of "now" and the run's `started_at`."""
    within = evaluate(context("local_funnel_failed_ci_green"))  # now 00:30, +5m
    past = evaluate(
        load_context(
            "local_funnel_failed_ci_green",
            now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
    )
    assert within.reviewer_funnel["agy"].state is FunnelState.IN_FLIGHT
    assert past.reviewer_funnel["agy"].state is FunnelState.TIMED_OUT


# --- funnel breadcrumbs are split out of the CI rollup ---------------------


def test_funnel_runs_lifted_out_of_ci_checks(context):
    """`review: <agent>-local` runs land in `review_funnel`, NOT `checks`."""
    ctx = context("local_funnel_failed_ci_green")
    check_names = {c.get("name") or c.get("context") for c in ctx.checks}
    assert check_names == {"ci / check", "license/cla"}
    assert all(not (c.get("name") or "").startswith("review:") for c in ctx.checks)
    reviewers = {f.reviewer for f in ctx.review_funnel}
    assert reviewers == {"codex-local", "agy-local"}


def test_failed_funnel_run_does_not_fail_the_ci_checks(context):
    """The codex-local funnel run failed, yet CI reads GREEN — the two never cross.

    This is the load-bearing subtlety: left in the rollup, the funnel FAILURE
    would dominate `classify_checks` and block the PR on phantom "CI failing"."""
    status = evaluate(context("local_funnel_failed_ci_green"))
    assert status.checks is ChecksState.GREEN


def test_funnel_check_parsed_fields(context):
    """The breadcrumb carries the raw status/conclusion/started_at off the rollup."""
    ctx = context("local_funnel_failed_ci_green")
    by_reviewer = {f.reviewer: f for f in ctx.review_funnel}
    codex = by_reviewer["codex-local"]
    assert codex == ReviewFunnelCheck(
        reviewer="codex-local",
        status="COMPLETED",
        conclusion="FAILURE",
        started_at="2026-01-01T00:00:00Z",
    )
    agy = by_reviewer["agy-local"]
    assert agy.status == "IN_PROGRESS"
    assert agy.conclusion is None
    assert agy.started_at == "2026-01-01T00:25:00Z"


# --- the adapter, not the engine, maps a reviewer to its breadcrumb --------


def test_local_adapter_claims_its_funnel_run(context):
    """A local adapter resolves its own `review: <agent>-local` breadcrumb."""
    ctx = context("local_funnel_failed_ci_green")
    codex_fc = by_name("codex").funnel_check(ctx)
    assert codex_fc is not None and codex_fc.conclusion == "FAILURE"
    agy_fc = by_name("agy").funnel_check(ctx)
    assert agy_fc is not None and agy_fc.status == "IN_PROGRESS"


def test_app_adapter_has_no_funnel_run(context):
    """Copilot sources its funnel from native signals — no check-run breadcrumb."""
    ctx = context("local_funnel_failed_ci_green")
    assert by_name("copilot").funnel_check(ctx) is None


def test_funnel_check_selects_latest_started_not_list_order(context):
    """Among same-name runs, the latest `started_at` wins — NOT rollup list order.

    `statusCheckRollup` order is not a recency contract, so a second (newer) run
    arriving BEFORE the stale one in the list must still be picked. Here the live
    in-progress run is listed first and the stale failure last; the live one wins."""
    ctx = context("local_funnel_failed_ci_green")
    ctx.review_funnel = [
        ReviewFunnelCheck(
            reviewer="codex-local",
            status="IN_PROGRESS",
            conclusion=None,
            started_at="2026-01-01T00:20:00Z",  # newer, but listed FIRST
        ),
        ReviewFunnelCheck(
            reviewer="codex-local",
            status="COMPLETED",
            conclusion="FAILURE",
            started_at="2026-01-01T00:00:00Z",  # stale, but listed LAST
        ),
    ]
    picked = by_name("codex").funnel_check(ctx)
    assert picked is not None
    assert picked.status == "IN_PROGRESS"
    assert picked.started_at == "2026-01-01T00:20:00Z"


# --- TaskStatus carries the structured per-reviewer funnel data ------------


def test_task_status_carries_structured_funnel(context):
    """`reviewer_funnel` pairs each reviewer's lifecycle with its breadcrumb."""
    status = evaluate(context("local_funnel_failed_ci_green"))
    codex = status.reviewer_funnel["codex"]
    assert codex.lifecycle is ReviewLifecycle.NOT_REQUESTED
    assert codex.check_status == "COMPLETED"
    assert codex.check_conclusion == "FAILURE"
    assert codex.check_started_at == "2026-01-01T00:00:00Z"

    # Copilot posted an approving review on the head → done, no breadcrumb.
    copilot = status.reviewer_funnel["copilot"]
    assert copilot.lifecycle is ReviewLifecycle.DONE_CLEAN
    assert copilot.check_status is None
    assert copilot.check_conclusion is None


def test_reviewer_funnel_serializes_to_dict(context):
    """The structured funnel survives `to_dict()` for the JSON surface."""
    d = evaluate(context("local_funnel_failed_ci_green")).to_dict()
    funnel = d["reviewer_funnel"]
    assert funnel["codex"] == {
        "lifecycle": "not_requested",
        "state": "failed",
        "check_status": "COMPLETED",
        "check_conclusion": "FAILURE",
        "check_started_at": "2026-01-01T00:00:00Z",
    }
    assert funnel["agy"]["check_status"] == "IN_PROGRESS"
    # Keys mirror `reviewers` — the legacy lifecycle map is unchanged + still present.
    assert set(funnel) == set(d["reviewers"])
