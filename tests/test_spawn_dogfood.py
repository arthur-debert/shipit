"""Tests for `shipit.spawn.dogfood` — the live `shipit spawn` verification harness.

The harness itself drives LIVE tooling (real `claude` Runs, real PRs, real git) and
is never run by these checks. These tests cover its two halves with every live seam
FAKED:

  - the **pure assertions** (`assert_dissociated_clone`, `assert_under_central_root`,
    `assert_distinct_from_scratch`, `assert_readonly_worktree`) run against planted
    fixtures — a real `.git` dir vs a `.git` file, a planted `objects/info/alternates`,
    a `.claude`-containing path, chmod'd working files — so each invariant passes/fails
    correctly; and
  - the **orchestration** (`verify_write_run` / `verify_reviewer_run` /
    `verify_fail_closed` / `verify`) runs with `_run_spawn`, `_current_branch`,
    `_pixi_runs`, `_scratch_dirty`, `_open_pr_heads`, `_pr_reviews` monkeypatched — so
    the wiring is proven without spawning `claude` or hitting GitHub.

This is the regression net that keeps the opt-in harness from silently rotting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipit import execrun, pixienv
from shipit.identity import repo_from_slug
from shipit.spawn import dogfood
from shipit.tree import readonly

# --------------------------------------------------------------------------
# Fixtures: plant a Tree-shaped directory on disk.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_tmp_path_perms(tmp_path):
    """Some tests chmod a planted Tree read-only (directories included, mirroring
    :func:`shipit.tree.readonly.chmod_readonly`). Restore write bits after the test so
    pytest's ``tmp_path`` teardown can remove read-only dirs on every platform."""
    yield
    for p in (tmp_path, *tmp_path.rglob("*")):
        try:
            p.chmod(p.stat().st_mode | 0o222)
        except OSError:
            pass


def _make_clone(root: Path, *, dissociated: bool = True) -> Path:
    """A Tree-shaped dir: `.git` is a DIRECTORY (a clone), optionally dissociated.

    One flat leaf (ADR-0074): <repo>-<agent>-<timestamp>-<id>, one segment below the
    central root — no owner/kind/epics path segment."""
    tree = root / "widget-claude-20260717-081333-deadbeefdead"
    (tree / ".git" / "objects" / "info").mkdir(parents=True)
    (tree / "file.txt").write_text("hi")
    if not dissociated:
        (tree / ".git" / "objects" / "info" / "alternates").write_text(
            "/some/objects\n"
        )
    return tree


def _central_root(root: Path) -> str:
    return str(root)


# --------------------------------------------------------------------------
# Pure assertion: dissociated clone (invariant 2).
# --------------------------------------------------------------------------


def test_dissociated_clone_passes_for_a_real_dissociated_clone(tmp_path):
    tree = _make_clone(tmp_path, dissociated=True)
    report = dogfood.Report()
    dogfood.assert_dissociated_clone(report, str(tree), label="t")
    assert report.passed
    names = " | ".join(c.name for c in report.checks)
    assert ".git is a directory" in names
    assert "dissociated" in names


def test_dissociated_clone_fails_when_git_is_a_file_worktree(tmp_path):
    """A native worktree's `.git` is a FILE (a gitfile pointer), not a dir → FAIL."""
    tree = tmp_path / "widget-claude-20260717-081333-deadbeefdead"
    tree.mkdir(parents=True)
    (tree / ".git").write_text("gitdir: /parent/.git/worktrees/x\n")
    report = dogfood.Report()
    dogfood.assert_dissociated_clone(report, str(tree), label="t")
    assert not report.passed
    git_check = next(c for c in report.checks if "directory" in c.name)
    assert not git_check.passed
    assert "file" in git_check.detail


def test_dissociated_clone_fails_with_planted_alternates(tmp_path):
    """A clone that kept `objects/info/alternates` is NOT dissociated → FAIL."""
    tree = _make_clone(tmp_path, dissociated=False)
    report = dogfood.Report()
    dogfood.assert_dissociated_clone(report, str(tree), label="t")
    assert not report.passed
    alt = next(c for c in report.checks if "dissociated" in c.name)
    assert not alt.passed
    assert "present" in alt.detail


def test_dissociated_clone_fails_when_path_absent(tmp_path):
    report = dogfood.Report()
    dogfood.assert_dissociated_clone(report, str(tmp_path / "nope"), label="t")
    assert not report.passed
    assert any("present on disk" in c.name and not c.passed for c in report.checks)


