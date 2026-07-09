"""Smoke tests for the `shipit pr review request` CLI surface — glue + renderers.

The reviewer-request service (attach-verify, bare-run skip) is the engine's
(`shipit.prstate.request`, unit-tested in test_prstate_request.py); these prove
the verb WIRING: scope selection through the registry, resolve → request →
render through the seam, and the failure paths (unknown reviewer, no PR, gh
failure, dropped edge) surfacing as the one uniform ``error: …`` + exit 1 via
the shared shell. The engine itself is NOT re-tested here.
"""

from __future__ import annotations

import pytest

from shipit import cli
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.request import RequestResult, ReviewerOutcome
from shipit.prstate.roster import Roster
from shipit.verbs.pr import review as review_verb

# The typed PR target (CLI01-WS02 / ADR-0030): the verb threads a PrId — repo +
# number as ONE value — never a bare int.
REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=7)


# --- the local-agent scope via the CLI verb -----------------------------------


def test_local_agent_request_detaches_in_flight(monkeypatch, capsys):
    """OBS03: requesting a local-agent reviewer DETACHES the review (force scope) —
    the verb reports it in-flight and exits 0, without blocking on a model run. The
    service detach boundary is faked so nothing forks."""
    from shipit.review import service

    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    detached: list = []
    monkeypatch.setattr(
        service,
        "start_detached_review",
        lambda backend, pr, **kw: detached.append((backend.funnel_agent, pr)) or True,
    )
    rc = review_verb.run(7, reviewer="codex", repo=REPO)
    assert rc == 0
    # The detached-review entry received the TYPED target (ADR-0030).
    assert detached == [("codex", TARGET)]
    assert "review in flight: codex on #7" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["codex-local", "agy-local"])
def test_local_agent_spec_alias_detaches(monkeypatch, capsys, name):
    """The PRD/glossary spell these `codex-local`/`agy-local`; the `-local` alias
    resolves the base adapter so the local review detaches, not an unknown-name
    error."""
    from shipit.review import service

    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    monkeypatch.setattr(
        service, "start_detached_review", lambda backend, pr, **kw: True
    )
    rc = review_verb.run(7, reviewer=name, repo=REPO)
    assert rc == 0
    out = capsys.readouterr().out
    assert "review in flight:" in out


def test_local_alias_does_not_match_app_reviewer(capsys):
    """`-local` aliases only the local-agent family — `copilot-local` is unknown
    (an app reviewer has a requested edge and is not a local backend)."""
    rc = review_verb.run(7, reviewer="copilot-local", repo=REPO)
    assert rc == 1
    assert "unknown reviewer" in capsys.readouterr().err


# --- CLI verb wiring + behavior ----------------------------------------------


def test_unknown_reviewer_is_rejected(capsys):
    """A typo'd --reviewer name fails loud (the registry's domain refusal through
    the shell: `error: …` + exit 1) listing the known names."""
    rc = review_verb.run(7, reviewer="copliot", repo=REPO)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "unknown reviewer" in err
    assert "copilot" in err  # the known-names list


def test_no_pr_for_branch_is_fatal(monkeypatch, capsys):
    """A mutating verb treats a branch with no PR as fatal (non-zero) — the
    per-verb refusal wording survives as the exception message (ADR-0030)."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = review_verb.run(None, reviewer="copilot", repo=REPO)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no PR" in err


def test_gh_failure_resolving_is_fatal(monkeypatch, capsys):
    """A real gh/auth failure resolving the branch's PR -> clean stderr + non-zero."""

    def boom(pr, repo, branch):
        raise ExecError(["gh"], rc=1, stderr="gh auth exploded")

    monkeypatch.setattr(review_verb, "resolve_pr", boom)
    rc = review_verb.run(None, reviewer="copilot", repo=REPO)
    assert rc == 1
    assert "gh auth exploded" in capsys.readouterr().err


def test_verb_renders_verified(monkeypatch, capsys):
    """The bare-run happy path: resolve -> request_reviewers -> render verified."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    monkeypatch.setattr(
        review_verb,
        "request_reviewers",
        lambda pr, adapters, roster, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "verified")]
        ),
    )
    monkeypatch.setattr(review_verb, "required_adapters", lambda roster: [object()])
    monkeypatch.setattr(review_verb, "load_roster", lambda: Roster())
    rc = review_verb.run(7, repo=REPO)
    assert rc == 0
    assert "verified: copilot" in capsys.readouterr().out


def test_dropped_request_exits_nonzero(monkeypatch, capsys):
    """A dropped remote request -> the uniform `error: …` stderr + non-zero exit
    (never a silent park); the outcome block still names it on stdout."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo, branch: TARGET)
    monkeypatch.setattr(
        review_verb,
        "request_reviewers",
        lambda pr, adapters, roster, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "dropped")]
        ),
    )
    monkeypatch.setattr(review_verb, "required_adapters", lambda roster: [object()])
    monkeypatch.setattr(review_verb, "load_roster", lambda: Roster())
    rc = review_verb.run(7, repo=REPO)
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "dropped by GitHub" in captured.err
    assert "copilot" in captured.err


