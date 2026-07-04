"""Unit tests for `shipit.review.producer` — the Tree-fetch review producer.

The producer replaces the retired front-loaded backends (ADR-0020 §Reviewer-path
reconciliation — REPLACE): it provisions a shared read-only Tree on the PR head,
launches codex / agy through their spawn read-only posture with a task that fetches
the diff itself, and CAPTURES the structured stdout. These tests pin the seam inputs
(agent → adapter mapping, the launch argv, the capture/parse, dry-run, preflight) with
the Tree clone + the model launch FAKED — no real Tree, no real model.
"""

from __future__ import annotations

import pytest

from shipit import execrun
from shipit.agent import backend as agent_backend
from shipit.identity import repo_from_slug
from shipit.review import producer
from shipit.review.backends import BackendError, BackendUnavailable
from shipit.review.diff import ReviewView, review_view
from shipit.spawn.launch import LaunchResult
from shipit.tree.create import Tree

_VALID = '{"summary": {"status": "COMMENT", "overall_feedback": "ok"}, "comments": []}'


def _ctx() -> ReviewView:
    return review_view(
        number=42,
        repo="arthur-debert/shipit",
        head_sha="deadbeef" * 5,  # a full 40-hex sha (COR02)
        base_ref="TRE05/umbrella",
        base_sha="cafe" * 10,  # a full 40-hex sha (PROC03)
        diff="diff --git a/x b/x\n",
        is_draft=False,
        changed_files=["x"],
        workdir="/checkout",
        head_ref="TRE05/WS04b",
    )


@pytest.fixture
def _faked(monkeypatch):
    """Fake the Tree clone, the remote-url read, and the PATH preflight so a launch
    exercises ONLY the producer wiring. Returns a dict the test fills with the captured
    launch argv/cwd/env."""
    monkeypatch.setattr(
        producer,
        "create_readonly",
        lambda plan, *, source_repo, github_url: Tree(
            path="/trees/arthur-debert/shipit/review/tre05-ws04b-abcd1234",
            branch=plan.branch,
            base=f"origin/{plan.branch}",
        ),
    )
    monkeypatch.setattr(producer.git, "remote_url", lambda *, cwd: "https://x/y.git")
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    captured: dict = {}

    def launcher(cmd, *, cwd, env, timeout=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        captured["timeout"] = timeout
        return LaunchResult(returncode=0, stdout=_VALID, stderr="")

    captured["launcher"] = launcher
    return captured


def test_codex_launches_in_the_tree_and_captures_the_review(_faked):
    review = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), launcher=_faked["launcher"]
    )

    assert review["summary"]["status"] == "COMMENT"  # captured + parsed from stdout
    cmd = _faked["cmd"]
    # Launched as a codex reviewer (read-only posture), rooted in the read-only Tree.
    assert cmd[:2] == ["codex", "exec"]
    assert "workspace-write" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert _faked["cwd"].endswith("tre05-ws04b-abcd1234")
    # codex gets the native schema flag (a real temp file path was written + passed).
    assert "--output-schema" in cmd
    # The task tells the agent to fetch the diff itself for THIS pr and not to post.
    prompt = cmd[-1]
    assert "gh pr diff 42" in prompt
    assert "do not run" in prompt.lower()
    # #404: codex has NO native timeout flag, so the launch seam carries the deadline —
    # the default `600s` reaches the runner as a bare 600.0s process deadline (no
    # headroom: the seam IS codex's sole enforcement).
    assert _faked["timeout"] == 600.0


def test_agy_maps_to_the_antigravity_adapter_with_prose_schema(_faked):
    review = producer.run_tree_review(
        agent_backend.ANTIGRAVITY,
        _ctx(),
        model="pro",
        timeout="900s",
        launcher=_faked["launcher"],
    )

    assert review["summary"]["status"] == "COMMENT"
    cmd = _faked["cmd"]
    assert cmd[0] == "agy"
    # agy is rooted via --add-dir <Tree> (it ignores process cwd) and carries the timeout.
    assert cmd[cmd.index("--add-dir") + 1].endswith("tre05-ws04b-abcd1234")
    assert "--print-timeout=900s" in cmd
    # No native schema flag for agy; the schema rides the prompt prose instead.
    assert "--output-schema" not in cmd
    assert "JSON Schema:" in cmd[-1]
    # Reviewer posture: agy omits the write Run's --dangerously-skip-permissions.
    assert "--dangerously-skip-permissions" not in cmd
    # #404: agy enforces `--print-timeout` ITSELF and its native timeout yields a
    # SALVAGEABLE truncated review, so the launch-seam deadline is set with HEADROOM
    # (900s + 60s) over the native flag — the native path wins the race and the seam
    # is a pure backstop that only bites if agy hangs past its own deadline.
    assert _faked["timeout"] == 900.0 + producer._SEAM_HEADROOM_SECONDS


