"""Drift guards for shipit's own thin CI caller (.github/workflows/ci.yml).

The #645 invariants: ci.yml triggers on both push and pull_request, so it MUST
carry a concurrency group that cancels superseded same-event runs without
letting the push run and the PR run of one head cancel each other, and its
aggregate `check` job MUST treat a cancelled (superseded) block as neutral
(skip) while every completed non-success result still fails explicitly. These
are behavioral promises the PR engine relies on (a cancelled run must never
read as a failed required check), so they get drift guards like the pixi-pin
lockstep test in test_install.py.
"""

from pathlib import Path

from shipit import checks

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return checks._load_yaml_text((_WORKFLOWS / name).read_text(encoding="utf-8"))


def test_ci_caller_has_superseded_run_concurrency_group():
    # #645 fix 1: colliding push+PR events need a concurrency group so a
    # superseded run cancels cleanly instead of surviving as a stale verdict.
    doc = _load("ci.yml")
    concurrency = doc.get("concurrency")
    assert isinstance(concurrency, dict), "ci.yml lost its concurrency group"
    assert concurrency.get("cancel-in-progress") is True
    group = concurrency.get("group", "")
    # Scoped per EVENT: the push run and the PR run of the same head live in
    # different groups and never cancel each other — that cross-event cancel
    # is exactly the collision #645 retires.
    assert "github.event_name" in group
    # Scoped per ref/PR: a new push supersedes only its own branch's (push)
    # or its own PR's (pull_request) in-flight run.
    assert "github.event.pull_request.number" in group
    assert "github.ref" in group


def test_ci_caller_check_job_skips_on_cancelled_block():
    # #645 fix 2: a cancelled `checks` block means the run was superseded; the
    # aggregate `check` must go NEUTRAL (skip), not fail — while still running
    # on every completed result so a genuine failure stays an explicit red.
    doc = _load("ci.yml")
    check = doc["jobs"]["check"]
    assert check["needs"] == "checks"
    condition = check["if"]
    assert "always()" in condition
    assert "needs.checks.result != 'cancelled'" in condition
    # The verdict itself stays explicit: only success passes; failure and
    # skipped both fail (a silently-skipped block must not satisfy the
    # required check).
    steps_script = "".join(step.get("run", "") for step in check["steps"])
    assert 'test "$RESULT" = "success"' in steps_script


def test_wf_checks_block_is_call_only_so_concurrency_stays_caller_side():
    # #645 fix 3 (the pinned-block question): wf-checks.yml is consumed
    # portfolio-wide via @v1. It needs NO concurrency of its own because it
    # has no event triggers — its jobs run inside the CALLER's workflow run,
    # so the caller-level group above covers them. This guard keeps that
    # reasoning true: if the block ever grows a trigger beyond workflow_call,
    # the concurrency story must be revisited.
    doc = _load("wf-checks.yml")
    assert checks.workflow_triggers(doc) == ["workflow_call"]
