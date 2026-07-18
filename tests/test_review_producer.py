"""Unit tests for `shipit.review.producer` — the Tree-fetch review producer.

The producer replaces the retired front-loaded backends (ADR-0020 §Reviewer-path
reconciliation — REPLACE): it provisions a per-Run read-only Tree on the PR head,
launches codex / agy through their spawn read-only posture with a task that fetches
the diff itself, and CAPTURES the structured stdout. These tests pin the seam inputs
(agent → adapter mapping, the launch argv, the capture/parse, dry-run, preflight) with
the Tree clone + the model launch FAKED — no real Tree, no real model.
"""

from __future__ import annotations

import logging

import pytest

from shipit import execrun
from shipit.agent import backend as agent_backend
from shipit.identity import repo_from_slug
from shipit.review import producer
from shipit.review.backends import (
    BackendError,
    BackendUnavailable,
    parse_review_output,
)
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
            # A per-Run reviewer Tree is one flat leaf (ADR-0074): <repo>-<agent>-<ts>-<id>.
            path="/trees/shipit-codex-20260702-121314-abcd1234-abcd-1234-abcd-abcd1234abcd",
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
    captured = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), launcher=_faked["launcher"]
    )

    review = captured.review
    assert review["summary"]["status"] == "COMMENT"  # captured + parsed from stdout
    cmd = _faked["cmd"]
    # Launched as a codex reviewer (read-only posture), rooted in the read-only Tree.
    assert cmd[:2] == ["codex", "exec"]
    assert "workspace-write" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert _faked["cwd"].endswith(
        "shipit-codex-20260702-121314-abcd1234-abcd-1234-abcd-abcd1234abcd"
    )
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


def test_tree_review_logs_readonly_work_env_evidence(_faked, caplog):
    caplog.set_level(logging.INFO, logger="shipit.review")

    producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=_faked["launcher"])

    record = next(
        record
        for record in caplog.records
        if getattr(record, "work_env_boundary", None) == "review.readonly-run"
    )
    assert record.role == "reviewer"
    assert record.pr == 42
    assert record.reviewer == "codex"
    assert record.checkout_strategy == "per-run-read-only-tree"
    assert record.routing == "ambient"
    assert record.working_dir.endswith(
        "shipit-codex-20260702-121314-abcd1234-abcd-1234-abcd-abcd1234abcd"
    )
    assert record.working_dir_repo == "arthur-debert/shipit"
    assert record.working_dir_branch == "TRE05/WS04b"
    assert record.working_dir_commit == "deadbeef" * 5
    assert "environment_variables" not in record.__dict__
    assert "pixi_run_id" not in record.__dict__


def test_agy_maps_to_the_antigravity_adapter_with_prose_schema(_faked):
    captured = producer.run_tree_review(
        agent_backend.ANTIGRAVITY,
        _ctx(),
        model="pro",
        timeout="900s",
        launcher=_faked["launcher"],
    )

    assert captured.review["summary"]["status"] == "COMMENT"
    cmd = _faked["cmd"]
    assert cmd[0] == "agy"
    # agy is rooted via --add-dir <Tree> (it ignores process cwd) and carries the timeout.
    assert cmd[cmd.index("--add-dir") + 1].endswith(
        "shipit-codex-20260702-121314-abcd1234-abcd-1234-abcd-abcd1234abcd"
    )
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


# ---------------------------------------------------------------------------
# Part 5 (#826) — the deterministic ONE-shot re-prompt net for agy parse failures
# ---------------------------------------------------------------------------


def test_agy_reprompts_once_on_unparseable_output_then_parses_the_retry(_faked):
    # agy's FIRST response is unparseable; the producer re-prompts ONCE with the
    # specific parse failure appended and parses the valid SECOND response — the
    # deterministic fix even when the agent skipped its best-effort self-check.
    prompts: list[str] = []

    def launcher(cmd, *, cwd, env, timeout=None):
        prompts.append(cmd[-1])
        if len(prompts) == 1:
            return LaunchResult(
                returncode=0, stdout="prose, not json at all", stderr=""
            )
        return LaunchResult(returncode=0, stdout=_VALID, stderr="")

    captured = producer.run_tree_review(
        agent_backend.ANTIGRAVITY, _ctx(), launcher=launcher
    )
    assert captured.review["summary"]["status"] == "COMMENT"  # the RETRY parsed
    assert len(prompts) == 2  # original + exactly ONE retry
    retry = prompts[1]
    # The retry is the ORIGINAL task (still fetches the diff) PLUS a terminal block
    # quoting the SPECIFIC parse failure so agy fixes the concrete problem.
    assert "gh pr diff 42" in retry
    assert "RETRY — your PREVIOUS response could NOT be parsed" in retry
    # The actual failure hint fed back — since #1006 the diagnosis states what the
    # output WAS (a non-verdict) instead of the old catch-all size/latency guess.
    assert "no review verdict" in retry
    assert "try a faster model or a smaller diff" not in retry


