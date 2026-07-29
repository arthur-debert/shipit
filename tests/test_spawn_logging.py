from __future__ import annotations

import logging
from dataclasses import replace

from test_spawn_subagent import _PR, bounds

from shipit.execrun import ExecError
from shipit.spawn import launch
from shipit.verbs import spawn as spawn_verb


def _launcher(*, returncode=0):
    def runner(cmd, *, cwd, env, timeout=None):
        return launch.LaunchResult(returncode=returncode, stdout="{}", stderr="boom")

    return runner


def _spawn_records(caplog, level=None):
    records = [r for r in caplog.records if r.name == "shipit.spawn"]
    if level is not None:
        records = [r for r in records if r.levelno == level]
    return records


def _write_spawn(tmp_path, *, launcher=None, pr=_PR) -> int:
    b, _calls = bounds(tmp_path, pr=pr)
    if launcher is not None:
        b = replace(b, runner=launcher)
    return spawn_verb.run(
        repo="widget",
        epic="TRE03",
        ws=1,
        issue=156,
        role="implementer",
        bounds=b,
    )


def test_write_spawn_narrates_the_lifecycle_at_info(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path)
    assert rc == 0
    infos = _spawn_records(caplog, logging.INFO)

    requested = [r for r in infos if hasattr(r, "issue") and not hasattr(r, "branch")]
    assert len(requested) == 1
    assert requested[0].role == "implementer"
    assert requested[0].backend == "claude"
    assert requested[0].epic == "TRE03" and requested[0].ws == 1
    assert requested[0].issue == 156

    assigned = [r for r in infos if hasattr(r, "base") and hasattr(r, "duration_ms")]
    assert len(assigned) == 1
    assert assigned[0].branch == "TRE03/WS01"
    assert assigned[0].base == "origin/TRE03/umbrella"
    assert isinstance(assigned[0].duration_ms, int)

    launched = [r for r in infos if hasattr(r, "cwd")]
    assert len(launched) == 1
    assert launched[0].backend == "claude" and launched[0].role == "implementer"
    assert launched[0].cwd == str(tmp_path / "tree")

    exited = [r for r in infos if hasattr(r, "rc")]
    assert len(exited) == 1
    assert exited[0].rc == 0 and isinstance(exited[0].duration_ms, int)

    spawned = [r for r in infos if hasattr(r, "pr")]
    assert len(spawned) == 1
    assert spawned[0].pr == 321
    assert spawned[0].pr_is_draft is True
    assert spawned[0].branch == "TRE03/WS01"
    assert spawned[0].tree == str(tmp_path / "tree")

    assert not _spawn_records(caplog, logging.ERROR)
    assert not _spawn_records(caplog, logging.WARNING)


def test_reviewer_spawn_narrates_the_lifecycle_at_info(tmp_path, caplog):
    b, _calls = bounds(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run(
            repo="widget",
            epic="TRE03",
            ws=3,
            role="reviewer",
            backend="codex",
            bounds=b,
        )
    assert rc == 0
    infos = _spawn_records(caplog, logging.INFO)

    delegated = [r for r in infos if hasattr(r, "base") and hasattr(r, "pr")]
    assert len(delegated) == 1
    assert delegated[0].branch == "TRE03/WS03"
    assert delegated[0].pr == 321

    launched = [r for r in infos if hasattr(r, "cwd")]
    assert len(launched) == 1 and launched[0].backend == "codex"
    exited = [r for r in infos if hasattr(r, "rc")]
    assert len(exited) == 1 and exited[0].rc == 0

    spawned = [r for r in infos if hasattr(r, "tree")]
    assert len(spawned) == 1
    assert spawned[0].role == "reviewer"
    assert not hasattr(spawned[0], "pr")


def test_tree_creation_failure_logs_error_with_the_exception(tmp_path, caplog):
    b, _calls = bounds(tmp_path)

    def boom(spec, *, source_repo, github_url):
        raise ExecError(["git", "clone"], rc=1, stderr="clone failed")

    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run(
            repo="widget",
            epic="TRE03",
            ws=1,
            issue=156,
            role="implementer",
            bounds=replace(b, create_tree=boom),
        )
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info
    assert isinstance(errors[0].exc_info[1], ExecError)


def test_launch_transport_failure_logs_error_with_the_exception(tmp_path, caplog):
    def no_binary(cmd, *, cwd, env, timeout=None):
        raise ExecError(["claude"], rc=None, stderr="not found", cause="missing-binary")

    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, launcher=no_binary)
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info and isinstance(errors[0].exc_info[1], ExecError)
    assert errors[0].backend == "claude"