def test_dissociated_clone_fails_when_path_is_none():
    report = dogfood.Report()
    dogfood.assert_dissociated_clone(report, None, label="t")
    assert not report.passed


# --------------------------------------------------------------------------
# Pure assertion: under central root, not in .claude (invariant 3).
# --------------------------------------------------------------------------


def test_under_central_root_passes(tmp_path):
    tree = _make_clone(tmp_path)
    report = dogfood.Report()
    dogfood.assert_under_central_root(
        report, str(tree), _central_root(tmp_path), label="t"
    )
    assert report.passed


def test_under_central_root_fails_when_outside_root(tmp_path):
    tree = _make_clone(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    report = dogfood.Report()
    dogfood.assert_under_central_root(report, str(tree), str(other), label="t")
    under = next(c for c in report.checks if "under the central root" in c.name)
    assert not under.passed


def test_under_central_root_fails_when_inside_dotclaude(tmp_path):
    """A Tree path with a `.claude` component fails the no-dotclaude sub-check."""
    tree = tmp_path / ".claude" / "worktrees" / "agent-x"
    (tree / ".git").mkdir(parents=True)
    report = dogfood.Report()
    dogfood.assert_under_central_root(report, str(tree), str(tmp_path), label="t")
    dotclaude = next(c for c in report.checks if ".claude" in c.name)
    assert not dotclaude.passed


# --------------------------------------------------------------------------
# Pure assertion: distinct from scratch (invariant 1, per-Tree half).
# --------------------------------------------------------------------------


def test_distinct_from_scratch_passes(tmp_path):
    tree = _make_clone(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    report = dogfood.Report()
    dogfood.assert_distinct_from_scratch(report, str(tree), str(scratch), label="t")
    assert report.passed


def test_distinct_from_scratch_fails_when_nested_in_scratch(tmp_path):
    scratch = tmp_path / "scratch"
    tree = scratch / "nested" / "tree"
    (tree / ".git").mkdir(parents=True)
    report = dogfood.Report()
    dogfood.assert_distinct_from_scratch(report, str(tree), str(scratch), label="t")
    assert not report.passed


# --------------------------------------------------------------------------
# Pure assertion: read-only worktree (ADR-0018).
# --------------------------------------------------------------------------


def test_readonly_worktree_passes_when_tree_is_readonly(tmp_path):
    tree = _make_clone(tmp_path)
    # The real guardrail clears write bits on every working dir AND file (keeping
    # .git writable) — use it so the fixture matches what a live Tree looks like.
    readonly.chmod_readonly(str(tree))
    report = dogfood.Report()
    dogfood.assert_readonly_worktree(report, str(tree), label="t")
    assert report.passed, [c for c in report.checks if not c.passed]
    names = " | ".join(c.name for c in report.checks)
    assert "refuses a new file" in names


def test_readonly_worktree_fails_when_a_file_is_writable(tmp_path):
    tree = _make_clone(tmp_path)  # file.txt is left writable by default
    report = dogfood.Report()
    dogfood.assert_readonly_worktree(report, str(tree), label="t")
    no_writable = next(c for c in report.checks if "no writable working file" in c.name)
    assert not no_writable.passed
    assert "file.txt" in no_writable.detail


def test_readonly_worktree_fails_when_a_directory_is_writable(tmp_path):
    """A Tree whose FILES are read-only but whose DIRECTORIES keep their write bits is
    still mutable — a reviewer could create files in any writable dir — so the
    read-only check (and the active write probe) must FAIL."""
    tree = _make_clone(tmp_path)
    # Clear write bits on the files only; leave the directories writable.
    for p in tree.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            p.chmod(p.stat().st_mode & ~0o222)
    report = dogfood.Report()
    dogfood.assert_readonly_worktree(report, str(tree), label="t")
    no_writable = next(
        c for c in report.checks if "no writable working file or directory" in c.name
    )
    assert not no_writable.passed
    probe = next(c for c in report.checks if "refuses a new file" in c.name)
    assert not probe.passed


# --------------------------------------------------------------------------
# parse_spawned
# --------------------------------------------------------------------------


def test_parse_spawned_extracts_the_json_block():
    stdout = 'SPAWNED\n{\n  "tree": "/t",\n  "branch": "E/WS05",\n  "pr": 12\n}\n'
    payload = dogfood.parse_spawned(stdout)
    assert payload == {"tree": "/t", "branch": "E/WS05", "pr": 12}


def test_parse_spawned_returns_none_without_a_spawned_line():
    assert dogfood.parse_spawned("spawn subagent: tree creation failed\n") is None


def test_parse_spawned_tolerates_leading_noise():
    stdout = 'warming up...\nSPAWNED\n{"tree": "/t", "branch": "E/WS05"}\n'
    assert dogfood.parse_spawned(stdout) == {"tree": "/t", "branch": "E/WS05"}


# --------------------------------------------------------------------------
# The live seams — through the Exec runner (faked here)
# --------------------------------------------------------------------------


def _exec_result(rc: int, stdout: str = "", stderr: str = "") -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=("x",), rc=rc, stdout=stdout, stderr=stderr, duration_ms=1
    )


def test_run_spawn_drives_the_shipped_cli_through_the_runner(monkeypatch):
    # check=False (a nonzero spawn is a normal asserted outcome) and an EXPLICIT
    # timeout=None (a live claude Run is unbounded token generation — the one
    # legitimate no-bound choice, made per call, never by default). `env`
    # overlays the inherited environment via the runner's default merge.
    captured = {}

    def fake_run(argv, *, cwd=None, env=None, check=True, timeout="unset", **kw):
        captured.update(argv=argv, cwd=cwd, env=env, check=check, timeout=timeout)
        return _exec_result(2, stdout="SPAWNED", stderr="boom")

    monkeypatch.setattr(dogfood.execrun, "run", fake_run)
    result = dogfood._run_spawn(["spawn", "subagent"], cwd="/scratch", env={"K": "V"})
    assert result == dogfood.SpawnInvocation(2, "SPAWNED", "boom")
    assert captured["argv"] == ["shipit", "spawn", "subagent"]
    assert captured["cwd"] == "/scratch"
    assert captured["env"] == {"K": "V"}
    assert captured["check"] is False
    assert captured["timeout"] is None


def test_pixi_runs_probes_with_scrubbed_env_verbatim(monkeypatch):
    # The scrubbed env is the COMPLETE child environment (replace_env=True): the
    # runner's default merge over os.environ would re-add the very parent
    # PIXI_* / Conda pointers the scrub removed. The probe runs through the pixi
    # adapter's run-wrap (explicit --manifest-path, PROC02-WS02) and shares pixi's
    # provisioning-shaped bound (its worst case is a first-activation re-solve).
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    captured = {}

    def fake_run(argv, *, cwd=None, env=None, replace_env=False, timeout=None, **kw):
        captured.update(argv=argv, env=env, replace_env=replace_env, timeout=timeout)
        return _exec_result(0, stdout="pixi-ok\n")

    monkeypatch.setattr(dogfood.execrun, "run", fake_run)
    ok, detail = dogfood._pixi_runs("/tree")
    assert ok
    assert detail == "rc=0"
    assert captured["argv"] == [
        "pixi",
        "run",
        "--manifest-path",
        str(Path("/tree") / "pixi.toml"),
        "--",
        "python",
        "-c",
        "print('pixi-ok')",
    ]
    assert captured["replace_env"] is True
    assert "PIXI_PROJECT_MANIFEST" not in captured["env"]
    assert captured["timeout"] == pixienv.INSTALL_TIMEOUT


def test_pixi_runs_reports_a_launch_failure_as_a_failed_check(monkeypatch):
    # A missing pixi normalizes into the runner's ExecError; the probe degrades
    # to a recorded FAIL detail, never an escaping exception.
    def boom(argv, **kw):
        raise execrun.ExecError(argv, rc=None, cause=execrun.CAUSE_MISSING_BINARY)

    monkeypatch.setattr(dogfood.execrun, "run", boom)
    ok, detail = dogfood._pixi_runs("/tree")
    assert not ok
    assert "pixi not launchable" in detail


# --------------------------------------------------------------------------
# Orchestration: verify_write_run with seams faked.
# --------------------------------------------------------------------------


def _seam_a_write_tree(monkeypatch, tmp_path, *, branch="TRE03/WS05"):
    """Plant a healthy write Tree on disk and wire the seams to a passing write run."""
    tree = _make_clone(tmp_path, dissociated=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    payload = {
        "tree": str(tree),
        "branch": branch,
        "base": "origin/main",
        "role": "implementer",
        "backend": "claude",
        "pr": 42,
        "pr_state": "OPEN",
        "pr_is_draft": True,
    }
    out = "SPAWNED\n" + __import__("json").dumps(payload)
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda argv, *, cwd, env=None: dogfood.SpawnInvocation(0, out, ""),
    )
    monkeypatch.setattr(dogfood, "_current_branch", lambda p: branch)
    monkeypatch.setattr(dogfood, "_pixi_runs", lambda p: (True, "rc=0"))
    monkeypatch.setattr(dogfood, "_scratch_dirty", lambda p: "")
    monkeypatch.setattr(dogfood, "_open_pr_heads", lambda repo: [branch])
    monkeypatch.setattr(dogfood, "_resolve_repo_slug", lambda repo, *, scratch: repo)
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    return cfg, tree, payload


def test_verify_write_run_passes_on_healthy_seams(tmp_path, monkeypatch):
    cfg, _tree, _payload = _seam_a_write_tree(monkeypatch, tmp_path)
    report = dogfood.Report()
    result = dogfood.verify_write_run(report, cfg)
    assert result is not None
    assert report.passed, [c for c in report.checks if not c.passed]
    names = " | ".join(c.name for c in report.checks)
    assert "planned branch" in names
    assert "OPEN, DRAFT PR" in names
    assert "pixi runs" in names
    assert "no cwd leak" in names
    assert "no origin side effect" in names


def test_verify_write_run_stops_when_spawn_exits_nonzero(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda argv, *, cwd, env=None: dogfood.SpawnInvocation(1, "", "boom"),
    )
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.Report()
    assert dogfood.verify_write_run(report, cfg) is None
    assert not report.passed
    assert report.checks[0].name == "write spawn exited 0"
    assert not report.checks[0].passed


def test_verify_write_run_fails_on_wrong_branch(tmp_path, monkeypatch):
    """A Tree that landed on shipit/install instead of the planned branch FAILS."""
    cfg, _tree, _payload = _seam_a_write_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(dogfood, "_current_branch", lambda p: "shipit/install")
    report = dogfood.Report()
    dogfood.verify_write_run(report, cfg)
    assert not report.passed
    not_install = next(c for c in report.checks if "NOT shipit/install" in c.name)
    assert not not_install.passed


def test_verify_write_run_fails_on_shipit_install_pr_side_effect(tmp_path, monkeypatch):
    cfg, _tree, _payload = _seam_a_write_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(dogfood, "_open_pr_heads", lambda repo: ["shipit/install"])
    report = dogfood.Report()
    dogfood.verify_write_run(report, cfg)
    side = next(c for c in report.checks if "no origin side effect" in c.name)
    assert not side.passed


def test_verify_write_run_fails_on_non_draft_pr(tmp_path, monkeypatch):
    tree = _make_clone(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    payload = {
        "tree": str(tree),
        "branch": "TRE03/WS05",
        "pr": 42,
        "pr_state": "OPEN",
        "pr_is_draft": False,
    }
    out = "SPAWNED\n" + __import__("json").dumps(payload)
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda a, *, cwd, env=None: dogfood.SpawnInvocation(0, out, ""),
    )
    monkeypatch.setattr(dogfood, "_current_branch", lambda p: "TRE03/WS05")
    monkeypatch.setattr(dogfood, "_pixi_runs", lambda p: (True, ""))
    monkeypatch.setattr(dogfood, "_scratch_dirty", lambda p: "")
    monkeypatch.setattr(dogfood, "_open_pr_heads", lambda repo: [])
    monkeypatch.setattr(dogfood, "_resolve_repo_slug", lambda repo, *, scratch: repo)
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.Report()
    dogfood.verify_write_run(report, cfg)
    draft = next(c for c in report.checks if "DRAFT PR" in c.name)
    assert not draft.passed


# --------------------------------------------------------------------------
# Orchestration: verify_reviewer_run with seams faked.
# --------------------------------------------------------------------------


def _seam_a_reviewer_tree(monkeypatch, tmp_path, *, branch="TRE03/WS05"):
    # A per-Run reviewer Tree is one flat leaf (ADR-0074): <repo>-<agent>-<ts>-<id>,
    # one segment below the central root — no `review/` kind segment.
    review_tree = tmp_path / "widget-claude-20260717-081333-abcd1234abcd"
    (review_tree / ".git" / "objects" / "info").mkdir(parents=True)
    (review_tree / "file.txt").write_text("code")
    # genuinely read-only working tree (dirs + files, .git left writable)
    readonly.chmod_readonly(str(review_tree))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    payload = {
        "tree": str(review_tree),
        "branch": branch,
        "base": f"origin/{branch}",
        "role": "reviewer",
        "backend": "claude",
    }
    out = "SPAWNED\n" + __import__("json").dumps(payload)
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda a, *, cwd, env=None: dogfood.SpawnInvocation(0, out, ""),
    )
    # The reviewer-review check is a DELTA: no reviews before the spawn, one after.
    review_calls = {"n": 0}

    def _pr_reviews(repo, pr):
        review_calls["n"] += 1
        return [] if review_calls["n"] == 1 else [{"id": 1, "state": "COMMENTED"}]

    monkeypatch.setattr(dogfood, "_pr_reviews", _pr_reviews)
    monkeypatch.setattr(dogfood, "_resolve_repo_slug", lambda repo, *, scratch: repo)
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    return cfg, review_tree, payload


