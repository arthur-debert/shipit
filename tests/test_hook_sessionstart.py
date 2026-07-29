from __future__ import annotations

import builtins
import io
import json
import logging
import shlex
import subprocess
from pathlib import Path

import pytest
import structlog

from shipit import logcontext
from shipit.harness import activation
from shipit.pixienv import Activation, parse_activation
from shipit.tree import layout
from shipit.verbs.hook import sessionstart

TREE_ROOT = "/trees/SES01/WS01"

SHELL_HOOK_JSON = json.dumps(
    {
        "environment_variables": {
            "PATH": f"{TREE_ROOT}/.pixi/envs/default/bin:/usr/bin:/bin",
            "CONDA_PREFIX": f"{TREE_ROOT}/.pixi/envs/default",
            "CONDA_DEFAULT_ENV": "shipit",
            "PIXI_PROMPT": "(shipit) ",
        },
        "activation_scripts": [],
    }
)


def _fake_runner(captured: dict):

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=SHELL_HOOK_JSON, stderr="")

    return runner


def _run(payload: dict | str, env: dict, runner=None) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    kwargs = {"runner": runner} if runner is not None else {}
    return sessionstart.run(stdin=io.StringIO(text), environ=env, **kwargs)


@pytest.fixture
def pixi_repo(tmp_path):
    (tmp_path / "pixi.toml").write_text('[project]\nname = "x"\n')
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    return tmp_path


def test_pixi_toolchain_maps_to_export_lines():
    toolchain = activation.Toolchain(kind=activation.PIXI, manifest=Path("pixi.toml"))
    script = activation.activation_script(toolchain, parse_activation(SHELL_HOOK_JSON))
    assert script.splitlines() == [
        f"export PATH={TREE_ROOT}/.pixi/envs/default/bin:/usr/bin:/bin",
        f"export CONDA_PREFIX={TREE_ROOT}/.pixi/envs/default",
        "export CONDA_DEFAULT_ENV=shipit",
        "export PIXI_PROMPT='(shipit) '",
    ]


def test_no_toolchain_maps_to_empty():
    act = parse_activation(SHELL_HOOK_JSON)
    assert activation.activation_script(None, act) == ""
    assert activation.activation_script(None, None) == ""


def test_unknown_toolchain_kind_maps_to_empty():
    toolchain = activation.Toolchain(kind="npm", manifest=Path("package.json"))
    assert (
        activation.activation_script(toolchain, parse_activation(SHELL_HOOK_JSON)) == ""
    )


def test_export_lines_skip_non_identifier_keys():
    act = Activation(
        environment_variables={"OK": "1", "BAD-KEY": "x", "2BAD": "y"},
        activation_scripts=(),
    )
    assert activation.export_lines(act) == "export OK=1"


def test_activation_scripts_are_not_rendered_their_env_effects_already_are():
    toolchain = activation.Toolchain(kind=activation.PIXI, manifest=Path("pixi.toml"))
    act = Activation(
        environment_variables={"SCRIPT_VAR": "set-by-script", "DECLARED": "1"},
        activation_scripts=("/repo/act.sh",),
    )
    script = activation.activation_script(toolchain, act)
    assert script.splitlines() == [
        "export SCRIPT_VAR=set-by-script",
        "export DECLARED=1",
    ]
    assert "act.sh" not in script


def test_export_lines_quote_hostile_values():
    act = Activation(
        environment_variables={"HOSTILE": "a 'b' $(rm -rf /) $HOME"},
        activation_scripts=(),
    )
    line = activation.export_lines(act)
    assert line == """export HOSTILE='a '"'"'b'"'"' $(rm -rf /) $HOME'"""


def test_detect_toolchain_walks_up_to_the_manifest(pixi_repo):
    toolchain = activation.detect_toolchain(pixi_repo / "src" / "pkg")
    assert toolchain == activation.Toolchain(
        kind=activation.PIXI, manifest=(pixi_repo / "pixi.toml").resolve()
    )


def test_detect_toolchain_none_without_a_manifest(tmp_path):
    assert activation.detect_toolchain(tmp_path) is None


