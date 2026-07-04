"""Hook boundary + pure core: SessionStart → toolchain activation into CLAUDE_ENV_FILE.

The coordinator-activation seam (ADR-0027, SES01-WS01). Covers the three things the
slice owns: the PURE toolchain→activation mapping (pixi → export lines rendered from
pixi's `shell-hook --json` snapshot; non-pixi → empty), the manifest resolution from
the session's cwd, and the boundary's FAIL-OPEN contract (exit 0 always; a repo with
no activatable toolchain — or any error — writes nothing and never errors, because
activation is additive, never load-bearing).
"""

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
from shipit.session import liveness
from shipit.tree import layout
from shipit.verbs.hook import sessionstart

TREE_ROOT = "/trees/SES01/WS01"

# A faithful `pixi shell-hook --json` blob (mirrors tests/test_pixienv.py's fixture):
# the complete env-var snapshot pixi's activation produces, plus a value that NEEDS
# quoting so the export rendering is exercised end to end.
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
    """An `execrun.run`-shaped stub that records argv and returns the fixture JSON."""

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
    """A checkout with a pixi.toml at its root and a nested working dir."""
    (tmp_path / "pixi.toml").write_text('[project]\nname = "x"\n')
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------------------
# Pure core — the toolchain→activation mapping
# --------------------------------------------------------------------------


def test_pixi_toolchain_maps_to_export_lines():
    # pixi → the shell-hook snapshot rendered as pure `export` lines, in pixi's
    # order — sourceable as a preamble (no functions, nothing interactive).
    toolchain = activation.Toolchain(kind=activation.PIXI, manifest=Path("pixi.toml"))
    script = activation.activation_script(toolchain, parse_activation(SHELL_HOOK_JSON))
    assert script.splitlines() == [
        f"export PATH={TREE_ROOT}/.pixi/envs/default/bin:/usr/bin:/bin",
        f"export CONDA_PREFIX={TREE_ROOT}/.pixi/envs/default",
        "export CONDA_DEFAULT_ENV=shipit",
        "export PIXI_PROMPT='(shipit) '",  # embedded space+parens → quoted
    ]


def test_no_toolchain_maps_to_empty():
    # Non-pixi → the EMPTY script: the graceful-no-op half of the mapping.
    act = parse_activation(SHELL_HOOK_JSON)
    assert activation.activation_script(None, act) == ""
    assert activation.activation_script(None, None) == ""


def test_unknown_toolchain_kind_maps_to_empty():
    # The kind-keyed dispatch is the extension seam: an unmapped kind degrades to
    # the no-op script rather than guessing an activation.
    toolchain = activation.Toolchain(kind="npm", manifest=Path("package.json"))
    assert (
        activation.activation_script(toolchain, parse_activation(SHELL_HOOK_JSON)) == ""
    )


def test_export_lines_skip_non_identifier_keys():
    # A key that cannot be a shell identifier cannot become an `export` line;
    # it is dropped rather than written broken into a sourced preamble.
    act = Activation(
        environment_variables={"OK": "1", "BAD-KEY": "x", "2BAD": "y"},
        activation_scripts=(),
    )
    assert activation.export_lines(act) == "export OK=1"


def test_activation_scripts_are_not_rendered_their_env_effects_already_are():
    # pixi EXECUTES activation scripts while computing `shell-hook --json` and
    # folds their env effects into environment_variables (probed live, pixi 0.71:
    # a script's `export SCRIPT_VAR=…` appears in the JSON env map). So a
    # non-empty activation_scripts list changes NOTHING here: the exports already
    # match `pixi run`'s env, and the script paths themselves must never leak
    # into the sourced preamble (re-sourcing would double-apply them).
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
    # A value with quotes/spaces/expansions survives sourcing VERBATIM.
    act = Activation(
        environment_variables={"HOSTILE": "a 'b' $(rm -rf /) $HOME"},
        activation_scripts=(),
    )
    line = activation.export_lines(act)
    assert line == """export HOSTILE='a '"'"'b'"'"' $(rm -rf /) $HOME'"""


# --------------------------------------------------------------------------
# Manifest resolution — from the session's cwd
# --------------------------------------------------------------------------