def test_nonzero_exit_is_a_hard_failure(_faked):
    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=1, stdout="", stderr="codex: auth error")

    with pytest.raises(RuntimeError) as exc:
        producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=launcher)
    # Not a BackendError (which would settle empty/timed_out) — a plain failure → failed.
    assert not isinstance(exc.value, BackendError)
    assert "auth error" in str(exc.value)


def test_agy_timeout_marker_settles_as_timeout_not_failure(_faked):
    def launcher(cmd, *, cwd, env, timeout=None):
        # agy prints the marker (exit 0 in practice); a parse over it raises a
        # timeout-flavoured BackendError that the service maps to timed_out.
        return LaunchResult(
            returncode=0,
            stdout="{ truncated… timed out waiting for response",
            stderr="",
        )

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(agent_backend.ANTIGRAVITY, _ctx(), launcher=launcher)
    assert "timed out" in str(exc.value).lower()


def test_nonzero_exit_with_timeout_marker_in_stderr_is_structurally_timed_out(_faked):
    # Regression (Copilot #194): a real timeout can exit NONZERO with the marker in
    # *stderr*, not stdout. The human-facing message paraphrases the timeout and does
    # NOT echo `_TIMEOUT_MARKER`, so a string-match on the message would misclassify it
    # as `empty`. The producer must instead set the STRUCTURED `timed_out` flag so the
    # service settles `timed_out`. (Before the fix, `_capture` raised with the flag
    # auto-derived False -> the funnel closed `neutral` instead of `timed_out`.)
    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(
            returncode=1,
            stdout="",
            stderr="agy: timed out waiting for response",
        )

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(agent_backend.ANTIGRAVITY, _ctx(), launcher=launcher)
    assert exc.value.timed_out is True  # structured -> service maps to timed_out
    # the marker is NOT in the (paraphrased) message — a string match would have failed:
    from shipit.review.backends.base import _TIMEOUT_MARKER

    assert _TIMEOUT_MARKER not in str(exc.value).lower()


def test_seam_timeout_becomes_a_timed_out_backend_error_with_raw_salvage(_faked):
    # #404: codex has no native timeout flag, so the LAUNCH SEAM kills the stalled
    # child at the deadline — `execrun.run` raises ExecError(cause=CAUSE_TIMEOUT)
    # carrying the partial streams even under check=False. The producer must convert
    # THAT into BackendError(timed_out=True) so the service settles `timed_out`
    # (degraded, non-blocking, ADR-0006), NOT the generic `failed`.
    def launcher(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(
            cmd,
            rc=None,
            stdout="partial review body the child wrote before it hung",
            stderr="killed at deadline",
            cause=execrun.CAUSE_TIMEOUT,
        )

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=launcher)
    assert exc.value.timed_out is True  # structured -> service maps to timed_out
    assert "timed out" in str(exc.value).lower()
    # The message reports the ACTUAL seam deadline the backstop fired at (codex has no
    # native timeout, so no headroom: the default 600s IS the kill deadline).
    assert "600s" in str(exc.value)
    # The partial stdout+stderr rides `raw` so the #76 salvage can still surface it.
    assert "partial review body" in exc.value.raw
    assert "killed at deadline" in exc.value.raw


def test_seam_timeout_message_reports_agys_headroom_deadline_not_the_bare_timeout(
    _faked,
):
    # For a NATIVE-timeout backend (agy) the seam sits ABOVE the native flag by
    # `_SEAM_HEADROOM_SECONDS`, so the backstop fires at `timeout + headroom`, NOT the
    # bare configured `--timeout`. The message must name that actual kill deadline so a
    # debugger isn't misled about when/why the seam backstop triggered.
    def launcher(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(
            cmd,
            rc=None,
            stdout="partial",
            stderr="killed at deadline",
            cause=execrun.CAUSE_TIMEOUT,
        )

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(
            agent_backend.ANTIGRAVITY, _ctx(), model="pro", launcher=launcher
        )
    # Default 600s + 60s headroom = 660s actual seam kill, while the configured
    # `--timeout` string (600s) is still surfaced for context.
    assert "660s" in str(exc.value)
    assert "600s" in str(exc.value)


def test_non_timeout_launch_execerror_propagates_as_a_plain_failure(_faked):
    # A NON-timeout transport failure (a missing binary that slipped past preflight,
    # a vanished cwd) must NOT be reclassed as a timeout — it propagates as the raw
    # ExecError for the service's generic `failed` mapping, never a BackendError.
    def launcher(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(
            cmd, rc=None, stderr="No such file", cause=execrun.CAUSE_MISSING_BINARY
        )

    with pytest.raises(execrun.ExecError) as exc:
        producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=launcher)
    assert exc.value.cause == execrun.CAUSE_MISSING_BINARY
    assert not isinstance(exc.value, BackendError)


def test_unparseable_output_raises_backend_error_with_raw_for_salvage(_faked):
    raw = "here is some prose but no json at all"

    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout=raw, stderr="")

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=launcher)
    assert exc.value.raw == raw  # the #76 salvage still gets the raw content
    # A non-timeout unparseable result is NOT a timeout -> the service settles `empty`.
    assert exc.value.timed_out is False