def test_format_request_renders_each_outcome_and_the_all_skipped_note():
    """The pure renderer (the render seam): one line per outcome; a bare run that
    skipped everyone says so explicitly rather than rendering silence."""
    result = RequestResult(
        outcomes=[
            ReviewerOutcome("copilot", "verified"),
            ReviewerOutcome("codex", "in_flight"),
            ReviewerOutcome("gemini", "no_op"),
        ]
    )
    out = review_verb.format_request(7, result)
    assert "verified: copilot request attached on #7" in out
    assert "review in flight: codex on #7" in out
    assert "gemini: auto-triggers, no request mechanism — no-op" in out

    all_skipped = RequestResult(outcomes=[ReviewerOutcome("copilot", "skipped")])
    out = review_verb.format_request(7, all_skipped)
    assert "copilot: already reviewed #7 (review-once) — skip" in out
    assert "nothing to request" in out


# --- group/command registration smoke ----------------------------------------


def test_pr_review_subgroup_registered(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "review" in capsys.readouterr().out


def test_pr_review_lists_request(capsys):
    rc = cli.main(["pr", "review", "--help"])
    assert rc == 0
    assert "request" in capsys.readouterr().out


def test_pr_review_request_help(capsys):
    rc = cli.main(["pr", "review", "request", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--reviewer" in out


def test_pr_review_run_is_hidden(capsys):
    """The detached child entry `_run` is internal — it never shows in `--help`."""
    rc = cli.main(["pr", "review", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "request" in out
    assert "_run" not in out


def test_pr_review_run_invokes_detached_child(monkeypatch):
    """OBS03: the hidden `_run` command is the detached child entry — it parses its
    args and drives `service.run_detached_review` with the parent's `run_id`."""
    from shipit.review import service

    captured: dict = {}

    def fake_child(backend, pr, **kw):
        captured.update({"backend": backend, "pr": pr, **kw})
        return {}

    monkeypatch.setattr(service, "run_detached_review", fake_child)
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "owner/repo",
            "--run-id",
            "555",
        ]
    )
    assert rc == 0
    # The child boundary resolved `--agent codex` back to the ONE registry identity.
    from shipit.agent import backend as agent_backend

    assert captured["backend"] is agent_backend.CODEX
    # The child's own entry point minted the PrId at the process boundary
    # (explicit --repo/--pr — it never reads the root context).
    assert captured["pr"] == PrId(repo=repo_from_slug("owner/repo"), number=5)
    assert "repo" not in captured  # the repo rides ON the target now
    assert captured["run_id"] == 555
    assert captured["as_app"] is True


def test_pr_review_run_reconstructs_the_fanout_config(monkeypatch):
    """RVW02-WS04: the child entry parses `--dimensions` (comma-joined), the
    `--nit-cap`, and the four `--calibrator-*` fields into the typed values the
    service consumes — a config re-read never happens in the child."""
    from shipit.review import service

    captured: dict = {}

    def fake_child(backend, pr, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(service, "run_detached_review", fake_child)
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "owner/repo",
            "--dimensions",
            "correctness,test-quality",
            "--nit-cap",
            "0",
            "--calibrator-backend",
            "claude",
            "--calibrator-reasoning",
            "medium",
        ]
    )
    assert rc == 0
    assert captured["dimensions"] == ("correctness", "test-quality")
    assert captured["nit_cap"] == 0
    calibrator = captured["calibrator"]
    assert calibrator.backend == "claude"
    assert calibrator.reasoning == "medium"
    assert calibrator.timeout == "600s"  # unset fields keep the shipped defaults


def test_pr_review_run_defaults_leave_the_fanout_config_unset(monkeypatch):
    from shipit.review import service

    captured: dict = {}
    monkeypatch.setattr(
        service, "run_detached_review", lambda backend, pr, **kw: captured.update(kw)
    )
    rc = cli.main(
        ["pr", "review", "_run", "--agent", "codex", "--pr", "5", "--repo", "o/r"]
    )
    assert rc == 0
    assert captured["dimensions"] is None
    assert captured["calibrator"] is None
    assert captured["nit_cap"] is None


def test_pr_review_run_rejects_a_bad_calibrator_cleanly(monkeypatch, capsys):
    """A malformed --calibrator-* field dies at the process boundary as one
    clean line — before any model run bills."""
    from shipit.review import service

    ran: list = []
    monkeypatch.setattr(service, "run_detached_review", lambda *a, **k: ran.append(1))
    rc = cli.main(
        [
            "pr",
            "review",
            "_run",
            "--agent",
            "codex",
            "--pr",
            "5",
            "--repo",
            "o/r",
            "--calibrator-backend",
            "gpt-cli",
        ]
    )
    assert rc == 1
    assert "calibrator" in capsys.readouterr().err
    assert ran == []