def test_detect_toolchain_walks_up_to_the_manifest(pixi_repo):
    # Resolved from the session cwd like pixi's own discovery: a nested cwd still
    # finds the root pixi.toml.
    toolchain = activation.detect_toolchain(pixi_repo / "src" / "pkg")
    assert toolchain == activation.Toolchain(
        kind=activation.PIXI, manifest=(pixi_repo / "pixi.toml").resolve()
    )


def test_detect_toolchain_none_without_a_manifest(tmp_path):
    assert activation.detect_toolchain(tmp_path) is None


# --------------------------------------------------------------------------
# Boundary — fail-open, no-op without a toolchain, append-only writes
# --------------------------------------------------------------------------


def test_pixi_repo_activation_lands_in_the_env_file(pixi_repo, tmp_path):
    # The happy path: payload cwd → manifest → `pixi shell-hook --json` (default
    # env: no `--environment` flag) → export lines appended to CLAUDE_ENV_FILE.
    env_file = tmp_path / "claude-env"
    captured: dict = {}
    code = _run(
        {"hook_event_name": "SessionStart", "cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner(captured),
    )
    assert code == 0
    assert captured["cmd"][:3] == ["pixi", "shell-hook", "--json"]
    assert "--environment" not in captured["cmd"]  # default env
    assert str((pixi_repo / "pixi.toml").resolve()) in captured["cmd"]
    content = env_file.read_text()
    assert f"export CONDA_PREFIX={TREE_ROOT}/.pixi/envs/default\n" in content
    assert "export CONDA_DEFAULT_ENV=shipit" in content


def test_env_file_is_appended_never_clobbered(pixi_repo, tmp_path):
    # CLAUDE_ENV_FILE is a shared seam other SessionStart hooks write to; this
    # boundary owns only its own lines.
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
    # No activatable toolchain → exit 0, nothing written, pixi never invoked.
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
    # Without CLAUDE_ENV_FILE there is nowhere to write — no-op, pixi never runs.
    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no CLAUDE_ENV_FILE — pixi must not run")

    assert _run({"cwd": str(pixi_repo)}, {}, runner=exploding_runner) == 0


def test_malformed_payload_falls_back_to_process_cwd(pixi_repo, tmp_path, monkeypatch):
    # Hooks run in the project dir, so a garbage payload degrades to Path.cwd()
    # and activation still lands (fail-open never means fail-useless).
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
    # A pixi error (missing binary, solve failure, …) costs the session NOTHING:
    # exit 0, no partial write. Activation is additive, never load-bearing.
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
    """Make writes to ``env_file`` tear: half the text lands, then OSError.

    Simulates a mid-append failure (disk full, transient I/O error) so the
    rollback contract is exercised: the file must end up EXACTLY as it was.
    """
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
    # A write failing MID-append must not leave a truncated export line: the env
    # file is sourced before every Bash call, so torn content is worse than none.
    # Fail-open means the shared seam ends up byte-identical to before the hook.
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
    # Same tear, but the hook itself created the file: rollback removes it rather
    # than leaving a half-written preamble behind.
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
    # TOCTOU shape: the env file exists when the hook starts but is deleted
    # before the append. The single-stat existence check must not crash, and the
    # append recreates the file with the activation lines.
    env_file = tmp_path / "claude-env"
    env_file.write_text("export DOOMED=1\n")
    real_open = builtins.open

    def deleting_open(file, *args, **kwargs):
        if str(file) == str(env_file):
            monkeypatch.setattr(builtins, "open", real_open)
            env_file.unlink(missing_ok=True)  # vanish between stat() and open()
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
    # Even the final write failing must not surface: the env-file path is a
    # directory here, so open() raises — swallowed, exit 0.
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(tmp_path)},
        runner=_fake_runner({}),
    )
    assert code == 0


# --------------------------------------------------------------------------
# Liveness pidfile — the second additive write (SES02, ADR-0027)
# --------------------------------------------------------------------------

CREATED = 1_750_000_000.0

#: A realistic hook ancestry: shipit(300) <- pixi(200) <- claude(100). The claude
#: process is node-named — only the command line betrays it (the ADR's misread
#: guard: NEVER match the OS process name).
ANCESTRY = {
    300: liveness.ProcessInfo(
        pid=300, ppid=200, create_time=CREATED + 9, argv="python -m shipit hook"
    ),
    200: liveness.ProcessInfo(
        pid=200, ppid=100, create_time=CREATED + 8, argv="pixi run shipit hook"
    ),
    100: liveness.ProcessInfo(
        pid=100,
        ppid=1,
        create_time=CREATED,
        argv="node /x/@anthropic-ai/claude-code/cli.js -w sess-1",
    ),
}