def test_pixi_repo_activation_lands_in_the_env_file(pixi_repo, tmp_path):
    env_file = tmp_path / "claude-env"
    captured: dict = {}
    code = _run(
        {"hook_event_name": "SessionStart", "cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner(captured),
    )
    assert code == 0
    assert captured["cmd"][:3] == ["pixi", "shell-hook", "--json"]
    assert "--environment" not in captured["cmd"]
    assert str((pixi_repo / "pixi.toml").resolve()) in captured["cmd"]
    content = env_file.read_text()
    assert f"export CONDA_PREFIX={TREE_ROOT}/.pixi/envs/default\n" in content
    assert "export CONDA_DEFAULT_ENV=shipit" in content


def test_env_file_is_appended_never_clobbered(pixi_repo, tmp_path):
    env_file = tmp_path / "claude-env"
    env_file.write_text("export OTHER_HOOK=kept\n")
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    content = env_file.read_text()
    assert content.startswith("export OTHER_HOOK=kept\n")
    assert "export CONDA_DEFAULT_ENV=shipit" in content


def test_non_pixi_repo_is_a_clean_noop(tmp_path):
    env_file = tmp_path / "claude-env"

    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no toolchain — pixi must not run")

    code = _run(
        {"cwd": str(tmp_path)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=exploding_runner,
    )
    assert code == 0
    assert not env_file.exists()


def test_missing_env_file_var_is_a_noop(pixi_repo):
    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no CLAUDE_ENV_FILE — pixi must not run")

    assert _run({"cwd": str(pixi_repo)}, {}, runner=exploding_runner) == 0


def test_malformed_payload_falls_back_to_process_cwd(pixi_repo, tmp_path, monkeypatch):
    monkeypatch.chdir(pixi_repo)
    env_file = tmp_path / "claude-env"
    code = _run(
        "not json at all",
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    assert "export CONDA_DEFAULT_ENV=shipit" in env_file.read_text()


def test_pixi_failure_fails_open(pixi_repo, tmp_path):
    env_file = tmp_path / "claude-env"

    def failing_runner(cmd, **kwargs):
        raise RuntimeError("pixi exploded")

    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=failing_runner,
    )
    assert code == 0
    assert not env_file.exists()


def _torn_open(monkeypatch, env_file: Path) -> None:
    real_open = builtins.open

    class TornHandle:
        def __init__(self, *args, **kwargs):
            self._handle = real_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def write(self, text):
            self._handle.write(text[: len(text) // 2])
            self._handle.flush()
            raise OSError("no space left on device")

    def torn(file, *args, **kwargs):
        if str(file) == str(env_file):
            return TornHandle(file, *args, **kwargs)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", torn)


def test_torn_write_rolls_back_to_the_prior_bytes(pixi_repo, tmp_path, monkeypatch):
    env_file = tmp_path / "claude-env"
    env_file.write_text("export OTHER_HOOK=kept\n")
    _torn_open(monkeypatch, env_file)
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    assert env_file.read_text() == "export OTHER_HOOK=kept\n"


def test_torn_write_removes_a_file_the_hook_created(pixi_repo, tmp_path, monkeypatch):
    env_file = tmp_path / "claude-env"
    _torn_open(monkeypatch, env_file)
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    assert not env_file.exists()


def test_append_survives_env_file_vanishing_before_the_write(
    pixi_repo, tmp_path, monkeypatch
):
    env_file = tmp_path / "claude-env"
    env_file.write_text("export DOOMED=1\n")
    real_open = builtins.open

    def deleting_open(file, *args, **kwargs):
        if str(file) == str(env_file):
            monkeypatch.setattr(builtins, "open", real_open)
            env_file.unlink(missing_ok=True)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deleting_open)
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    assert "export CONDA_DEFAULT_ENV=shipit" in env_file.read_text()


def test_unwritable_env_file_fails_open(pixi_repo, tmp_path):
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(tmp_path)},
        runner=_fake_runner({}),
    )
    assert code == 0


SESSION_ID = "619cf51a-f501-44dc-992f-74df773204aa"
SESSION_LEAF = f"repo-claude-20260703-041649-{SESSION_ID}"


def _ephemeral_tree(root: Path, leaf: str = SESSION_LEAF) -> Path:
    tree = root / leaf
    tree.mkdir(parents=True)
    return tree


def _run_log_context(cwd: Path, env_file: Path) -> int:

    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no toolchain — pixi must not run")

    return sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(cwd)})),
        environ={"CLAUDE_ENV_FILE": str(env_file)},
        runner=exploding_runner,
    )


