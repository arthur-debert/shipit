import io
import json
from dataclasses import dataclass, replace

from shipit.identity import Repo, repo_from_slug
from shipit.tree.create import Tree
from shipit.verbs import session
from shipit.verbs.hook import worktreecreate


@dataclass
class LaunchCapture:
    spec: object | None = None
    source_repo: object | None = None
    chdir: str | None = None
    exec_file: str | None = None
    argv: list[str] | None = None
    env: dict[str, str] | None = None


def test_run_codex_creates_ephemeral_tree_and_execs_codex(
    monkeypatch, tmp_path, capsys
):
    capture = LaunchCapture()
    source = tmp_path / "source"
    session_id = "codex-20260709-082101-4242"
    tree_path = (
        tmp_path / "trees" / "arthur-debert" / "shipit" / "ephemeral" / (session_id)
    )

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        session.identity,
        "resolve_repo",
        lambda root: Repo("arthur-debert", "shipit"),
    )
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        capture.spec = spec
        capture.source_repo = source_repo
        return Tree(
            path=str(tree_path), branch=f"ephemeral/{session_id}", base="origin/main"
        )

    def fake_chdir(path: str) -> None:
        capture.chdir = path

    def fake_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        capture.exec_file = file
        capture.argv = argv
        capture.env = env

    rc = session.run_codex(
        ["--model", "gpt-5"],
        creator=fake_creator,
        chdir=fake_chdir,
        execute=fake_execute,
        which=lambda binary: "/usr/local/bin/codex",
        environ={
            "PATH": "/bin",
            "OPENAI_API_KEY": "api-billed",
            "CODEX_ACCESS_TOKEN": "subscription-token",
            "PIXI_PROJECT_ROOT": str(source),
        },
    )

    assert rc == 0
    assert capture.spec is not None
    assert capture.spec.repo == Repo("arthur-debert", "shipit")
    assert capture.spec.agent_hash == "deadbeef"
    assert capture.spec.ephemeral == session_id
    assert capture.source_repo == str(source)
    assert capture.chdir == str(tree_path)
    assert capture.exec_file == "codex"
    assert capture.argv == [
        "codex",
        "--cd",
        str(tree_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "gpt-5",
    ]
    assert capture.env is not None
    assert capture.env["CODEX_ACCESS_TOKEN"] == "subscription-token"
    assert "OPENAI_API_KEY" not in capture.env
    assert "PIXI_PROJECT_ROOT" not in capture.env
    assert capture.env["SHIPIT_LOG_CTX_SESSION"] == session_id
    assert capture.env["SHIPIT_LOG_CTX_TREE"] == str(tree_path)
    assert f"codex session {session_id}" in capsys.readouterr().out


def test_run_codex_activates_the_tree_pixi_env(monkeypatch, tmp_path):
    capture = LaunchCapture()
    source = tmp_path / "source"
    tree_path = tmp_path / "tree"
    tree_path.mkdir()
    (tree_path / "pixi.toml").write_text("[workspace]\nname = 'x'\n")

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        session.identity,
        "resolve_repo",
        lambda root: Repo("arthur-debert", "shipit"),
    )
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        return Tree(path=str(tree_path), branch="ephemeral/codex-1", base="origin/main")

    def fake_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        capture.env = env

    def activation_runner(cmd, **kwargs):
        assert cmd[:3] == ["pixi", "shell-hook", "--json"]
        return type(
            "Result",
            (),
            {
                "stdout": json.dumps(
                    {
                        "environment_variables": {
                            "PATH": f"{tree_path}/.pixi/envs/default/bin:/bin",
                            "CONDA_PREFIX": f"{tree_path}/.pixi/envs/default",
                        },
                        "activation_scripts": [],
                    }
                )
            },
        )()

    rc = session.run_codex(
        [],
        creator=fake_creator,
        chdir=lambda path: None,
        execute=fake_execute,
        which=lambda binary: "/usr/local/bin/codex",
        environ={"PATH": "/bin"},
        activation_runner=activation_runner,
    )

    assert rc == 0
    assert capture.env is not None
    assert capture.env["PATH"] == f"{tree_path}/.pixi/envs/default/bin:/bin"
    assert capture.env["CONDA_PREFIX"] == f"{tree_path}/.pixi/envs/default"


def test_run_codex_resume_uses_first_class_resume_argv(monkeypatch, tmp_path):
    capture = LaunchCapture()
    source = tmp_path / "source"
    tree_path = tmp_path / "tree"

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        session.identity,
        "resolve_repo",
        lambda root: Repo("arthur-debert", "shipit"),
    )
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        capture.spec = spec
        return Tree(path=str(tree_path), branch="ephemeral/codex-1", base="origin/main")

    def fake_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        capture.exec_file = file
        capture.argv = argv

    rc = session.run_codex(
        ["--model", "gpt-5"],
        resume_thread_id="019f-thread",
        creator=fake_creator,
        chdir=lambda path: None,
        execute=fake_execute,
        which=lambda binary: "/usr/local/bin/codex",
        environ={"PATH": "/bin"},
    )

    assert rc == 0
    assert capture.exec_file == "codex"
    assert capture.argv == [
        "codex",
        "resume",
        "--cd",
        str(tree_path),
        "--dangerously-bypass-approvals-and-sandbox",
        "019f-thread",
        "--model",
        "gpt-5",
    ]