def test_agy_retry_is_one_shot_two_failures_fall_through_to_salvage(_faked):
    # The retry is ONE shot, not a loop: two consecutive unparseable responses
    # exhaust it and the BackendError propagates (raw carried) so the service's #76
    # salvage stays the FINAL backstop AFTER the retry, never a hang.
    prompts: list[str] = []

    def launcher(cmd, *, cwd, env, timeout=None):
        prompts.append(cmd[-1])
        return LaunchResult(returncode=0, stdout="still not json", stderr="")

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(agent_backend.ANTIGRAVITY, _ctx(), launcher=launcher)
    assert len(prompts) == 2  # original + exactly ONE retry, then give up
    assert exc.value.raw == "still not json"  # the salvage still gets the raw
    assert exc.value.timed_out is False


def test_codex_never_reprompts_on_unparseable_output(_faked):
    # codex enforces the shape via `--output-schema`, so it does NOT opt into the
    # retry net: an unparseable codex output raises on the FIRST launch, no retry.
    prompts: list[str] = []

    def launcher(cmd, *, cwd, env, timeout=None):
        prompts.append(cmd[-1])
        return LaunchResult(returncode=0, stdout="prose, no json", stderr="")

    with pytest.raises(BackendError):
        producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=launcher)
    assert len(prompts) == 1  # codex is never re-prompted


def test_agy_timeout_is_not_reprompted(_faked):
    # A TIMEOUT is never retried — re-prompting a slow run would just burn a second
    # full deadline, and a timeout is not an off-shape body a re-prompt corrects.
    # The timeout BackendError propagates after exactly ONE launch.
    prompts: list[str] = []

    def launcher(cmd, *, cwd, env, timeout=None):
        prompts.append(cmd[-1])
        return LaunchResult(
            returncode=0,
            stdout="{ truncated… timed out waiting for response",
            stderr="",
        )

    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(agent_backend.ANTIGRAVITY, _ctx(), launcher=launcher)
    assert exc.value.timed_out is True
    assert len(prompts) == 1  # timeout -> no retry


def test_dry_run_prints_argv_and_never_launches_or_clones(monkeypatch, capsys):
    # No create_readonly / which fakes: dry-run must work without the CLI or a clone.
    cloned: list = []
    monkeypatch.setattr(producer, "create_readonly", lambda *a, **k: cloned.append(1))
    launched: list = []

    def launcher(cmd, *, cwd, env, timeout=None):
        launched.append(1)
        return LaunchResult(returncode=0, stdout=_VALID, stderr="")

    # Request a reasoning level: codex DOES carry a knob, so the adapter would
    # apply it to a REAL launch — but a dry run launches nothing, so the captured
    # result must still report reasoning unset (not the requested "low").
    captured = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), dry_run=True, launcher=launcher, reasoning="low"
    )

    assert captured.review["summary"]["overall_feedback"] == "(dry-run)"
    # A dry run bills no model, so it MEASURES no usage and applies no reasoning:
    # both are the explicit-unknown/unset state, never a fabricated/echoed value.
    assert captured.usage.total_tokens is None
    assert captured.reasoning is None
    assert not cloned  # no Tree cloned
    assert not launched  # no model billed
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "codex" in out and "exec" in out  # the would-run argv is shown
    # the requested level is not LOST — it rides the printed would-run argv,
    # which is why the captured (unlaunched) result need not echo it.
    assert "model_reasoning_effort=low" in out


def test_missing_cli_fails_loud(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: None)
    with pytest.raises(BackendUnavailable):
        producer.run_tree_review(
            agent_backend.CODEX, _ctx(), launcher=lambda *a, **k: None
        )


