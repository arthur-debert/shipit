"""Unit tests for the backend-agnostic launcher (:mod:`shipit.spawn.launch`).

The per-backend argv/auth-env/read-only-posture moved behind the ADR-0020 adapter
seam (covered in ``test_spawn_backend_claude.py``); what stays here is the *shared*
launch machinery: ``cwd`` = the Tree, the Exec-runner consumer view (PROC01-WS04:
the real runner is one Exec through :func:`shipit.execrun.run`, with the launch
contract's semantics pinned as explicit parameters), and the English PR-contract
prompts. These tests pin each piece WITHOUT spawning a real child — the prompts
directly, and the Exec seam by faking ``execrun.run`` / injecting a fake runner.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from shipit import execrun, workenv
from shipit.identity import repo_from_slug
from shipit.spawn import launch


def test_write_task_forbids_ending_the_turn_with_background_work_in_flight():
    # #663: a headless Run that ends its turn EXITS — its background children die
    # with it (interactive sessions get re-invoked when harness-tracked background
    # work completes; a headless Run does not). RVW02-WS05's first Run launched
    # its long pipelines as background tasks, ended its turn to "wait", and the
    # exit silently killed 21 minutes of billed work. The task prompt must carry
    # the backend-neutral rule for EVERY spawned write Run: long work runs in the
    # foreground (or is synchronously awaited), and the turn never ends while
    # background work is in flight. All the halves are load-bearing: the headless
    # framing, the turn-end-is-exit equivalence, the kill consequence, and the
    # foreground mandate.
    task = launch.write_task(
        "implementer",
        issue=663,
        branch="issues/663/work",
        base_branch="main",
        closes=True,
    )
    assert "headless" in task.lower()
    assert "ending your turn exits" in task.lower()
    assert "background" in task.lower()
    assert "killed" in task.lower()
    assert "foreground" in task.lower()


def test_write_task_background_rule_is_shape_independent():
    # The #663 rule guards the RUN's lifecycle, not the write shape — the epic
    # work-stream shape must carry it identically to the standalone-issue shape.
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main", closes=False
    )
    assert "ending your turn exits" in task.lower()
    assert "foreground" in task.lower()


def test_reviewer_task_names_the_branch_and_posts_a_review():
    task = launch.reviewer_task("TRE03/WS03")
    assert "TRE03/WS03" in task
    assert "gh pr review" in task
    # The read-only posture is stated: no edits/build/push/merge.
    assert "READ-ONLY" in task


def test_reviewer_task_reads_the_diff_with_gh_pr_diff_not_a_hardcoded_base():
    # The diff instruction must use `gh pr diff` (the PR's actual base/head), NOT a baked
    # `git diff origin/main...HEAD` — an epic/umbrella PR has a non-main base, so a
    # hardcoded base would compute the wrong range.
    task = launch.reviewer_task("TRE03/WS03")
    assert "gh pr diff" in task
    assert "origin/main" not in task


def test_launch_routes_through_the_injected_runner():
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["env"] = env
        seen["timeout"] = timeout
        return launch.LaunchResult(returncode=0, stdout="{}", stderr="")

    result = launch.launch(
        ["claude", "-p", "t"],
        cwd="/trees/x",
        env={"PATH": "/bin"},
        runner=fake_runner,
    )

    assert result.returncode == 0
    assert seen["cmd"] == ["claude", "-p", "t"]
    assert seen["cwd"] == "/trees/x"  # rooting is the OS process cwd = the Tree
    assert seen["env"] == {"PATH": "/bin"}
    # With no explicit timeout the seam is UNBOUNDED (LAUNCH_TIMEOUT) — the
    # write/spawn-Run posture: no bound may kill a long implementer Run.
    assert seen["timeout"] is launch.LAUNCH_TIMEOUT


def test_launch_threads_an_explicit_timeout_to_the_runner():
    # The review producer passes the reviewer's --timeout as a real process deadline
    # (#404); the launcher must SEE it so the seam can kill a stalled backend.
    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["timeout"] = timeout
        return launch.LaunchResult(0, "{}", "")

    launch.launch(["codex"], cwd="/trees/x", env={}, timeout=600.0, runner=fake_runner)

    assert seen["timeout"] == 600.0


def test_launch_stringifies_a_path_cwd():
    from pathlib import Path

    seen: dict = {}

    def fake_runner(cmd, *, cwd, env, timeout=None):
        seen["cwd"] = cwd
        return launch.LaunchResult(0, "", "")

    launch.launch([], cwd=Path("/trees/y"), env={}, runner=fake_runner)

    assert seen["cwd"] == "/trees/y"
    assert isinstance(seen["cwd"], str)


def test_exec_runner_is_a_consumer_view_over_the_exec_runner(monkeypatch):
    # PROC01-WS04: the real launch seam is ONE Exec through execrun.run (ADR-0028),
    # with every launch-contract semantic pinned as an explicit parameter.
    captured: dict = {}

    def fake_exec_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="out", stderr="err", duration_ms=12
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    result = launch._exec_runner(
        ["claude", "-p", "t"], cwd="/trees/x", env={"PATH": "/bin"}
    )

    assert result == launch.LaunchResult(returncode=0, stdout="out", stderr="err")
    assert captured["argv"] == ["claude", "-p", "t"]
    kwargs = captured["kwargs"]
    # Rooted in the Tree, with the (already-scrubbed) env REPLACING the child's
    # environment — a scrubbed key must not creep back in via the runner's merge.
    assert kwargs["cwd"] == "/trees/x"
    assert kwargs["env"] == {"PATH": "/bin"}
    assert kwargs["replace_env"] is True
    # A nonzero child is a result, not an exception (ADR-0019 §6).
    assert kwargs["check"] is False
    # The EXPLICIT timeout override: no default (5m or otherwise) may kill a Run.
    assert "timeout" in kwargs
    assert kwargs["timeout"] is None
    assert launch.LAUNCH_TIMEOUT is None


def test_exec_runner_passes_an_explicit_deadline_to_the_exec_runner(monkeypatch):
    # #404: the review producer's deadline must reach `execrun.run` as the Exec's
    # timeout, so a stalled review backend is actually killed at the seam.
    captured: dict = {}

    def fake_exec_run(argv, **kwargs):
        captured["kwargs"] = kwargs
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="out", stderr="", duration_ms=1
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    launch._exec_runner(["codex"], cwd="/x", env={}, timeout=600.0)

    assert captured["kwargs"]["timeout"] == 600.0


def test_exec_runner_returns_nonzero_without_raising(monkeypatch):
    # check=False rides the Exec: a nonzero agent child comes back as a
    # LaunchResult the verb reports — never an ExecError (ADR-0019/0020).
    def fake_exec_run(argv, **kwargs):
        return execrun.ExecResult(
            argv=tuple(argv), rc=7, stdout="", stderr="boom", duration_ms=3
        )

    monkeypatch.setattr(launch.execrun, "run", fake_exec_run)

    result = launch._exec_runner(["claude"], cwd="/x", env={})

    assert result.returncode == 7
    assert result.stderr == "boom"


def test_exec_runner_normalizes_a_missing_binary_into_execerror(tmp_path):
    # The transport itself failing (backend binary not on PATH) IS an ExecError —
    # the runner normalizes the raw FileNotFoundError (ADR-0028), and the spawn
    # verb maps it to a clean exit-1. Driven against the REAL runner: the exec
    # never happens, so this is fast and hermetic.
    with pytest.raises(execrun.ExecError) as excinfo:
        launch._exec_runner(
            ["definitely-not-a-real-backend-binary"],
            cwd=str(tmp_path),
            env={"PATH": str(tmp_path)},
        )

    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_exec_runner_emits_the_exec_record_with_duration(monkeypatch, caplog):
    # Acceptance (PROC01-WS04): a launch Exec appears in the structured record with
    # duration like any other Exec. Fake the subprocess layer INSIDE execrun so the
    # real runner produces its one record for the launch.
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(execrun.subprocess, "run", fake_run)

    with caplog.at_level("DEBUG", logger="shipit.exec"):
        launch._exec_runner(["claude", "-p", "t"], cwd="/trees/x", env={})

    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1  # exactly one record per Exec
    message = records[0].getMessage()
    assert "claude -p '<redacted: prompt sha256=" in message
    assert " -p t" not in message
    assert "cwd=/trees/x" in message
    assert "rc=0" in message
    assert "ms" in message  # the duration rides the record


def test_pixi_wrap_routes_through_pixi_for_a_provisioned_tree(tmp_path):
    # A WRITE Tree is `pixi install`-provisioned → it carries `.pixi/envs/default`, so the
    # backend argv is re-expressed to run THROUGH the Tree's pixi env (ADR-0019 amendment):
    # `pixi run --manifest-path <tree>/pixi.toml -- <argv>`. The `--` separates pixi's args
    # from the child argv, and the manifest path is the Tree's own pixi.toml.
    (tmp_path / ".pixi" / "envs" / "default").mkdir(parents=True)
    argv = ["claude", "-p", "do the thing", "--agent", "implementer"]

    wrapped = launch.pixi_wrap(argv, tmp_path)

    assert wrapped == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "--",
        "claude",
        "-p",
        "do the thing",
        "--agent",
        "implementer",
    ]


def test_pixi_wrap_stays_bare_for_an_unprovisioned_tree(tmp_path):
    # A reviewer's READ-ONLY Tree (ADR-0018, clone+checkout, no provision) and a non-pixi
    # repo carry no `.pixi/envs/default`, so the argv is returned UNCHANGED — routing those
    # through `pixi run` would force a solve into a chmod'd tree or fail outright.
    argv = ["claude", "-p", "review", "--agent", "reviewer"]

    assert launch.pixi_wrap(argv, tmp_path) == argv


def test_pixi_wrap_accepts_a_str_tree_path(tmp_path):
    # The call site passes `tree.path` (a str); the gate probe must work on a str too.
    (tmp_path / ".pixi" / "envs" / "default").mkdir(parents=True)

    wrapped = launch.pixi_wrap(["claude"], str(tmp_path))

    assert wrapped[:4] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
    ]


def _write_env(*, pixi_provisioned: bool):
    """A resolved write-Run Work Env over a NONEXISTENT path — the point: routing
    consumes the carried decision and never re-probes the filesystem."""
    return workenv.resolve_write_run_env(
        repo=repo_from_slug("acme/widget"),
        tree_path="/nonexistent/trees/acme/widget/E/WS01-abc123",
        branch="E/WS01",
        base="origin/E/umbrella",
        pixi_provisioned=pixi_provisioned,
    )


def test_route_argv_carries_out_the_pixi_run_decision():
    # RPE01-WS05: the Work Env CARRIES the routing decision the spawn boundary
    # made; route_argv only executes it — through the pixi adapter's builder
    # (ADR-0028), yielding EXACTLY the wrapped argv pixi_wrap produces for a
    # provisioned tree. The path deliberately does not exist: no re-probe.
    argv = ["claude", "-p", "do the thing", "--agent", "implementer"]

    routed = launch.route_argv(argv, _write_env(pixi_provisioned=True))

    assert routed == [
        "pixi",
        "run",
        "--manifest-path",
        "/nonexistent/trees/acme/widget/E/WS01-abc123/pixi.toml",
        "--",
        *argv,
    ]


def test_route_argv_leaves_an_ambient_work_env_bare():
    # A non-pixi write Run resolves AMBIENT (absent activation, honestly) and
    # keeps the existing bare-launch behavior — argv unchanged, same object
    # semantics as pixi_wrap's unprovisioned branch.
    argv = ["claude", "-p", "do the thing"]

    assert launch.route_argv(argv, _write_env(pixi_provisioned=False)) == argv


def test_route_argv_refuses_an_activation_snapshot_context():
    # This consumer does not apply activation snapshots. Treating one as ambient
    # would silently launch with the wrong tools, so misuse fails at the seam.
    env = replace(
        _write_env(pixi_provisioned=False),
        routing=workenv.ExecutionRouting.ACTIVATION_SNAPSHOT,
    )

    with pytest.raises(ValueError, match="activation-snapshot"):
        launch.route_argv(["claude"], env)


def test_route_argv_records_its_routing_decision_at_debug(caplog):
    # The routing narration matches pixi_wrap's (ADR-0029 mechanics at DEBUG,
    # `pixi_wrapped` as the greppable extra) so a mis-rooted child diagnoses
    # identically whichever seam routed it.
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        launch.route_argv(["claude"], _write_env(pixi_provisioned=True))
        launch.route_argv(["claude"], _write_env(pixi_provisioned=False))

    decisions = [r.pixi_wrapped for r in caplog.records if hasattr(r, "pixi_wrapped")]
    assert decisions == [True, False]


def test_scrub_tree_env_drops_leaked_pixi_and_conda_activation_keeps_the_rest():
    # On top of the adapter's auth scrub, the launch path drops parent-project PIXI_*
    # pointers and Conda ACTIVATION vars (the #167 leak class) so the child re-resolves
    # from the Tree — but installation-level CONDA_* (CONDA_EXE / CONDA_PYTHON_EXE) is
    # KEPT, since scrubbing all CONDA_* could break `pixi run` in a Conda-managed shell.
    env = {
        "HOME": "/home/a",
        "PATH": "/bin",
        "PIXI_PROJECT_MANIFEST": "/parent/pixi.toml",
        "PIXI_PROJECT_NAME": "parent",
        "CONDA_PREFIX": "/parent/.pixi/envs/default",
        "CONDA_PREFIX_1": "/parent/.pixi/envs/stacked",
        "CONDA_DEFAULT_ENV": "default",
        "CONDA_SHLVL": "2",
        "CONDA_PROMPT_MODIFIER": "(default) ",
        "CONDA_EXE": "/opt/conda/bin/conda",
        "CONDA_PYTHON_EXE": "/opt/conda/bin/python",
    }

    scrubbed = launch.scrub_tree_env(env)

    assert scrubbed == {
        "HOME": "/home/a",
        "PATH": "/bin",
        "CONDA_EXE": "/opt/conda/bin/conda",
        "CONDA_PYTHON_EXE": "/opt/conda/bin/python",
    }


def test_scrub_tree_env_drops_leaked_build_env_keeps_sccache_backend_vars():
    # agy ERROR: the launch env feeds a child that runs `cargo` via the Tree's own pixi
    # activation (pixi_wrap → `pixi run`). A leaked PARENT CARGO_TARGET_DIR / SCCACHE_BASEDIRS
    # would shadow the Tree's own `[activation.env]` value, so the child writes artifacts to
    # the PARENT Tree. Scrub the three per-Tree build vars; KEEP the sccache binary pointer
    # and cache credential (not per-Tree; the child needs them to reach the shared cache).
    env = {
        "PATH": "/bin",
        "CARGO_TARGET_DIR": "/parent/tree/target",
        "SCCACHE_BASEDIRS": "/parent/tree",
        "CARGO_INCREMENTAL": "0",
        "RUSTC_WRAPPER": "/usr/bin/sccache",
        "SCCACHE_GCS_KEY": "creds",
    }

    scrubbed = launch.scrub_tree_env(env)

    assert scrubbed == {
        "PATH": "/bin",
        "RUSTC_WRAPPER": "/usr/bin/sccache",
        "SCCACHE_GCS_KEY": "creds",
    }


def test_scrub_tree_env_keeps_pixi_cache_vars():
    # The cache-location vars are user-level (not project-bound), so they are KEPT to
    # preserve cross-Tree package-cache sharing — the same carve-out provisioning uses
    # (reused via `is_leaked_env_var`, so the two paths cannot drift).
    env = {"PIXI_CACHE_DIR": "/cache/pixi", "RATTLER_CACHE_DIR": "/cache/rattler"}

    assert launch.scrub_tree_env(env) == env


def test_scrub_tree_env_returns_a_fresh_dict():
    env = {"PATH": "/bin"}
    assert launch.scrub_tree_env(env) is not env


def test_write_task_names_the_role_issue_and_branch():
    task = launch.write_task(
        "implementer", issue=156, branch="TRE03/WS02", base_branch="main", closes=False
    )
    # The Run learns its role, which issue to implement, and the exact branch its
    # draft PR must come from (the head shipit later resolves the PR↔Run link by).
    assert "implementer" in task
    assert "#156" in task
    assert "TRE03/WS02" in task
    assert "main" in task


def test_write_task_instructs_a_draft_pr_and_to_stop():
    # WS02 (acceptance #156): the Run reports back through a DRAFT PR and stops after
    # one `shipit pr next` run — never flips ready or merges. All are load-bearing.
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main", closes=False
    )
    assert "draft" in task.lower()
    assert "for #42" in task
    assert "stop" in task.lower()
    # RVW01 (#383): the role contract has the Run place the initial review requests
    # via ONE engine run after PR-open, so the task must mandate that single
    # `shipit pr next` run — and must NOT forbid requesting reviews.
    assert "shipit pr next" in task
    assert "request reviews" not in task.lower()
    # ... while the review ROUNDS stay out of the Run's slice: the prohibition on
    # addressing them is load-bearing text, pinned so an edit can't drop it silently.
    assert "address review rounds" in task


def test_write_task_links_closes_for_the_standalone_issue_shape():
    # #649: a standalone-issue Run's merged PR must AUTO-CLOSE its issue, so the
    # task mandates the GitHub closing keyword `closes #N` — and never the
    # non-closing `for #N`, which GitHub ignores and which stranded merged
    # standalone issues open.
    task = launch.write_task(
        "implementer",
        issue=649,
        branch="issues/649/work",
        base_branch="main",
        closes=True,
    )
    assert "closes #649" in task
    assert "for #649" not in task


def test_write_task_links_for_on_the_epic_work_stream_shape():
    # #649 counterpart: an epic work-stream Run's PR link is DELIBERATELY
    # non-closing (`for #N`) — the WS issue must stay open until the umbrella PR
    # integrates and closes the epic's issues — so `closes #N` must never appear.
    task = launch.write_task(
        "implementer", issue=42, branch="X/WS01", base_branch="main", closes=False
    )
    assert "for #42" in task
    assert "closes #42" not in task


def test_write_task_carries_the_bank_state_protocol():
    # #587: a Run that nears its wall-clock/budget before the draft PR is open must
    # BANK its state — commit whatever exists with a `WIP:`-marked message and push
    # the branch — so the failed spawn is a resumable handoff, not a silent loss of
    # the Run's work. All three halves are load-bearing: the marker (the coordinator
    # greps for it), the exact branch, and the PUSH (an unpushed commit dies with
    # the Tree just like uncommitted work).
    task = launch.write_task(
        "implementer",
        issue=587,
        branch="issues/587/work",
        base_branch="main",
        closes=True,
    )
    assert "`WIP:`" in task
    assert "bank your state" in task.lower()
    assert "push the branch" in task
    assert "'issues/587/work'" in task  # the WIP lands on the Run's OWN branch
    # The push MUST be spelled upstream-safe: at bank time the branch is fresh (no
    # draft PR ⇒ no upstream), so a bare `git push` would reject the WIP commit and
    # lose exactly the work the protocol salvages. Pin the `-u origin <branch>` form.
    assert "git push -u origin issues/587/work" in task