@pytest.fixture
def clone(tmp_path):
    """A session-Tree shape: a dir whose .git is a directory."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _run_liveness(payload, *, probe=ANCESTRY.get, self_pid=300, env=None):
    return sessionstart.run(
        stdin=io.StringIO(json.dumps(payload)),
        environ=env if env is not None else {},
        probe=probe,
        self_pid=self_pid,
    )


def test_pidfile_records_the_claude_ancestor(clone):
    # The recorded PID is the claude ANCESTOR's (100), never the hook's own
    # (300); create-time is the ancestor's OS create-time read at write time.
    code = _run_liveness({"session_id": "sess-abc", "cwd": str(clone)})
    assert code == 0
    record = liveness.read_pidfile(clone)
    assert record == liveness.LivenessRecord(
        pid=100, session_id="sess-abc", create_time=CREATED
    )


def test_pidfile_lands_inside_dot_git_never_the_working_tree(clone):
    # In the working tree the pidfile would dirty the Tree forever and the gc
    # floor would never reclaim it.
    _run_liveness({"session_id": "s", "cwd": str(clone)})
    assert liveness.pidfile_path(clone).exists()
    assert list(p.name for p in clone.iterdir()) == [".git"]


def test_no_claude_ancestor_writes_no_pidfile(clone):
    # Launched outside any Claude session: the chain tops out with no claude.
    chain = {
        300: liveness.ProcessInfo(
            pid=300, ppid=1, create_time=CREATED, argv="/bin/zsh -l"
        ),
    }
    code = _run_liveness({"cwd": str(clone)}, probe=chain.get)
    assert code == 0
    assert liveness.read_pidfile(clone) is None


def test_non_clone_cwd_writes_no_pidfile(tmp_path):
    code = _run_liveness({"session_id": "s", "cwd": str(tmp_path)})
    assert code == 0
    assert not (tmp_path / ".git").exists()


def test_missing_session_id_degrades_to_empty(clone):
    _run_liveness({"cwd": str(clone)})
    record = liveness.read_pidfile(clone)
    assert record is not None
    assert record.session_id == ""


def test_probe_explosion_fails_open(clone):
    def boom(pid):
        raise RuntimeError("ps went away")

    code = _run_liveness({"cwd": str(clone)}, probe=boom)
    assert code == 0
    assert liveness.read_pidfile(clone) is None


# --------------------------------------------------------------------------
# Log-context export — session/tree keys for every in-session command
# (REL01 #349, ADR-0029)
# --------------------------------------------------------------------------

SESSION_LEAF = "sess-20260703-41649"


def _ephemeral_tree(root: Path, leaf: str = SESSION_LEAF) -> Path:
    """An ephemeral session-Tree dir under ``root`` (the path IS the signal)."""
    tree = root / "org" / "repo" / "ephemeral" / leaf
    tree.mkdir(parents=True)
    return tree


def _run_log_context(cwd: Path, env_file: Path) -> int:
    """Run the hook with the log-context check isolated from live boundaries:
    a runner that must not be reached unless a toolchain exists (none does in
    these bare dirs), and an empty ancestry so liveness no-ops."""

    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no toolchain — pixi must not run")

    return sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(cwd)})),
        environ={"CLAUDE_ENV_FILE": str(env_file)},
        runner=exploding_runner,
        probe={}.get,
        self_pid=1,
    )


def test_ephemeral_tree_cwd_exports_session_log_context(tmp_path, monkeypatch):
    # The seam the check exists for: a session Tree's dir leaf IS the per-launch
    # session id (ADR-0027) — the exact value tree/create.py binds at creation —
    # so the exported var joins in-session records to the birth records. The
    # names come from logcontext.ENV_PREFIX so writer and reader (bind_from_env
    # at configure_logging) agree by construction.
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(tree, env_file)
    assert code == 0
    lines = env_file.read_text().splitlines()
    assert f"export {logcontext.ENV_PREFIX}SESSION={SESSION_LEAF}" in lines
    assert (
        f"export {logcontext.ENV_PREFIX}TREE={shlex.quote(str(tree.resolve()))}"
        in lines
    )


def test_exported_session_id_round_trips_through_bind_from_env(tmp_path, monkeypatch):
    # End to end across the seam: the values the hook writes are exactly what a
    # child shipit process rebinds at logging setup — parse the export lines back
    # into an environment and let the reader half do its thing.
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
            "session": SESSION_LEAF,
            "tree": str(tree.resolve()),
        }
    finally:
        structlog.contextvars.clear_contextvars()


def test_non_tree_cwd_exports_no_log_context(tmp_path, monkeypatch):
    # A cwd outside the central root (a plain checkout, the source clone, …)
    # carries no session identity: nothing to export, clean DEBUG no-op — and
    # with no toolchain either, the env file is never even created.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    cwd = tmp_path / "plain-checkout"
    cwd.mkdir()
    env_file = tmp_path / "claude-env"
    code = _run_log_context(cwd, env_file)
    assert code == 0
    assert not env_file.exists()


def test_write_tree_cwd_exports_no_log_context(tmp_path, monkeypatch):
    # Under the central root but the WRONG kind: only the ephemeral kind's leaf
    # is a session id (a write Tree's leaf is branch-slug-hash), so the issue/
    # epic/branches namespaces must never mint a bogus session key.
    root = tmp_path / "trees"
    tree = root / "org" / "repo" / "issues" / "349" / "work-deadbeef"
    tree.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(tree, env_file)
    assert code == 0
    assert not env_file.exists()


def test_nested_ephemeral_dir_inside_a_tree_exports_no_log_context(
    tmp_path, monkeypatch
):
    # Under the central root, parent segment IS "ephemeral", but the depth is
    # wrong: a directory named ephemeral/ INSIDE a Tree's clone (a repo is free
    # to contain one) must not mint a bogus session key — only the minted shape
    # <root>/<org>/<repo>/ephemeral/<leaf> is a session Tree.
    root = tmp_path / "trees"
    nested = _ephemeral_tree(root) / "src" / "ephemeral" / "not-a-session"
    nested.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(nested, env_file)
    assert code == 0
    assert not env_file.exists()


def test_shallow_ephemeral_dir_exports_no_log_context(tmp_path, monkeypatch):
    # Same discriminator, other direction: <root>/ephemeral/<x> is too shallow
    # for the minted shape (no org/repo segments) — no session key.
    root = tmp_path / "trees"
    shallow = root / "ephemeral" / "not-a-session"
    shallow.mkdir(parents=True)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = _run_log_context(shallow, env_file)
    assert code == 0
    assert not env_file.exists()


def test_log_context_lands_alongside_the_activation_exports(tmp_path, monkeypatch):
    # The issue's mechanism verbatim: the log-context lines ride the SAME env
    # file as the pixi activation, appended after it (run order), so one sourced
    # preamble carries both the toolchain and the correlation keys.
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    (tree / "pixi.toml").write_text('[project]\nname = "x"\n')
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    env_file = tmp_path / "claude-env"
    code = sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(tree)})),
        environ={"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
        probe={}.get,
        self_pid=1,
    )
    assert code == 0
    content = env_file.read_text()
    assert "export CONDA_DEFAULT_ENV=shipit" in content
    session_line = f"export {logcontext.ENV_PREFIX}SESSION={SESSION_LEAF}"
    assert session_line in content
    assert content.index("CONDA_DEFAULT_ENV") < content.index(session_line)


def test_log_context_detection_error_is_silent_and_debug(tmp_path, monkeypatch, caplog):
    # A broken detection environment (a relative SHIPIT_TREES_ROOT, which
    # central_root() rejects) costs the session NOTHING and skips at DEBUG —
    # the same #348 calibration the source-clone check uses, since the two
    # share the path arithmetic and the every-start failure mode.
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
    # The WRITE half keeps the canon's WARNING (a swallowed append is degraded:
    # the session's records lose their correlation key): CLAUDE_ENV_FILE is a
    # directory here, so the append raises — swallowed, exit 0, WARNING logged.
    root = tmp_path / "trees"
    tree = _ephemeral_tree(root)
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        code = _run_log_context(tree, tmp_path)  # env "file" is a dir
    assert code == 0
    assert any(
        r.levelno == logging.WARNING and "log-context" in r.getMessage()
        for r in caplog.records
        if r.name == HOOK_LOGGER
    )


# --------------------------------------------------------------------------
# Source-clone warning — an independent, fail-open advisory check (REL01 #348)
# --------------------------------------------------------------------------

HOOK_LOGGER = "shipit.hook"


def _clone_shape(path: Path) -> Path:
    """Give ``path`` the two source-clone markers: .shipit.toml + a .git dir."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".shipit.toml").write_text("[secrets]\n")
    return path