def test_preflight_round_names_each_missing_binary_in_one_error(monkeypatch):
    """RVW03-WS03: the round-level preflight raises ONE actionable
    BackendUnavailable naming every missing binary — the 'binary X not found —
    install/configure it' shape — instead of letting each pass discover the
    miss and report 'all N dimension passes failed'."""
    monkeypatch.setattr(
        producer.shutil,
        "which",
        lambda binary: None if binary in ("codex", "claude") else f"/usr/bin/{binary}",
    )
    with pytest.raises(BackendUnavailable) as exc:
        producer.preflight_round(
            [agent_backend.CODEX, agent_backend.CLAUDE, agent_backend.ANTIGRAVITY]
        )
    message = str(exc.value)
    assert "binary 'codex' not found — install/configure it" in message
    assert "binary 'claude' not found — install/configure it" in message
    assert "no passes were launched" in message
    assert "agy" not in message  # the present binary is not blamed


def test_preflight_round_checks_a_duplicate_binary_once(monkeypatch):
    """Two round entries sharing one binary (reviewer + calibrator on the same
    backend) are one check and, when missing, one blame line."""
    checked: list[str] = []

    def which(binary):
        checked.append(binary)
        return None

    monkeypatch.setattr(producer.shutil, "which", which)
    with pytest.raises(BackendUnavailable) as exc:
        producer.preflight_round([agent_backend.CODEX, agent_backend.CODEX])
    assert checked == ["codex"]
    assert str(exc.value).count("codex") == 2  # the binary + the backend name


def test_missing_head_branch_is_a_clean_failure(_faked):
    ctx = _ctx()
    ctx.head_ref = ""
    with pytest.raises(RuntimeError) as exc:
        producer.run_tree_review(agent_backend.CODEX, ctx, launcher=_faked["launcher"])
    assert "head branch" in str(exc.value)


def test_missing_head_branch_fails_with_a_preprovisioned_tree(_faked):
    ctx = _ctx()
    ctx.head_ref = ""
    with pytest.raises(RuntimeError, match="head branch"):
        producer.run_tree_review(
            agent_backend.CODEX,
            ctx,
            tree_path="/trees/already-provisioned",
            launcher=_faked["launcher"],
        )
    assert "cmd" not in _faked


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


def test_dimension_pass_reuses_the_handed_in_tree_and_scopes_the_prompt(
    _faked, monkeypatch
):
    # RVW02-WS04: a Dimension pass hands in the ALREADY-provisioned Tree (the
    # fan-out provisions once for all parallel passes) — the producer must NOT
    # re-provision — and the launched task carries the dimension focus slice.
    from shipit.review.dimensions import by_name

    def boom(plan, *, source_repo, github_url):
        raise AssertionError("tree_path was handed in; no re-provisioning")

    monkeypatch.setattr(producer, "create_readonly", boom)
    captured = producer.run_tree_review(
        agent_backend.CODEX,
        _ctx(),
        launcher=_faked["launcher"],
        dimension=by_name("security-robustness"),
        tree_path="/trees/shared/leaf",
    )
    assert captured.review["summary"]["status"] == "COMMENT"
    assert _faked["cwd"] == "/trees/shared/leaf"
    prompt = _faked["cmd"][-1]
    assert "DIMENSION FOCUS — Security / robustness" in prompt


def test_pass_task_text_matches_the_launched_prompt(_faked):
    # The variant source: `pass_task_text` re-derives the exact task the
    # launch composes (the adapter may prepend its role preamble around it),
    # so the round record's per-run variant hashes the prompt content that ran.
    from shipit.review.dimensions import by_name

    dim = by_name("correctness")
    producer.run_tree_review(
        agent_backend.CODEX, _ctx(), launcher=_faked["launcher"], dimension=dim
    )
    task = producer.pass_task_text(agent_backend.CODEX, 42, dimension=dim)
    assert task in _faked["cmd"][-1]


def _range_view():
    from shipit.identity import Sha
    from shipit.review.diff import RangeView

    return RangeView(
        repo=repo_from_slug("acme/widget"),
        base_sha=Sha("a" * 40),
        head_sha=Sha("b" * 40),
        diff="diff --git a/x b/x\n",
        changed_files=["x"],
        workdir="/checkout",
    )


