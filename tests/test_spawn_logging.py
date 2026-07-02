"""LOG02-WS02 (#247): the spawn subsystem narrates its lifecycle on the record.

Convention-level tests (glassbox PRD §Testing Decisions): they assert that the
KEY LIFECYCLE EVENTS exist and carry the required fields — the spawn request,
the Tree assignment (with a duration), the backend launch, the child exit (rc +
duration), and the spawn handshake (the Run↔PR linkage) — and that propagating
failures land at ERROR with the exception attached. Deliberately NO per-message
string assertions: the message text is prose, the fields are the contract.

The user-facing surface is pinned unchanged elsewhere (``test_spawn_verb``
asserts the stderr diagnostics and the SPAWNED stdout block byte-for-byte);
here we only assert logging is ADDITIVE under it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from shipit import gh
from shipit.execrun import ExecError
from shipit.spawn import launch
from shipit.tree import layout
from shipit.tree.create import Tree
from shipit.verbs import spawn as spawn_verb

# ---------------------------------------------------------------------------
# Fakes — mirroring test_spawn_verb's boundary spies, trimmed to what the
# logging assertions need (no captured-arg plumbing).
# ---------------------------------------------------------------------------


def _patch_identity(monkeypatch, *, root="/repo", org_repo="acme/widget"):
    monkeypatch.setattr(gh, "repo_root", lambda: root)
    monkeypatch.setattr(gh, "current_repo", lambda: org_repo)
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:" + org_repo)
    monkeypatch.setattr(gh, "remote_branch_exists", lambda *a, **k: True)


def _fake_create(monkeypatch, tree_dir: Path) -> None:
    def fake_create(spec, *, source_repo, github_url):
        tree_dir.mkdir(parents=True, exist_ok=True)
        tp = layout.plan(spec)
        return Tree(path=str(tree_dir), branch=tp.branch, base=tp.base)

    monkeypatch.setattr(spawn_verb, "create", fake_create)


def _fake_create_readonly(monkeypatch, tree_dir: Path) -> None:
    def fake(plan, *, source_repo, github_url):
        tree_dir.mkdir(parents=True, exist_ok=True)
        return Tree(
            path=str(tree_dir), branch=plan.branch, base=f"origin/{plan.branch}"
        )

    monkeypatch.setattr(spawn_verb, "create_readonly", fake)


def _launcher(*, returncode=0):
    def runner(cmd, *, cwd, env):
        return launch.LaunchResult(returncode=returncode, stdout="{}", stderr="boom")

    return runner


def _patch_pr(monkeypatch, pr):
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: pr)


_PR = {"number": 321, "state": "OPEN", "isDraft": True, "baseRefName": "TRE03/umbrella"}


def _spawn_records(caplog, level=None):
    records = [r for r in caplog.records if r.name == "shipit.spawn"]
    if level is not None:
        records = [r for r in records if r.levelno == level]
    return records


def _write_spawn(tmp_path, monkeypatch, *, launcher=None, pr=_PR) -> int:
    """Drive a full write-shape spawn over faked boundaries; return its exit code."""
    parent = tmp_path / "repo"
    parent.mkdir(exist_ok=True)
    _patch_identity(monkeypatch, root=str(parent))
    _fake_create(monkeypatch, tmp_path / "tree")
    _patch_pr(monkeypatch, pr)
    return spawn_verb.run_subagent(
        repo="widget",
        epic="TRE03",
        ws=1,
        issue=156,
        role="implementer",
        launcher=launcher or _launcher(),
    )


# ---------------------------------------------------------------------------
# Lifecycle milestones at INFO, with the required fields
# ---------------------------------------------------------------------------


def test_write_spawn_narrates_the_lifecycle_at_info(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, monkeypatch)
    assert rc == 0
    infos = _spawn_records(caplog, logging.INFO)

    # The spawn REQUEST: the run's coordinates ride the record as flat fields.
    requested = [r for r in infos if hasattr(r, "issue") and not hasattr(r, "branch")]
    assert len(requested) == 1
    assert requested[0].role == "implementer"
    assert requested[0].backend == "claude"
    assert requested[0].epic == "TRE03" and requested[0].ws == 1
    assert requested[0].issue == 156

    # Tree ASSIGNMENT: branch + base + a duration (Tree birth is timed).
    assigned = [r for r in infos if hasattr(r, "base") and hasattr(r, "duration_ms")]
    assert len(assigned) == 1
    assert assigned[0].branch == "TRE03/WS01"
    assert assigned[0].base == "origin/TRE03/umbrella"
    assert isinstance(assigned[0].duration_ms, int)

    # Backend LAUNCH: backend + role + the Tree the child is rooted in. The argv
    # detail is the Exec runner's DEBUG record, deliberately not duplicated here.
    launched = [r for r in infos if hasattr(r, "cwd")]
    assert len(launched) == 1
    assert launched[0].backend == "claude" and launched[0].role == "implementer"
    assert launched[0].cwd == str(tmp_path / "tree")

    # Child EXIT: the rc and the Run's wall-clock.
    exited = [r for r in infos if hasattr(r, "rc")]
    assert len(exited) == 1
    assert exited[0].rc == 0 and isinstance(exited[0].duration_ms, int)

    # The spawn HANDSHAKE: the Run↔PR linkage on the record (pr doubles as the
    # domain key an agent slices by).
    spawned = [r for r in infos if hasattr(r, "pr")]
    assert len(spawned) == 1
    assert spawned[0].pr == 321
    assert spawned[0].pr_is_draft is True
    assert spawned[0].branch == "TRE03/WS01"
    assert spawned[0].tree == str(tmp_path / "tree")

    # And nothing on the lifecycle path logged above INFO on success.
    assert not _spawn_records(caplog, logging.ERROR)
    assert not _spawn_records(caplog, logging.WARNING)


def test_reviewer_spawn_narrates_the_lifecycle_at_info(tmp_path, monkeypatch, caplog):
    parent = tmp_path / "repo"
    parent.mkdir()
    _patch_identity(monkeypatch, root=str(parent))
    _fake_create_readonly(monkeypatch, tmp_path / "review")

    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run_subagent(
            repo="widget", epic="TRE03", ws=3, role="reviewer", launcher=_launcher()
        )
    assert rc == 0
    infos = _spawn_records(caplog, logging.INFO)

    # Tree assignment (the shared read-only Tree) is timed like the write one.
    assigned = [r for r in infos if hasattr(r, "base") and hasattr(r, "duration_ms")]
    assert len(assigned) == 1
    assert assigned[0].branch == "TRE03/WS03"
    assert isinstance(assigned[0].duration_ms, int)

    # Launch + child exit, as on the write path.
    assert [r for r in infos if hasattr(r, "cwd")]
    exited = [r for r in infos if hasattr(r, "rc")]
    assert len(exited) == 1 and exited[0].rc == 0

    # The handshake record: a reviewer reports through the EXISTING PR, so the
    # SPAWNED record carries no Run↔PR linkage.
    spawned = [r for r in infos if hasattr(r, "tree")]
    assert len(spawned) == 1
    assert spawned[0].role == "reviewer"
    assert not hasattr(spawned[0], "pr")


# ---------------------------------------------------------------------------
# Propagating failures at ERROR — with the exception attached where one exists
# ---------------------------------------------------------------------------


def test_tree_creation_failure_logs_error_with_the_exception(
    tmp_path, monkeypatch, caplog
):
    parent = tmp_path / "repo"
    parent.mkdir()
    _patch_identity(monkeypatch, root=str(parent))

    def boom(spec, *, source_repo, github_url):
        raise ExecError(["git", "clone"], rc=1, stderr="clone failed")

    monkeypatch.setattr(spawn_verb, "create", boom)

    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run_subagent(
            repo="widget",
            epic="TRE03",
            ws=1,
            issue=156,
            role="implementer",
            launcher=_launcher(),
        )
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info  # the exception rides the record
    assert isinstance(errors[0].exc_info[1], ExecError)


def test_launch_transport_failure_logs_error_with_the_exception(
    tmp_path, monkeypatch, caplog
):
    def no_binary(cmd, *, cwd, env):
        raise ExecError(["claude"], rc=None, stderr="not found", cause="missing-binary")

    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, monkeypatch, launcher=no_binary)
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info and isinstance(errors[0].exc_info[1], ExecError)
    assert errors[0].backend == "claude"


def test_nonzero_child_exit_logs_error_with_rc_and_duration(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, monkeypatch, launcher=_launcher(returncode=2))
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].rc == 2
    assert isinstance(errors[0].duration_ms, int)
    # A nonzero child is a lifecycle outcome, not an exception — none is attached.
    assert not errors[0].exc_info


def test_handshake_failure_no_pr_logs_error(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, monkeypatch, pr=None)
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].branch == "TRE03/WS01"


def test_handshake_failure_wrong_state_logs_error_with_the_pr(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, monkeypatch, pr={**_PR, "state": "MERGED"})
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].pr == 321 and errors[0].pr_state == "MERGED"


def test_validation_refusals_are_no_longer_print_only(monkeypatch, caplog):
    """Every refusal used to leave ONLY a stderr print; each now also logs at
    ERROR (the spray rule: anything whose only record was a print also logs)."""
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run_subagent(
            repo="widget", issue=1, role="implementer", backend="nonexistent"
        )
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].backend == "nonexistent"


def test_the_request_is_recorded_even_when_refused(monkeypatch, caplog):
    """The spawn REQUEST milestone precedes the gates: a refused spawn still
    leaves a durable record of what was asked."""
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run_subagent(
            repo="widget", epic="TRE03", ws=0, issue=1, role="x"
        )
    assert rc == 1
    requested = [r for r in _spawn_records(caplog, logging.INFO) if hasattr(r, "role")]
    assert len(requested) == 1 and requested[0].ws == 0


# ---------------------------------------------------------------------------
# Launch mechanics at DEBUG
# ---------------------------------------------------------------------------


def test_pixi_wrap_records_its_routing_decision_at_debug(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        launch.pixi_wrap(["claude", "-p"], tmp_path)  # no provisioned env → bare
    bare = [
        r for r in _spawn_records(caplog, logging.DEBUG) if hasattr(r, "pixi_wrapped")
    ]
    assert len(bare) == 1 and bare[0].pixi_wrapped is False

    caplog.clear()
    tmp_path.joinpath(*launch.PIXI_DEFAULT_ENV).mkdir(parents=True)
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        launch.pixi_wrap(["claude", "-p"], tmp_path)
    wrapped = [
        r for r in _spawn_records(caplog, logging.DEBUG) if hasattr(r, "pixi_wrapped")
    ]
    assert len(wrapped) == 1 and wrapped[0].pixi_wrapped is True


def test_scrub_tree_env_records_the_drop_at_debug_names_only(caplog):
    env = {
        "PIXI_PROJECT_MANIFEST": "/parent/pixi.toml",
        "CONDA_PREFIX": "/conda/env-secretish-path",
        "HOME": "/home/u",
    }
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        scrubbed = launch.scrub_tree_env(env)
    assert "PIXI_PROJECT_MANIFEST" not in scrubbed
    records = [
        r for r in _spawn_records(caplog, logging.DEBUG) if hasattr(r, "dropped")
    ]
    assert len(records) == 1
    assert records[0].dropped == 2
    # Names only — a dropped var's VALUE never reaches the record.
    assert "/parent/pixi.toml" not in records[0].getMessage()
    assert "/conda/env-secretish-path" not in records[0].getMessage()


def test_scrub_tree_env_is_silent_when_nothing_leaks(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        launch.scrub_tree_env({"HOME": "/home/u", "PATH": "/usr/bin"})
    assert not [
        r for r in _spawn_records(caplog, logging.DEBUG) if hasattr(r, "dropped")
    ]