def _run_warning_check(cwd: Path) -> tuple[int, str]:
    """Run the hook with the warning check isolated: no env file (activation
    no-ops before pixi), an empty ancestry (liveness no-ops before the pidfile)."""
    out = io.StringIO()
    code = sessionstart.run(
        stdin=io.StringIO(json.dumps({"cwd": str(cwd)})),
        stdout=out,
        environ={},
        probe={}.get,
        self_pid=1,
    )
    return code, out.getvalue()


def test_source_clone_cwd_warns_on_stdout(tmp_path, monkeypatch, caplog):
    # The launch the check exists for: claude started directly in the source
    # clone (has .shipit.toml, is a git repo, NOT under the central root). The
    # warning lands on stdout (→ session context) and a WARNING record rides
    # along as the durable trail.
    clone = _clone_shape(tmp_path / "src-clone")
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(tmp_path / "trees"))
    with caplog.at_level(logging.DEBUG, logger=HOOK_LOGGER):
        code, out = _run_warning_check(clone)
    assert code == 0
    assert out == sessionstart.SOURCE_CLONE_WARNING + "\n"
    assert any(
        r.levelno == logging.WARNING and r.name == HOOK_LOGGER for r in caplog.records
    )


def test_ephemeral_tree_cwd_is_silent(tmp_path, monkeypatch):
    # A session Tree is a clone of the same repo — it carries BOTH markers — but
    # it lives under the central root, so it must never warn (the no-false-
    # positives constraint). The branch is irrelevant: session Trees move off
    # ephemeral/* mid-session (work-by-branch), which is exactly why the
    # discriminator is the path.
    root = tmp_path / "trees"
    tree = _clone_shape(root / "org" / "repo" / "ephemeral" / "sess-20260703-1")
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    code, out = _run_warning_check(tree)
    assert code == 0
    assert out == ""