def test_range_dimension_pass_runs_offline_with_the_range_scoped_focus(_faked):
    # RVW03-WS01: the offline fan-out replay narrows the RANGE task to one
    # dimension exactly like the PR task — same focus slice — launched in the
    # replay checkout with NO Tree and NO gh. Scope rides the shared
    # `_scope_and_context` baseline over the range's own `git diff` fetch
    # (ADR-0050), carrying the RANGE diff noun, never a `gh pr diff`.
    from shipit.review.dimensions import by_name

    view = _range_view()
    captured = producer.run_range_review(
        agent_backend.CODEX,
        view,
        launcher=_faked["launcher"],
        dimension=by_name("correctness"),
    )
    assert captured.review["summary"]["status"] == "COMMENT"
    assert _faked["cwd"] == "/checkout"
    prompt = _faked["cmd"][-1]
    assert f"git diff {'a' * 40}..{'b' * 40}" in prompt
    assert "DIMENSION FOCUS — Correctness" in prompt
    # The shared scope baseline reaches the range pass and names the range's diff.
    assert "report ONLY findings this range's diff INTRODUCED or EXPOSED" in prompt
    assert "this PR's diff" not in prompt
    assert "gh pr diff" not in prompt


def test_range_pass_task_text_matches_the_launched_prompt(_faked):
    # The offline fan-out's variant source: `range_pass_task_text` re-derives
    # the exact task `run_range_review` composes, so a replayed pass's
    # `round.runs` variant hashes the prompt content that actually ran.
    from shipit.review.dimensions import by_name

    dim = by_name("test-quality")
    view = _range_view()
    producer.run_range_review(
        agent_backend.CODEX, view, launcher=_faked["launcher"], dimension=dim
    )
    task = producer.range_pass_task_text(agent_backend.CODEX, view, dimension=dim)
    assert task in _faked["cmd"][-1]


def test_range_pass_task_text_rejects_a_non_funnel_backend():
    with pytest.raises(ValueError, match="unknown funnel review backend"):
        producer.range_pass_task_text(agent_backend.CLAUDE, _range_view())


def test_incremental_range_launches_the_fix_range_task(_faked):
    # RVW02-WS06: an incremental round launches the fix-range task (git diff
    # base..head, NOT `gh pr diff`) with mandated neighborhood context, and
    # `pass_task_text` re-derives the SAME prompt for the round-record variant.
    captured = producer.run_tree_review(
        agent_backend.CODEX,
        _ctx(),
        launcher=_faked["launcher"],
        incremental_range=("b" * 40, "c" * 40),
        tree_path="/trees/shared/leaf",
    )
    assert captured.review["summary"]["status"] == "COMMENT"
    prompt = _faked["cmd"][-1]
    assert f"git diff {'b' * 40}..{'c' * 40}" in prompt
    assert "MANDATORY CONTEXT EXPANSION" in prompt
    task = producer.pass_task_text(
        agent_backend.CODEX, 42, incremental_range=("b" * 40, "c" * 40)
    )
    assert task in prompt


