from dataclasses import dataclass

from shipit.identity import Repo
from shipit.tree.create import Tree
from shipit.verbs import session


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