def test_verify_reviewer_run_passes_on_healthy_seams(tmp_path, monkeypatch):
    cfg, _tree, payload = _seam_a_reviewer_tree(monkeypatch, tmp_path)
    argv_seen = []
    calls = {"n": 0}

    def run_spawn(argv, *, cwd, env=None):
        argv_seen.append(argv)
        calls["n"] += 1
        p = dict(payload)
        if calls["n"] == 2:
            # Per-Run (ADR-0074): the 2nd reviewer on the same head gets its OWN
            # distinct flat Tree — sharing is retired, so the paths must differ.
            p["tree"] = str(tmp_path / "widget-claude-20260717-081334-ef567890ef56")
        return dogfood.SpawnInvocation(0, "SPAWNED\n" + __import__("json").dumps(p), "")

    monkeypatch.setattr(dogfood, "_run_spawn", run_spawn)
    report = dogfood.Report()
    dogfood.verify_reviewer_run(report, cfg, {"pr": 42})
    assert report.passed, [c for c in report.checks if not c.passed]
    names = " | ".join(c.name for c in report.checks)
    assert "no PR linkage" in names
    assert "per-Run" in names
    assert "read-only Tree has no writable working file" in names
    assert "posted a NEW review" in names
    assert argv_seen
    assert all(argv[-2:] == ["--backend", "codex"] for argv in argv_seen)