def test_incremental_range_and_dimension_are_mutually_exclusive(_faked):
    # RVW02-WS06: an incremental round is ONE full-scope fix-range pass, not a
    # dimension pass — supplying both is a caller programming error that BOTH the
    # variant-source helper and the launch path reject with ValueError, rather
    # than silently letting incremental_range win and hashing/launching a task
    # shape the caller did not mean.
    from shipit.review.dimensions import by_name

    dim = by_name("correctness")
    with pytest.raises(ValueError, match="mutually exclusive"):
        producer.pass_task_text(
            agent_backend.CODEX,
            42,
            dimension=dim,
            incremental_range=("b" * 40, "c" * 40),
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        producer.run_tree_review(
            agent_backend.CODEX,
            _ctx(),
            launcher=_faked["launcher"],
            dimension=dim,
            incremental_range=("b" * 40, "c" * 40),
        )


def test_provision_review_tree_requires_a_head_branch(monkeypatch):
    import pytest as _pytest

    ctx = review_view(
        number=42,
        repo="arthur-debert/shipit",
        head_sha="deadbeef" * 5,
        base_ref="TRE05/umbrella",
        base_sha="cafe" * 10,
        diff="diff --git a/x b/x\n",
        is_draft=False,
        changed_files=["x"],
        workdir="/checkout",
        head_ref="",
    )
    with _pytest.raises(RuntimeError, match="head branch"):
        producer.provision_review_tree(ctx, agent_backend.CODEX)


def test_codex_usage_is_captured_from_the_stderr_tokens_line(_faked):
    # RVW03-WS04 (#667): codex 0.139 reports its token total on STDERR as a
    # human log line ("tokens used" + a comma-grouped figure, probed). The
    # capture must read it at launch-result level — no transcript join.
    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(
            returncode=0,
            stdout=_VALID,
            stderr="OpenAI Codex v0.139.0\ncodex\nOK\ntokens used\n11,943\n",
        )

    captured = producer.run_tree_review(agent_backend.CODEX, _ctx(), launcher=launcher)
    assert captured.usage.total_tokens == 11943
    assert captured.usage.reported is True


def test_codex_usage_without_the_tokens_line_reads_unreported_not_zero(_faked):
    # A CLI formatting drift must degrade to the HONEST unknown, never a zero.
    captured = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), launcher=_faked["launcher"]
    )
    assert captured.usage.total_tokens is None
    assert captured.usage.reported is False


def test_agy_usage_is_explicitly_unreported(_faked):
    # agy 1.1.1 reports NO usage anywhere (probed) — the record must say so
    # explicitly (total None) rather than fabricating a number.
    captured = producer.run_tree_review(
        agent_backend.ANTIGRAVITY, _ctx(), launcher=_faked["launcher"]
    )
    assert captured.usage.total_tokens is None
    assert captured.usage.as_record()["source"] == "unreported"


def test_reasoning_reaches_codex_argv_and_the_capture_reports_it(_faked):
    # RVW03-WS04 (#685): a requested ReasoningLevel must land in REAL argv where
    # the CLI has a knob — codex's `-c model_reasoning_effort=<level>` — and the
    # capture reports the level actually applied (what records stamp).
    captured = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), launcher=_faked["launcher"], reasoning="low"
    )
    cmd = _faked["cmd"]
    assert "model_reasoning_effort=low" in cmd
    assert cmd[cmd.index("model_reasoning_effort=low") - 1] == "-c"
    assert captured.reasoning == "low"


def test_reasoning_unset_leaves_codex_argv_bare_and_reports_none(_faked):
    captured = producer.run_tree_review(
        agent_backend.CODEX, _ctx(), launcher=_faked["launcher"]
    )
    assert not any("model_reasoning_effort" in arg for arg in _faked["cmd"])
    assert captured.reasoning is None


def test_reasoning_is_dropped_for_agy_and_never_echoed(_faked):
    # agy has NO reasoning knob (probed 1.1.1): the requested level must NOT
    # ride its argv, and the capture must report None — the record then reads
    # "unset" instead of echoing a config value that never ran (#685).
    captured = producer.run_tree_review(
        agent_backend.ANTIGRAVITY, _ctx(), launcher=_faked["launcher"], reasoning="low"
    )
    assert not any("reasoning" in arg or "effort" in arg for arg in _faked["cmd"])
    assert captured.reasoning is None


# ---------------------------------------------------------------------------
# RVW03-WS02 — the launch seam fills the per-run artifact bundle, every path
# ---------------------------------------------------------------------------


def _bundle(tmp_path):
    from shipit.review.artifacts import RunArtifacts

    return RunArtifacts(tmp_path / "bundle")


def test_success_launch_fills_the_bundle(_faked, tmp_path):
    import json

    bundle = _bundle(tmp_path)
    producer.run_tree_review(
        agent_backend.CODEX,
        _ctx(),
        launcher=_faked["launcher"],
        run_id="run-1",
        artifacts=bundle,
    )
    # The EXACT prompt the launch composed — the same bytes pass_task_text derives.
    expected_task = producer.pass_task_text(agent_backend.CODEX, _ctx().number)
    assert (bundle.dir / "prompt.txt").read_text() == expected_task
    assert (bundle.dir / "stdout.raw").read_text() == _VALID
    assert (bundle.dir / "stderr.raw").read_text() == ""
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is False
    assert meta["argv"] == _faked["cmd"]
    assert "duration_ms" in meta