def test_dry_run_prints_argv_and_never_launches_or_clones(monkeypatch, capsys):
    # No create_readonly / which fakes: dry-run must work without the CLI or a clone.
    cloned: list = []
    monkeypatch.setattr(producer, "create_readonly", lambda *a, **k: cloned.append(1))
    launched: list = []

    def launcher(cmd, *, cwd, env, timeout=None):
        launched.append(1)
        return LaunchResult(returncode=0, stdout=_VALID, stderr="")

    review = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), dry_run=True, launcher=launcher
    )

    assert review["summary"]["overall_feedback"] == "(dry-run)"
    assert not cloned  # no Tree cloned
    assert not launched  # no model billed
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "codex" in out and "exec" in out  # the would-run argv is shown


def test_missing_cli_fails_loud(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: None)
    with pytest.raises(BackendUnavailable):
        producer.run_tree_review(
            agent_backend.CODEX, _ctx(), launcher=lambda *a, **k: None
        )


def test_missing_head_branch_is_a_clean_failure(_faked):
    ctx = _ctx()
    ctx.head_ref = ""
    with pytest.raises(RuntimeError) as exc:
        producer.run_tree_review(agent_backend.CODEX, ctx, launcher=_faked["launcher"])
    assert "head branch" in str(exc.value)


def test_resolve_repo_uses_the_view_slug_when_known(monkeypatch):
    """A resolved view's slug is the source of truth for the read-only Tree's
    identity — no `gh repo view` re-inference — parsed by the ONE canonical
    parser, so it lands the case-normalized Repo."""
    monkeypatch.setattr(
        producer.gh,
        "current_repo",
        lambda: (_ for _ in ()).throw(AssertionError("must not infer when repo known")),
    )
    assert producer._resolve_repo(_ctx()) == repo_from_slug("arthur-debert/shipit")


def test_resolve_repo_falls_back_to_gh_for_handbuilt_context(monkeypatch):
    """The falsey-repo fallback (ADR-0024): a hand-built view (`repo is None`)
    provisions the Tree under the `gh repo view`-inferred identity rather than a
    `local/local` placeholder."""
    ctx = review_view(
        number=42,
        repo=None,
        head_sha="deadbeef" * 5,  # a full 40-hex sha (COR02)
        base_ref="main",
        base_sha="cafe" * 10,  # a full 40-hex sha (PROC03)
        diff="",
        is_draft=False,
    )
    assert ctx.repo is None
    monkeypatch.setattr(
        producer.gh, "current_repo", lambda: repo_from_slug("inferred/repo")
    )
    assert producer._resolve_repo(ctx) == repo_from_slug("inferred/repo")


def test_resolve_repo_error_names_gh_view_for_the_empty_slug_fallback(monkeypatch):
    """A `ValueError` from the empty-slug `gh repo view` fallback blames
    `gh repo view` (not the empty slug) and surfaces the underlying message, so
    the malformed CLI output is debuggable from the top-line error (agy review)."""
    ctx = review_view(
        number=42,
        repo=None,
        head_sha="deadbeef" * 5,
        base_ref="main",
        base_sha="cafe" * 10,  # a full 40-hex sha (PROC03)
        diff="",
        is_draft=False,
    )
    monkeypatch.setattr(
        producer.gh,
        "current_repo",
        lambda: (_ for _ in ()).throw(ValueError("gh emitted 'not-a-slug'")),
    )
    with pytest.raises(RuntimeError) as exc:
        producer._resolve_repo(ctx)
    message = str(exc.value)
    assert "`gh repo view`" in message
    assert "gh emitted 'not-a-slug'" in message


def test_launch_specs_are_keyed_by_the_backend_value_not_a_retyped_name():
    """The launch-spec table is keyed by the registry :class:`Backend` VALUE OBJECTS,
    not by a retyped canonical-name string (COR02-WS03 / codex review). Renaming a
    backend is then a single registry edit — the key follows the constant's identity —
    and the launch axis covers EXACTLY the funnel backends the registry declares, so a
    newly registered funnel backend without a launch spec is caught here rather than
    failing at run time with `unknown funnel review backend`."""
    assert all(isinstance(k, agent_backend.Backend) for k in producer._SPECS)
    assert set(producer._SPECS) == set(agent_backend.funnel_backends())