def test_branch_tree_cwd_is_silent(tmp_path, monkeypatch):
    # Same for a per-Run write Tree (branches/ namespace): under the root → silent.
    root = tmp_path / "trees"
    tree = _clone_shape(root / "org" / "repo" / "branches" / "spike-foo-deadbeef")
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, str(root))
    code, out = _run_warning_check(tree)
    assert code == 0
    assert out == ""


def test_non_clone_cwd_is_silent(tmp_path, monkeypatch):
    # Neither marker alone is a source clone: a shipit repo that is not a git
    # repo (no .git), and a git repo that is not a shipit repo (no .shipit.toml).
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
    # A broken detection environment (here: a relative SHIPIT_TREES_ROOT, which
    # central_root() rejects) costs the session NOTHING: exit 0, empty stdout,
    # and the swallow logs at DEBUG — #348's explicit calibration exception to
    # the WARNING canon, because the check writes nothing durable.
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
    # Independence: the warning firing must not cost the session its activation
    # (the pixi_repo here is a source clone too — .shipit.toml + .git + pixi.toml).
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
        probe={}.get,
        self_pid=1,
    )
    assert code == 0
    assert out.getvalue() == sessionstart.SOURCE_CLONE_WARNING + "\n"
    assert "export CONDA_DEFAULT_ENV=shipit" in env_file.read_text()


def test_liveness_write_survives_a_broken_activation(clone, tmp_path):
    # The two writes fail open INDEPENDENTLY: an unwritable env file must not
    # cost the session its liveness record.
    env_file = tmp_path / "no-such-dir" / "claude-env"

    def broken_runner(cmd, **kwargs):
        raise RuntimeError("pixi exploded")

    code = sessionstart.run(
        stdin=io.StringIO(json.dumps({"session_id": "s", "cwd": str(clone)})),
        environ={"CLAUDE_ENV_FILE": str(env_file)},
        runner=broken_runner,
        probe=ANCESTRY.get,
        self_pid=300,
    )
    assert code == 0
    assert liveness.read_pidfile(clone) is not None