def test_nonzero_exit_bundle_keeps_full_raw_and_logs_point_at_it(
    _faked, tmp_path, caplog
):
    import json
    import logging

    long_err = "x" * 2000  # far past the 500-char message truncation

    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=1, stdout="partial out", stderr=long_err)

    bundle = _bundle(tmp_path)
    caplog.set_level(logging.WARNING, logger="shipit.review")
    with pytest.raises(RuntimeError) as exc:
        producer.run_tree_review(
            agent_backend.CODEX,
            _ctx(),
            launcher=launcher,
            artifacts=bundle,
            run_id="run-x",
        )
    # The absolute bundle path is kept OUT of the raised message — that message
    # crosses into the GitHub-facing funnel check summary and must not leak a
    # user-home / state path. The LOCAL log points a developer at the full raw.
    assert str(bundle.dir) not in str(exc.value)
    assert str(bundle.dir) in caplog.text
    # The breadcrumb carries correlation extras so `shipit logs --run/--reviewer`
    # selects the very line that says where the raw output lives.
    [breadcrumb] = [r for r in caplog.records if "full raw output at" in r.getMessage()]
    assert breadcrumb.run_id == "run-x"
    assert breadcrumb.reviewer == "codex"
    # The bundle carries the UNtruncated streams + the exit meta.
    assert (bundle.dir / "stderr.raw").read_text() == long_err
    assert (bundle.dir / "stdout.raw").read_text() == "partial out"
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["exit_code"] == 1


def test_seam_timeout_bundle_keeps_partial_streams_and_timed_out_meta(_faked, tmp_path):
    import json

    def launcher(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(
            cmd,
            rc=None,
            stdout="partial body",
            stderr="killed at deadline",
            cause=execrun.CAUSE_TIMEOUT,
        )

    bundle = _bundle(tmp_path)
    with pytest.raises(BackendError):
        producer.run_tree_review(
            agent_backend.CODEX, _ctx(), launcher=launcher, artifacts=bundle
        )
    assert (bundle.dir / "stdout.raw").read_text() == "partial body"
    assert (bundle.dir / "stderr.raw").read_text() == "killed at deadline"
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["timed_out"] is True
    assert meta["exit_code"] is None
    # The prompt was written BEFORE the launch, so a killed child leaves it.
    assert (bundle.dir / "prompt.txt").exists()


def test_exit_zero_timeout_marker_corrects_the_bundle_timed_out_meta(_faked, tmp_path):
    import json

    from shipit.review.backends.base import _TIMEOUT_MARKER

    # Exit 0, but the stdout is unparseable AND carries the timeout marker:
    # parse_review_output raises BackendError(timed_out=True). The launch seam
    # optimistically recorded timed_out=False before the parse — the meta must be
    # CORRECTED to True so the bundle never claims a real timeout was a clean run.
    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout=f"...{_TIMEOUT_MARKER}...", stderr="")

    bundle = _bundle(tmp_path)
    with pytest.raises(BackendError) as exc:
        producer.run_tree_review(
            agent_backend.CODEX, _ctx(), launcher=launcher, artifacts=bundle
        )
    assert exc.value.timed_out is True
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is True