def test_verify_reviewer_run_detects_a_reused_tree(tmp_path, monkeypatch):
    """Per-Run (ADR-0074): if a 2nd reviewer spawn returns the SAME tree (the retired
    shared-clone behaviour), the per-Run check FAILS."""
    cfg, tree, payload = _seam_a_reviewer_tree(monkeypatch, tmp_path)

    # The seam's default _run_spawn returns the SAME tree for every reviewer spawn —
    # i.e. a 2nd reviewer reusing the first's clone, which per-Run must reject.
    def run_spawn(argv, *, cwd, env=None):
        return dogfood.SpawnInvocation(
            0, "SPAWNED\n" + __import__("json").dumps(payload), ""
        )

    monkeypatch.setattr(dogfood, "_run_spawn", run_spawn)
    report = dogfood.Report()
    dogfood.verify_reviewer_run(report, cfg, {"pr": 42})
    per_run = next(c for c in report.checks if "per-Run" in c.name)
    assert not per_run.passed


def test_verify_reviewer_run_fails_when_no_new_review_posted(tmp_path, monkeypatch):
    """A PR whose review count does not GROW across the spawn — even with a
    pre-existing review present both before and after — FAILS the delta check."""
    cfg, _tree, _payload = _seam_a_reviewer_tree(monkeypatch, tmp_path)
    # A stale review exists before AND after the spawn → zero delta → must FAIL.
    monkeypatch.setattr(dogfood, "_pr_reviews", lambda repo, pr: [{"id": 7}])
    report = dogfood.Report()
    dogfood.verify_reviewer_run(report, cfg, {"pr": 42})
    posted = next(c for c in report.checks if "posted a NEW review" in c.name)
    assert not posted.passed