def test_run_codex_resume_can_launch_from_explicit_source_repo(monkeypatch, tmp_path):
    capture = LaunchCapture()
    source = tmp_path / "source"
    tree_path = tmp_path / "tree"

    monkeypatch.setattr(session.git, "repo_root", lambda: None)
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        capture.spec = spec
        capture.source_repo = source_repo
        return Tree(path=str(tree_path), branch="ephemeral/codex-1", base="origin/main")

    rc = session.run_codex(
        [],
        resume_thread_id="019f-thread",
        repo_identity=Repo("arthur-debert", "shipit"),
        source_repo=str(source),
        creator=fake_creator,
        chdir=lambda path: None,
        execute=lambda file, argv, env: setattr(capture, "argv", argv),
        which=lambda binary: "/usr/local/bin/codex",
        environ={"PATH": "/bin"},
    )

    assert rc == 0
    assert capture.source_repo == str(source)
    assert capture.spec.repo == Repo("arthur-debert", "shipit")
    assert capture.argv[:5] == [
        "codex",
        "resume",
        "--cd",
        str(tree_path),
        session.bootstrap.BYPASS_FLAG,
    ]


def test_run_resume_delegates_codex_target_to_codex_runner(tmp_path):
    captured = {}
    repo = repo_from_slug("arthur-debert/shipit")
    target = session.resume.ResumeTarget(
        repo=repo,
        backend="codex",
        shipit_session_id="codex-1",
        native_session_id="019f-thread",
    )

    def resolver(raw, **kwargs):
        captured["resolver"] = (raw, kwargs)
        return target

    def source_locator(repo):
        captured["source_repo"] = repo
        return str(tmp_path / "source")

    def codex_runner(args, **kwargs):
        captured["codex"] = (list(args), kwargs)
        return 0

    rc = session.run_resume(
        "codex-1",
        backend_args=["--model", "gpt-5"],
        resolver=resolver,
        source_locator=source_locator,
        codex_runner=codex_runner,
    )

    assert rc == 0
    assert captured["resolver"][0] == "codex-1"
    assert captured["source_repo"] == repo
    args, kwargs = captured["codex"]
    assert args == ["--model", "gpt-5"]
    assert kwargs["resume_thread_id"] == "019f-thread"
    assert kwargs["resumed_session_id"] == "codex-1"
    assert kwargs["repo_identity"] == repo


def test_run_resume_delegates_claude_target_to_claude_runner(tmp_path):
    captured = {}
    target = session.resume.ResumeTarget(
        repo=repo_from_slug("arthur-debert/shipit"),
        backend="claude",
        shipit_session_id="sess-1",
        native_session_id="claude-native",
    )

    def claude_runner(native_id, args, **kwargs):
        captured["claude"] = (native_id, list(args), kwargs)
        return 0

    rc = session.run_resume(
        "sess-1",
        backend_args=["--model", "opus"],
        resolver=lambda raw, **kwargs: target,
        source_locator=lambda repo: str(tmp_path / "source"),
        claude_runner=claude_runner,
    )

    assert rc == 0
    native_id, args, kwargs = captured["claude"]
    assert native_id == "claude-native"
    assert args == ["--model", "opus"]
    assert kwargs["resumed_session_id"] == "sess-1"