def test_range_review_fills_the_bundle_too(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def launcher(cmd, *, cwd, env, timeout=None):
        return LaunchResult(returncode=0, stdout=_VALID, stderr="")

    view = SimpleNamespace(workdir=str(tmp_path), base_sha="a" * 40, head_sha="b" * 40)
    bundle = _bundle(tmp_path)
    producer.run_range_review(
        agent_backend.CODEX, view, launcher=launcher, run_id="r", artifacts=bundle
    )
    assert (bundle.dir / "prompt.txt").read_text()
    assert (bundle.dir / "stdout.raw").read_text() == _VALID
    # Range and Tree passes share `_launch_and_capture`, so the correlation meta
    # must land on this path too — exit code and the caller's run id serialized.
    meta = json.loads((bundle.dir / "meta.json").read_text())
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is False


# ---------------------------------------------------------------------------
# Issue #1006 — parse-failure diagnosis is evidence-based and conservative.
# Every case crosses `parse_review_output` (the diagnosis is an implementation
# detail behind it): only an EXPLICIT timeout marker recommends a faster model
# or a smaller diff; every other non-delivery states what the output was
# without guessing a cause.
# ---------------------------------------------------------------------------


_SIZE_HINT = "try a faster model or a smaller diff"


def test_an_explicit_timeout_keeps_the_size_hint_and_the_structured_flag():
    # The backend's OWN marker is the one piece of evidence that the response was
    # cut off mid-flight — size/latency IS the lever, and the advice stays.
    raw = '{"summary": {"status": "COMM… timed out waiting for response'
    with pytest.raises(BackendError) as exc:
        parse_review_output(raw, backend_name="agy")
    assert "timed out" in str(exc.value)
    assert _SIZE_HINT in str(exc.value)
    assert exc.value.timed_out is True
    assert exc.value.raw == raw  # the #76 salvage still gets everything


def test_empty_stdout_is_diagnosed_as_silent_with_no_size_hint():
    # Nothing was delivered at all — a killed child or a failed login, never a
    # diff-size problem. The raw (empty) is still preserved for the salvage path.
    with pytest.raises(BackendError) as exc:
        parse_review_output("   \n", backend_name="agy")
    assert "no output at all" in str(exc.value)
    assert _SIZE_HINT not in str(exc.value)
    assert exc.value.timed_out is False
    assert exc.value.raw == "   \n"


def test_narration_with_ordinary_braces_is_a_non_verdict_not_a_size_problem():
    # The #998 signature: the agent narrated its diff-hunting in prose and never
    # answered. Braces in prose/command snippets are not evidence of anything —
    # blaming a cut-off (i.e. diff size) here is the misdiagnosis #1006 removes.
    for narration in (
        "I will search the workspace for a diff to review.",
        "no json here, just {braces} and prose",
        "I will run `git diff --name-only` and inspect {workspace}/docs to review.",
    ):
        with pytest.raises(BackendError) as exc:
            parse_review_output(narration, backend_name="agy")
        assert "no review verdict" in str(exc.value), narration
        assert _SIZE_HINT not in str(exc.value), narration
        assert exc.value.timed_out is False


def test_truncated_tool_json_with_nested_envelope_keys_is_a_plain_non_verdict():
    # A truncated TOOL-CALL object can carry "comments"/"summary" nested in its
    # arguments payload. Without an explicit timeout that proves nothing about a
    # cut-off — the diagnosis stays a conservative non-verdict, no size advice.
    for raw in (
        '{"tool": "post_review", "arguments": {"comments": [{"file": "a.py",',
        '{"tool": "x", "arguments": {"summary": {"status": "COMM',
    ):
        with pytest.raises(BackendError) as exc:
            parse_review_output(raw, backend_name="agy")
        assert "no review verdict" in str(exc.value), raw
        assert _SIZE_HINT not in str(exc.value), raw


def test_complete_wrong_shape_json_is_an_output_contract_fault_not_size():
    # The #826 signature: valid, COMPLETE JSON with the wrong envelope. The output
    # terminated on its own, so the lever is the output contract — never diff size.
    with pytest.raises(BackendError) as exc:
        parse_review_output(
            '{"findings": [{"file": "a.py", "text": "x"}]}', backend_name="codex"
        )
    assert "complete JSON that is not a review" in str(exc.value)
    assert "shipit review validate" in str(exc.value)
    assert _SIZE_HINT not in str(exc.value)


def test_partial_review_shaped_json_without_a_timeout_stays_a_non_verdict():
    # A prefix that LOOKS like the envelope being written is still not evidence of
    # a cut-off (an agentic run can quote schema JSON and stop for its own
    # reasons): without the explicit timeout marker the diagnosis is the
    # conservative non-verdict, and the size advice is withheld.
    with pytest.raises(BackendError) as exc:
        parse_review_output('{"summary": {"status": "COMM', backend_name="agy")
    assert "no review verdict" in str(exc.value)
    assert _SIZE_HINT not in str(exc.value)
    assert exc.value.timed_out is False


def test_a_valid_embedded_review_still_parses_unchanged():
    # The diagnosis is strictly failure-path: a review embedded in noisy stdout
    # parses exactly as before.
    noisy = f"agent chatter before\n{_VALID}\ntrailing chatter"
    review = parse_review_output(noisy, backend_name="agy")
    assert review["summary"]["status"] == "COMMENT"