def test_ephemeral_tree_cwd_exports_session_log_context(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(tree, env_file)
    assert code == 0
    lines = env_file.read_text().splitlines()
    assert f"export {logcontext.ENV_PREFIX}SESSION={SESSION_ID}" in lines
    assert (
        f"export {logcontext.ENV_PREFIX}TREE={shlex.quote(str(tree.resolve()))}"
        in lines
    )


def test_exported_session_id_round_trips_through_bind_from_env(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    _run_log_context(tree, env_file)
    child_env = {}
    for line in env_file.read_text().splitlines():
        key, _, value = line.removeprefix("export ").partition("=")
        child_env[key] = "".join(shlex.split(value))
    structlog.contextvars.clear_contextvars()
    try:
        logcontext.bind_from_env(child_env)
        assert logcontext.bound() == {
            "session": SESSION_ID,
            "tree": str(tree.resolve()),
        }
    finally:
        structlog.contextvars.clear_contextvars()


def test_non_tree_cwd_exports_no_log_context(tmp_path, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    cwd = tmp_path / "plain-checkout"
    cwd.mkdir()
    env_file = tmp_path / "claude-env"
    code = _run_log_context(cwd, env_file)
    assert code == 0
    assert not env_file.exists()


def test_old_nested_tree_cwd_exports_no_log_context(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = root / "owner" / "repo" / "issues" / "349" / "work-deadbeef"
    tree.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(tree, env_file)
    assert code == 0
    assert not env_file.exists()


def test_nested_dir_inside_a_tree_exports_the_containing_session(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    nested = tree / "src" / "ephemeral" / "not-a-session"
    nested.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(nested, env_file)
    assert code == 0
    lines = env_file.read_text().splitlines()
    assert f"export {logcontext.ENV_PREFIX}SESSION={SESSION_ID}" in lines
    assert (
        f"export {logcontext.ENV_PREFIX}TREE={shlex.quote(str(tree.resolve()))}"
        in lines
    )


def test_flat_position_dir_without_a_uuid_tail_exports_no_log_context(
    tmp_path, monkeypatch
):
    root = tmp_path / "trees"
    shallow = root / "not-a-session"
    shallow.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(shallow, env_file)
    assert code == 0
    assert not env_file.exists()


def test_log_context_lands_alongside_the_activation_exports(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    (tree / "pixi.toml").write_text('[project]\nname = "x"\n')
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(tree)})),
        environ={"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    content = env_file.read_text()
    assert "export CONDA_DEFAULT_ENV=shipit" in content
    session_line = f"export {logcontext.ENV_PREFIX}SESSION={SESSION_ID}"
    assert session_line in content
    assert content.index("CONDA_DEFAULT_ENV") < content.index(session_line)


def test_log_context_detection_error_is_silent_and_debug(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/trees")
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    env_file = tmp_path / "claude-env"
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        code = _run_log_context(cwd, env_file)
    assert code == 0
    assert not env_file.exists()
    hook_records = [r for r in caplog.records if r.name == HOOK_LOGGER]
    assert not [r for r in hook_records if r.levelno > logging.DEBUG]
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in hook_records)


def test_log_context_write_error_fails_open_at_warning(tmp_path, monkeypatch, caplog):
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        code = _run_log_context(tree, tmp_path)
    assert code == 0
    assert any(
        r.levelno == logging.WARNING and "log-context" in r.getMessage()
        for r in caplog.records
        if r.name == HOOK_LOGGER
    )


HOOK_LOGGER = "shipit.hook"


def _clone_shape(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".shipit.toml").write_text("[secrets]\n")
    return path


def _run_warning_check(cwd: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(cwd)})),
        stdout=out,
        environ={},
    )
    return code, out.getvalue()


def test_source_clone_cwd_warns_on_stdout(tmp_path, monkeypatch, caplog):
    clone = _clone_shape(tmp_path / "src-clone")
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        code, out = _run_warning_check(clone)
    assert code == 0
    assert out == sessionstart.SOURCE_CLONE_WARNING + "\n"
    assert "./agent-start claude" in out
    assert "./agent-start codex" in out
    assert any(
        r.levelno == logging.WARNING and r.name == HOOK_LOGGER for r in caplog.records
    )


def test_ephemeral_tree_cwd_is_silent(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _clone_shape(root / SESSION_LEAF)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    code, out = _run_warning_check(tree)
    assert code == 0
    assert out == ""


def test_run_tree_cwd_is_silent(tmp_path, monkeypatch):
    root = tmp_path / "trees"
    tree = _clone_shape(
        root / "repo-codex-20260101-000000-11111111-2222-4333-8444-555555555555"
    )
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    code, out = _run_warning_check(tree)
    assert code == 0
    assert out == ""


def test_non_clone_cwd_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    no_git = tmp_path / "no-git"
    no_git.mkdir()
    (no_git / ".shipit.toml").write_text("[secrets]\n")
    code, out = _run_warning_check(no_git)
    assert code == 0
    assert out == ""

    no_toml = tmp_path / "no-toml"
    no_toml.mkdir()
    (no_toml / ".git").mkdir()
    code, out = _run_warning_check(no_toml)
    assert code == 0
    assert out == ""


def test_detection_error_is_silent_and_debug(tmp_path, monkeypatch, caplog):
    clone = _clone_shape(tmp_path / "src-clone")
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/trees")
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        code, out = _run_warning_check(clone)
    assert code == 0
    assert out == ""
    hook_records = [r for r in caplog.records if r.name == HOOK_LOGGER]
    assert not [r for r in hook_records if r.levelno > logging.DEBUG]
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in hook_records)


def test_warning_never_suppresses_the_writes(tmp_path, monkeypatch, pixi_repo):
    (pixi_repo / ".git").mkdir()
    (pixi_repo / ".shipit.toml").write_text("[secrets]\n")
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    env_file = tmp_path / "claude-env"
    out = io.StringIO()
    code = sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(pixi_repo)})),
        stdout=out,
        environ={"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    assert sessionstart.SOURCE_CLONE_WARNING + "\n" in out.getvalue()
    assert "export CONDA_DEFAULT_ENV=shipit" in env_file.read_text()


def _session_started_records(caplog):
    from shipit import events

    return [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == "session.started"
    ]


def test_every_session_start_emits_session_started(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    with caplog.at_level(logging.INFO, logger="shipit.hook"):
        code = _run_log_context(cwd, tmp_path / "claude-env")
    assert code == 0
    (started,) = _session_started_records(caplog)
    assert started.levelno == logging.INFO
    assert not hasattr(started, "session_id")


def test_codex_session_start_persists_native_thread_id(tmp_path, monkeypatch, caplog):
    root = tmp_path / "trees"
    tree = _ephemeral_tree(
        root, leaf="repo-codex-20260711-121015-73781aaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    )
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    monkeypatch.setenv("CODEX_THREAD_ID", "019f-fresh-thread")

    with caplog.at_level(logging.INFO, logger="shipit.hook"):
        code = _run_log_context(tree, tmp_path / "codex-env")

    assert code == 0
    (started,) = _session_started_records(caplog)
    assert started.codex_thread == "019f-fresh-thread"
    assert not hasattr(started, "session_id")


def test_session_started_binds_the_ephemeral_session_scoped(tmp_path, monkeypatch):
    import structlog as _structlog

    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))

    seen: dict = {}
    real_emit = sessionstart.events.emit

    def spy(log, name, msg, *args, **kwargs):
        seen[name] = dict(logcontext.bound())
        return real_emit(log, name, msg, *args, **kwargs)

    _structlog.contextvars.clear_contextvars()
    monkeypatch.setattr(sessionstart.events, "emit", spy)
    code = _run_log_context(tree, tmp_path / "claude-env")
    assert code == 0
    assert seen["session.started"]["session"] == SESSION_ID
    assert seen["session.started"]["tree"] == str(tree.resolve())
    assert "session" not in logcontext.bound()


def test_session_started_emission_failure_is_fail_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        sessionstart.events,
        "emit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    with caplog.at_level(logging.DEBUG, logger="shipit.hook"):
        code = _run_log_context(cwd, tmp_path / "claude-env")
    assert code == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


GOOD_PIN = "c" * 40


def _stale_run(cwd, commits_ahead, payload=None):
    out = io.StringIO()
    rc = sessionstart.run(
        stdin=io.StringIO(json.dumps(payload or {"cwd": str(cwd)})),
        stdout=out,
        environ={},
        commits_ahead=commits_ahead,
    )
    assert rc == 0
    return out.getvalue()


def _pinned_repo(tmp_path, pin=GOOD_PIN):
    (tmp_path / ".shipit.toml").write_text(f'[shipit]\nversion = "{pin}"\n')
    return tmp_path


def test_stale_pin_emits_the_advisory_line(tmp_path):
    seen = {}

    def commits_ahead(repo, base, head):
        seen["args"] = (repo, base, head)
        return 7

    out = _stale_run(_pinned_repo(tmp_path), commits_ahead)
    assert f"pin {GOOD_PIN[:12]} is 7 commits behind shipit main" in out
    assert seen["args"] == (sessionstart.SHIPIT_REPO_SLUG, GOOD_PIN, "main")


def test_current_pin_is_silent(tmp_path):
    out = _stale_run(_pinned_repo(tmp_path), lambda *a: 0)
    assert "behind shipit main" not in out


def test_offline_read_is_silent(tmp_path):
    out = _stale_run(_pinned_repo(tmp_path), lambda *a: None)
    assert "behind" not in out


def test_exploding_read_fails_open_silently(tmp_path):
    def boom(*a):
        raise RuntimeError("gh exploded")

    out = _stale_run(_pinned_repo(tmp_path), boom)
    assert "behind" not in out


def test_pinless_repo_never_consults_the_network(tmp_path):
    calls = []
    out = _stale_run(tmp_path, lambda *a: calls.append(a) or 9)
    assert calls == []
    assert "behind" not in out


def test_non_sha_pin_is_pinless_for_the_advisory(tmp_path):
    calls = []
    out = _stale_run(
        _pinned_repo(tmp_path, pin="0.0.1"), lambda *a: calls.append(a) or 9
    )
    assert calls == []
    assert "behind" not in out


def test_one_commit_behind_singular_wording(tmp_path):
    out = _stale_run(_pinned_repo(tmp_path), lambda *a: 1)
    assert "is 1 commit behind" in out
    assert "1 commits" not in out


def _task_run(cwd):
    out = io.StringIO()
    rc = sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(cwd)})),
        stdout=out,
        environ={},
        commits_ahead=lambda *a: None,
    )
    assert rc == 0
    return out.getvalue()


def test_manifest_without_test_task_warns(tmp_path):
    (tmp_path / "pixi.toml").write_text('[tasks]\nlint = "shipit lint"\n')
    out = _task_run(tmp_path)
    assert "defines no `test` task" in out
    assert "POSIX `test` builtin" in out


def test_manifest_with_root_test_task_is_silent(tmp_path):
    (tmp_path / "pixi.toml").write_text('[tasks]\ntest = "pytest"\n')
    assert "defines no `test` task" not in _task_run(tmp_path)


def test_manifest_with_feature_test_task_is_silent(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        '[tasks]\nlint = "x"\n\n[feature.dev.tasks]\ntest = "cargo test"\n'
    )
    assert "defines no `test` task" not in _task_run(tmp_path)


def test_no_manifest_is_a_clean_noop_for_the_task_advisory(tmp_path):
    assert "defines no `test` task" not in _task_run(tmp_path)


def test_malformed_manifest_fails_open_silently(tmp_path):
    (tmp_path / "pixi.toml").write_text("not = valid = toml\n")
    assert "defines no `test` task" not in _task_run(tmp_path)
