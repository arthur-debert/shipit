"""Smoke tests for the `shipit pr status` CLI surface.

These prove the WIRING (group + verb registered, JSON field set, text render,
error -> non-zero exit) — NOT the engine's state logic, which is unit-tested
directly in the prstate suite. The boundary (`gather` / `evaluate` / the PR
resolver) is monkeypatched so there is no network.
"""

from __future__ import annotations

import json

import pytest

from shipit import cli
from shipit.prstate import ghapi
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import status as status_verb

# The exact JSON field set `pr status --json` must emit.
EXPECTED_JSON_FIELDS = {
    "pr",
    "state",
    "next_action",
    "reviewers",
    "open_threads",
    "checks",
    "mergeable",
    "cycles",
    "breaker",
    "reviewer_funnel",  # OBS04-WS01: structured per-reviewer funnel data
}


def _fake_status(pr: int) -> TaskStatus:
    return TaskStatus(
        state=TaskState.READY,
        next_action="run `pr ready` to flip draft->ready",
        pr=pr,
        reviewers={"copilot": "done_clean"},
        open_threads=0,
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
        cycles=1,
        breaker=None,
    )


@pytest.fixture
def patched(monkeypatch):
    """Stub the boundary: resolver -> PR 42 (or the explicit arg), gather carries
    the PR through, evaluate builds the status off it. No network."""
    monkeypatch.setattr(
        status_verb, "resolve_pr", lambda pr: pr if pr is not None else 42
    )
    monkeypatch.setattr(status_verb, "gather", lambda pr: pr)
    monkeypatch.setattr(
        status_verb, "evaluate", lambda ctx, required: _fake_status(ctx)
    )
    monkeypatch.setattr(status_verb, "required_reviewers", lambda: [])


def test_pr_group_registered(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "pr" in capsys.readouterr().out


def test_pr_help_lists_status(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "status" in capsys.readouterr().out


def test_status_help(capsys):
    rc = cli.main(["pr", "status", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--json" in out
    assert "next action" in out.lower()


def test_status_json_emits_exact_field_set(patched, capsys):
    rc = cli.main(["pr", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == EXPECTED_JSON_FIELDS
    assert payload["state"] == "ready"
    assert payload["pr"] == 42


def test_status_text_renders_state_and_next_action(patched, capsys):
    rc = cli.main(["pr", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ready" in out
    assert "run `pr ready`" in out


def test_status_explicit_pr_argument(patched, capsys):
    """An explicit numeric PR argument flows through to the resolver/JSON."""
    rc = cli.main(["pr", "status", "7", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["pr"] == 7


def test_no_pr_is_normal_exit_zero(monkeypatch, capsys):
    """A branch with no PR is a normal state (exit 0), not an error."""
    monkeypatch.setattr(status_verb, "resolve_pr", lambda pr: None)
    rc = cli.main(["pr", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "no_pr"
    assert payload["pr"] is None


def test_gh_failure_on_known_pr_exits_nonzero(monkeypatch, capsys):
    """A gh/auth failure while reading a KNOWN PR surfaces as stderr + non-zero."""
    monkeypatch.setattr(status_verb, "resolve_pr", lambda pr: 42)

    def boom(pr):
        raise ghapi.GhError("gh exploded")

    monkeypatch.setattr(status_verb, "gather", boom)
    rc = cli.main(["pr", "status"])
    assert rc != 0
    assert "gh exploded" in capsys.readouterr().err


def test_gh_failure_during_resolution_is_fatal(monkeypatch, capsys):
    """A REAL gh/auth failure resolving the branch's PR is fatal — NOT a silent
    no_pr. The resolver returns None for the genuine "no PR for branch" case, so a
    GhError reaching the verb is always a real failure (PRD: stderr + non-zero)."""

    def boom(pr):
        raise ghapi.GhError("gh auth exploded")

    monkeypatch.setattr(status_verb, "resolve_pr", boom)
    rc = cli.main(["pr", "status"])
    assert rc != 0
    assert "gh auth exploded" in capsys.readouterr().err


# --- the shared resolver: no-PR vs real failure discrimination ----------------

from shipit.verbs.pr._resolve import resolve_pr  # noqa: E402


def test_resolver_explicit_pr_passthrough():
    assert resolve_pr(7) == 7


def test_resolver_no_pr_marker_maps_to_none(monkeypatch):
    """gh's "no pull requests found for branch" exit is a normal no-PR state -> None."""

    def fake_gh(args, **kw):
        raise ghapi.GhError(
            'gh pr view failed (1): no pull requests found for branch "x"'
        )

    monkeypatch.setattr(ghapi, "_gh", fake_gh)
    assert resolve_pr(None) is None


def test_resolver_real_gh_error_propagates(monkeypatch):
    """Any other gh failure stays a GhError — never collapsed into None."""

    def fake_gh(args, **kw):
        raise ghapi.GhError("gh pr view failed (1): could not authenticate")

    monkeypatch.setattr(ghapi, "_gh", fake_gh)
    with pytest.raises(ghapi.GhError):
        resolve_pr(None)


def test_resolver_parses_number(monkeypatch):
    monkeypatch.setattr(ghapi, "_gh", lambda args, **kw: '{"number": 99}')
    assert resolve_pr(None) == 99