def test_nonzero_child_exit_logs_error_with_rc_and_duration(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, launcher=_launcher(returncode=2))
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].rc == 2
    assert isinstance(errors[0].duration_ms, int)
    assert not errors[0].exc_info


def test_handshake_failure_no_pr_logs_error(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, pr=None)
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].branch == "TRE03/WS01"


def test_handshake_failure_wrong_state_logs_error_with_the_pr(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, pr=replace(_PR, state="MERGED"))
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].pr == 321 and errors[0].pr_state == "MERGED"


def test_validation_refusals_are_no_longer_print_only(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run(
            repo="widget", issue=1, role="implementer", backend="nonexistent"
        )
    assert rc == 1
    errors = _spawn_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].backend == "nonexistent"


def test_refused_spawn_does_not_inherit_previous_spawn_context(tmp_path, caplog):
    from shipit import logcontext

    try:
        assert _write_spawn(tmp_path) == 0
        assert logcontext.bound().get("tree") == str(tmp_path / "tree")
        logcontext.bind(pr=321, repo="acme/widget")

        caplog.clear()
        rc = spawn_verb.run(
            repo="widget", issue=1, role="implementer", backend="nonexistent"
        )
        assert rc == 1
        assert not {"tree", "pr", "repo"} & logcontext.bound().keys()
    finally:
        logcontext.unbind("tree", "pr", "repo")


def test_the_request_is_recorded_even_when_refused(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        rc = spawn_verb.run(repo="widget", epic="TRE03", ws=0, issue=1, role="x")
    assert rc == 1
    requested = [r for r in _spawn_records(caplog, logging.INFO) if hasattr(r, "role")]
    assert len(requested) == 1 and requested[0].ws == 0


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
    assert "/parent/pixi.toml" not in records[0].getMessage()
    assert "/conda/env-secretish-path" not in records[0].getMessage()


def test_scrub_tree_env_is_silent_when_nothing_leaks(caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.spawn"):
        launch.scrub_tree_env({"HOME": "/home/u", "PATH": "/usr/bin"})
    assert not [
        r for r in _spawn_records(caplog, logging.DEBUG) if hasattr(r, "dropped")
    ]


def _event_tags(caplog):
    from shipit import events

    return [
        getattr(r, events.EXTRA_KEY)
        for r in _spawn_records(caplog)
        if getattr(r, events.EXTRA_KEY, None)
    ]


def test_write_spawn_tags_agent_spawned_and_agent_done(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path)
    assert rc == 0
    assert _event_tags(caplog) == [
        "agent.phase",
        "agent.spawned",
        "agent.phase",
        "agent.done",
        "agent.phase",
    ]


def test_reviewer_spawn_tags_agent_spawned_and_agent_done(tmp_path, caplog):
    b, _calls = bounds(tmp_path)
    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        rc = spawn_verb.run(
            repo="widget",
            epic="TRE03",
            ws=3,
            role="reviewer",
            backend="codex",
            bounds=b,
        )
    assert rc == 0
    assert _event_tags(caplog) == [
        "agent.phase",
        "agent.spawned",
        "agent.phase",
        "agent.done",
    ]


def test_nonzero_child_exit_tags_no_agent_done(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        rc = _write_spawn(tmp_path, launcher=_launcher(returncode=3))
    assert rc == 1
    assert _event_tags(caplog) == ["agent.phase", "agent.spawned", "agent.phase"]