def test_verify_reviewer_run_fails_when_summary_carries_a_pr(tmp_path, monkeypatch):
    """A reviewer must report THROUGH the PR — a SPAWNED summary with a `pr` key is wrong."""
    cfg, tree, payload = _seam_a_reviewer_tree(monkeypatch, tmp_path)
    payload["pr"] = 99
    out = "SPAWNED\n" + __import__("json").dumps(payload)
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda a, *, cwd, env=None: dogfood.SpawnInvocation(0, out, ""),
    )
    report = dogfood.Report()
    dogfood.verify_reviewer_run(report, cfg, {"pr": 42})
    linkage = next(c for c in report.checks if "no PR linkage" in c.name)
    assert not linkage.passed


# --------------------------------------------------------------------------
# Orchestration: verify_fail_closed with seams faked.
# --------------------------------------------------------------------------


def test_verify_fail_closed_passes_on_loud_nonzero_exit(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    seen = {}

    def run_spawn(argv, *, cwd, env=None):
        seen["env"] = env
        return dogfood.SpawnInvocation(
            1, "", "spawn subagent: tree creation failed: not absolute"
        )

    monkeypatch.setattr(dogfood, "_run_spawn", run_spawn)
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.Report()
    dogfood.verify_fail_closed(report, cfg)
    assert report.passed, [c for c in report.checks if not c.passed]
    # it forced the failure via a relative SHIPIT_TREES_ROOT
    assert seen["env"][dogfood.TREES_ROOT_ENV] == "relative-not-abs"


def test_verify_fail_closed_fails_if_spawn_succeeds(tmp_path, monkeypatch):
    """If the forced-failure spawn somehow exits 0, the fail-closed check FAILS."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda a, *, cwd, env=None: dogfood.SpawnInvocation(0, "SPAWNED\n{}", ""),
    )
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.Report()
    dogfood.verify_fail_closed(report, cfg)
    nonzero = next(c for c in report.checks if "exits nonzero" in c.name)
    assert not nonzero.passed


def test_verify_fail_closed_fails_when_native_worktree_appears(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    (scratch / ".claude" / "worktrees" / "agent-x").mkdir(parents=True)
    monkeypatch.setattr(
        dogfood,
        "_run_spawn",
        lambda a, *, cwd, env=None: dogfood.SpawnInvocation(
            1, "", "tree creation failed"
        ),
    )
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.Report()
    dogfood.verify_fail_closed(report, cfg)
    native = next(c for c in report.checks if "native worktree" in c.name)
    assert not native.passed


# --------------------------------------------------------------------------
# verify() end-to-end (all seams faked) + format_report + main guard.
# --------------------------------------------------------------------------


def test_verify_runs_all_three_scenarios(tmp_path, monkeypatch):
    """`verify` accumulates write + reviewer + fail-closed checks into one report."""
    write_tree = _make_clone(tmp_path, dissociated=True)
    # A per-Run reviewer Tree is one flat leaf (ADR-0074) — no `review/` kind segment.
    review_tree = tmp_path / "widget-claude-20260717-081333-abcd1234abcd"
    (review_tree / ".git" / "objects" / "info").mkdir(parents=True)
    (review_tree / "f").write_text("x")
    readonly.chmod_readonly(str(review_tree))
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    branch = "TRE03/WS05"
    write_payload = {
        "tree": str(write_tree),
        "branch": branch,
        "pr": 42,
        "pr_state": "OPEN",
        "pr_is_draft": True,
    }
    review_payload = {"tree": str(review_tree), "branch": branch, "role": "reviewer"}
    reviewer_calls = {"n": 0}

    def run_spawn(argv, *, cwd, env=None):
        if env and dogfood.TREES_ROOT_ENV in env:
            return dogfood.SpawnInvocation(1, "", "tree creation failed: relative")
        if "reviewer" in argv:
            reviewer_calls["n"] += 1
            p = dict(review_payload)
            if reviewer_calls["n"] == 2:
                # Per-Run: the 2nd reviewer gets its OWN distinct flat Tree.
                p["tree"] = str(tmp_path / "widget-claude-20260717-081334-ef567890ef56")
            return dogfood.SpawnInvocation(
                0, "SPAWNED\n" + __import__("json").dumps(p), ""
            )
        return dogfood.SpawnInvocation(
            0, "SPAWNED\n" + __import__("json").dumps(write_payload), ""
        )

    monkeypatch.setattr(dogfood, "_run_spawn", run_spawn)
    monkeypatch.setattr(dogfood, "_current_branch", lambda p: branch)
    monkeypatch.setattr(dogfood, "_pixi_runs", lambda p: (True, ""))
    monkeypatch.setattr(dogfood, "_scratch_dirty", lambda p: "")
    monkeypatch.setattr(dogfood, "_open_pr_heads", lambda repo: [branch])
    monkeypatch.setattr(dogfood, "_resolve_repo_slug", lambda repo, *, scratch: repo)
    review_calls = {"n": 0}

    def pr_reviews(repo, pr):
        review_calls["n"] += 1
        return [] if review_calls["n"] == 1 else [{"id": 1}]

    monkeypatch.setattr(dogfood, "_pr_reviews", pr_reviews)

    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.verify(cfg)
    assert report.passed, [c for c in report.checks if not c.passed]
    text = dogfood.format_report(report, cfg=cfg)
    assert "PASS" in text
    for c in report.checks:
        assert c.name in text


def test_verify_records_failure_when_a_seam_raises(tmp_path, monkeypatch):
    """A live seam throwing must NOT abort the harness: ``verify`` catches it, records
    a failed check carrying the exception detail, and still returns a report."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def boom(*_a, **_k):
        raise RuntimeError("live seam exploded")

    monkeypatch.setattr(dogfood, "_run_spawn", boom)
    cfg = dogfood.DogfoodConfig(
        scratch=str(scratch),
        repo="acme/widget",
        epic="TRE03",
        ws=5,
        issue=159,
        central_root=str(tmp_path),
    )
    report = dogfood.verify(cfg)  # must NOT raise
    assert not report.passed
    assert any(
        not c.passed and "RuntimeError: live seam exploded" in c.detail
        for c in report.checks
    )


# --------------------------------------------------------------------------
# _resolve_repo_slug — the repo-code → owner/name resolution for the REST seams.
# --------------------------------------------------------------------------


def test_resolve_repo_slug_canonicalises_an_owner_name(monkeypatch):
    """A value already in owner/name form is normalised via gh.repo_canonical."""
    monkeypatch.setattr(
        dogfood.gh,
        "repo_canonical",
        lambda slug: repo_from_slug(f"canon/{slug.split('/')[-1]}"),
    )
    assert dogfood._resolve_repo_slug("acme/widget", scratch="/s") == "canon/widget"


def test_resolve_repo_slug_resolves_a_bare_code_from_the_scratch_checkout(monkeypatch):
    """A slashless repo code resolves to the scratch checkout's owner/name (the spawn
    target), so the REST seams never request ``/repos/shipit/...``."""
    seen = {}

    def current_repo(*, cwd=None):
        seen["cwd"] = cwd
        return repo_from_slug("arthur-debert/shipit")

    monkeypatch.setattr(dogfood.gh, "current_repo", current_repo)
    assert (
        dogfood._resolve_repo_slug("shipit", scratch="/scratch/x")
        == "arthur-debert/shipit"
    )
    assert seen["cwd"] == "/scratch/x"


def test_main_requires_explicit_target(monkeypatch):
    for var in ("SCRATCH", "REPO", "EPIC", "WS", "ISSUE"):
        monkeypatch.delenv(f"SHIPIT_DOGFOOD_{var}", raising=False)
    with pytest.raises(SystemExit) as exc:
        dogfood.main([])
    assert exc.value.code != 0


def test_main_returns_zero_on_pass_and_one_on_fail(monkeypatch):
    passing = dogfood.Report()
    passing.record("ok", True)
    monkeypatch.setattr(dogfood, "verify", lambda cfg: passing)
    argv = [
        "--scratch",
        "/s",
        "--repo",
        "acme/widget",
        "--epic",
        "TRE03",
        "--ws",
        "5",
        "--issue",
        "159",
    ]
    assert dogfood.main(argv) == 0

    failing = dogfood.Report()
    failing.record("nope", False)
    monkeypatch.setattr(dogfood, "verify", lambda cfg: failing)
    assert dogfood.main(argv) == 1


def test_env_int_parses_or_none(monkeypatch):
    monkeypatch.setenv("SHIPIT_DOGFOOD_WS", "7")
    assert dogfood._env_int("SHIPIT_DOGFOOD_WS") == 7
    monkeypatch.setenv("SHIPIT_DOGFOOD_WS", "notanint")
    assert dogfood._env_int("SHIPIT_DOGFOOD_WS") is None
    monkeypatch.delenv("SHIPIT_DOGFOOD_WS", raising=False)
    assert dogfood._env_int("SHIPIT_DOGFOOD_WS") is None