def test_run_codex_spec_matches_the_coordinator_worktreecreate_spec(
    monkeypatch, tmp_path
):
    # The parity pin (#631): `run_codex` and the Claude coordinator fork of the
    # WorktreeCreate hook (`worktreecreate._create_tree(ephemeral=...)`) build
    # the SAME ephemeral TreeSpec shape today by shared construction only — no
    # type or helper links the two call sites. Drive both paths with identical
    # identity/hash seams and assert the specs agree field-for-field (each
    # mints its own session id), so a future one-sided field change — a slug,
    # a session, a root added to one path — fails here instead of silently
    # forking the session-Tree shape per host.
    specs: dict[str, object] = {}
    source = tmp_path / "source"
    repo = repo_from_slug("arthur-debert/shipit")

    def creator(key):
        def create(spec, *, source_repo):
            specs[key] = spec
            return Tree(
                path=str(tmp_path / key),
                branch=f"ephemeral/{spec.ephemeral}",
                base="origin/main",
            )

        return create

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(session.identity, "resolve_repo", lambda root: repo)
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)
    rc = session.run_codex(
        [],
        creator=creator("codex"),
        chdir=lambda path: None,
        execute=lambda file, argv, env: None,
        which=lambda binary: "/usr/local/bin/codex",
        environ={"PATH": "/bin"},
    )
    assert rc == 0

    monkeypatch.setattr(worktreecreate.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        worktreecreate.identity, "resolve_repo", lambda cwd=".", **kw: repo
    )
    monkeypatch.setattr(worktreecreate, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(worktreecreate, "create_from_source", creator("claude"))
    # The spike-verified coordinator launch payload: no `prompt_id` — the
    # ephemeral fork (ADR-0027), exactly what `agent-start claude` produces.
    payload = json.dumps({"name": "sess-20260709-082101-4242", "cwd": str(source)})
    out = io.StringIO()
    assert worktreecreate.run(stdin=io.StringIO(payload), stdout=out) == 0

    codex_spec, claude_spec = specs["codex"], specs["claude"]
    # Both are the ephemeral shape, each carrying its own minted session id...
    assert codex_spec.ephemeral == "codex-20260709-082101-4242"
    assert claude_spec.ephemeral == "sess-20260709-082101-4242"
    # ...and EVERY other field is identical across the two hosts' paths.
    assert replace(codex_spec, ephemeral=claude_spec.ephemeral) == claude_spec


def test_run_claude_resume_execs_native_resume_through_worktree(
    monkeypatch, tmp_path, capsys
):
    capture = LaunchCapture()
    source = tmp_path / "source"
    repo = repo_from_slug("arthur-debert/shipit")

    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_chdir(path: str) -> None:
        capture.chdir = path

    def fake_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        capture.exec_file = file
        capture.argv = argv
        capture.env = env

    rc = session.run_claude_resume(
        "claude-native",
        ["--model", "opus"],
        repo_identity=repo,
        source_repo=str(source),
        chdir=fake_chdir,
        execute=fake_execute,
        which=lambda binary: "/usr/local/bin/claude",
        environ={
            "PATH": "/bin",
            "PIXI_PROJECT_ROOT": "/stale/source",
            "SHIPIT_LOG_CTX_SESSION": "old",
            "SHIPIT_LOG_CTX_TREE": "/old/tree",
        },
    )

    assert rc == 0
    assert capture.chdir == str(source)
    assert capture.exec_file == "claude"
    assert capture.argv == [
        "claude",
        "--worktree",
        "sess-20260709-082101-4242",
        "--resume",
        "claude-native",
        "--model",
        "opus",
    ]
    assert capture.env is not None
    assert capture.env["PATH"] == "/bin"
    assert "PIXI_PROJECT_ROOT" not in capture.env
    assert "SHIPIT_LOG_CTX_SESSION" not in capture.env
    assert "SHIPIT_LOG_CTX_TREE" not in capture.env
    assert "claude session sess-20260709-082101-4242" in capsys.readouterr().out


def test_run_codex_refuses_outside_git_checkout(monkeypatch, capsys):
    monkeypatch.setattr(session.git, "repo_root", lambda: None)

    rc = session.run_codex([], creator=lambda *a, **k: None)

    assert rc == 1
    assert "session codex: not inside a git checkout" in capsys.readouterr().err


def test_run_codex_refuses_missing_codex_before_creating_tree(
    monkeypatch, tmp_path, capsys
):
    source = tmp_path / "source"
    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))

    def creator_should_not_run(*args, **kwargs):
        raise AssertionError("tree creation should not run when codex is missing")

    rc = session.run_codex(
        [], creator=creator_should_not_run, which=lambda binary: None
    )

    assert rc == 127
    assert "session codex: the codex CLI is not on PATH" in capsys.readouterr().err


def test_run_codex_reports_chdir_failure_separately(monkeypatch, tmp_path, capsys):
    source = tmp_path / "source"
    tree_path = tmp_path / "tree"

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        session.identity,
        "resolve_repo",
        lambda root: Repo("arthur-debert", "shipit"),
    )
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        return Tree(path=str(tree_path), branch="ephemeral/codex-1", base="origin/main")

    def broken_chdir(path: str) -> None:
        raise OSError("missing tree")

    def execute_should_not_run(file: str, argv: list[str], env: dict[str, str]) -> None:
        raise AssertionError("exec should not run after chdir failure")

    rc = session.run_codex(
        [],
        creator=fake_creator,
        chdir=broken_chdir,
        execute=execute_should_not_run,
        which=lambda binary: "/usr/local/bin/codex",
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "session codex: could not enter Tree" in err
    assert "could not exec" not in err


def test_run_codex_reports_exec_failure_after_successful_chdir(
    monkeypatch, tmp_path, capsys
):
    capture = LaunchCapture()
    source = tmp_path / "source"
    tree_path = tmp_path / "tree"

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        session.identity,
        "resolve_repo",
        lambda root: Repo("arthur-debert", "shipit"),
    )
    monkeypatch.setattr(session, "new_agent_hash", lambda: "deadbeef")
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        return Tree(path=str(tree_path), branch="ephemeral/codex-1", base="origin/main")

    def fake_chdir(path: str) -> None:
        capture.chdir = path

    def broken_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        raise OSError("codex missing")

    rc = session.run_codex(
        [],
        creator=fake_creator,
        chdir=fake_chdir,
        execute=broken_execute,
        which=lambda binary: "/usr/local/bin/codex",
    )

    assert rc == 1
    assert capture.chdir == str(tree_path)
    err = capsys.readouterr().err
    assert "session codex: could not exec 'codex'" in err
    assert "could not enter Tree" not in err
