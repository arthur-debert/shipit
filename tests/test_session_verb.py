import io
import json
import logging
from dataclasses import dataclass, replace

from click.testing import CliRunner

from shipit.identity import Repo, repo_from_slug
from shipit.tree.create import Tree
from shipit.verbs import session
from shipit.verbs.hook import worktreecreate

#: Deterministic flat-leaf naming coordinates for the specs the launch paths mint
#: (ADR-0074): the backend binary <agent>, the <timestamp> stamp, and the full-UUID
#: <id>. `new_tree_naming` replaces the retired `new_agent_hash` — the naming half of
#: the spec is now three fields (agent/created/tree_id), not one hash on the dir leaf.
CREATED = "20260709-082101"
TREE_ID = "019f5115-fb40-7db2-a82f-d2fc02a1da22"


def _stub_naming(monkeypatch, module):
    """Pin ``module.new_tree_naming`` to deterministic coordinates.

    Preserves the caller's ``agent`` (the backend binary) and any explicit
    ``tree_id`` (the coordinator arm passes the harness session UUID), so the stub
    is faithful to each creation path's <id> provenance while the timestamp/id stay
    fixed for assertion.
    """
    monkeypatch.setattr(
        module,
        "new_tree_naming",
        lambda agent, *, tree_id=None: {
            "agent": agent,
            "created": CREATED,
            "tree_id": tree_id or TREE_ID,
        },
    )


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
    # ADR-0074: the Tree dir is the single flat leaf <repo>-<agent>-<timestamp>-<id>,
    # one segment below the root — no `ephemeral/` kind segment. The ephemeral session
    # id lives on the BRANCH now, not the dir.
    tree_path = tmp_path / "trees" / f"shipit-codex-{CREATED}-{TREE_ID}"

    monkeypatch.setattr(session.git, "repo_root", lambda: str(source))
    monkeypatch.setattr(
        session.identity,
        "resolve_repo",
        lambda root: Repo("arthur-debert", "shipit"),
    )
    _stub_naming(monkeypatch, session)
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
    # The naming half is now three flat-leaf fields: the backend binary <agent>, the
    # <timestamp> stamp, and the full-UUID <id> — no `agent_hash`.
    assert capture.spec.agent == session.bootstrap.CODEX.binary
    assert capture.spec.created == CREATED
    assert capture.spec.tree_id == TREE_ID
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
    _stub_naming(monkeypatch, session)
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


def test_run_codex_resume_redacts_prompt_from_surfaces(
    monkeypatch, tmp_path, capsys, caplog
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
    _stub_naming(monkeypatch, session)
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_creator(spec, *, source_repo):
        capture.spec = spec
        return Tree(path=str(tree_path), branch="ephemeral/codex-1", base="origin/main")

    def fake_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        capture.exec_file = file
        capture.argv = argv

    secret_prompt = "fix customer alpha's private bug"
    with caplog.at_level(logging.INFO, logger="shipit.session"):
        rc = session.run_codex(
            ["--model", "gpt-5"],
            resume_thread_id="019f-thread",
            prompt=secret_prompt,
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
        secret_prompt,
    ]
    launch = next(r for r in caplog.records if r.msg.startswith("launching codex"))
    assert secret_prompt not in launch.argv
    assert "<prompt:redacted>" in launch.argv
    assert launch.prompt_chars == len(secret_prompt)
    output = capsys.readouterr().out
    assert secret_prompt not in output
    assert "<prompt:redacted>" in output


def test_run_codex_resume_can_launch_from_explicit_source_repo(monkeypatch, tmp_path):
    capture = LaunchCapture()
    source = tmp_path / "source"
    tree_path = tmp_path / "tree"

    monkeypatch.setattr(session.git, "repo_root", lambda: None)
    _stub_naming(monkeypatch, session)
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


def test_run_resume_delegates_codex_target_to_codex_runner(tmp_path, monkeypatch):
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
        captured["context"] = session.logcontext.bound()
        return 0

    monkeypatch.setenv("SHIPIT_LOG_CTX_PR", "999")
    monkeypatch.setenv("SHIPIT_LOG_CTX_EPIC", "STALE01")

    def configure_logging(**kwargs):
        captured["logging_env"] = kwargs["env"]

    monkeypatch.setattr(session.logsetup, "configure_logging", configure_logging)
    session.logcontext.bind(pr=999, epic="STALE01")
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
    assert captured["context"] == {"repo": "arthur-debert/shipit"}
    assert "SHIPIT_LOG_CTX_PR" not in captured["logging_env"]
    assert "SHIPIT_LOG_CTX_EPIC" not in captured["logging_env"]


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


def test_resume_cli_last_preserves_backend_flags(monkeypatch):
    captured = {}

    def fake_run_resume(target, **kwargs):
        captured["target"] = target
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(session, "run_resume", fake_run_resume)

    result = CliRunner().invoke(
        session.resume_cmd,
        ["--last", "--repo", "arthur-debert/shipit", "--model", "opus"],
    )

    assert result.exit_code == 0
    assert captured["target"] is None
    assert captured["last"] is True
    assert captured["repo_identity"] == repo_from_slug("arthur-debert/shipit")
    assert captured["backend_args"] == ["--model", "opus"]


def test_resume_cli_last_rejects_an_explicit_target():
    result = CliRunner().invoke(
        session.resume_cmd,
        ["--last", "--repo", "arthur-debert/shipit", "codex-explicit"],
    )

    assert result.exit_code == 1
    assert "pass either a target or --last, not both" in result.output


def test_resume_cli_last_forwards_an_explicit_initial_backend_prompt(monkeypatch):
    captured = {}

    def fake_run_resume(target, **kwargs):
        captured["target"] = target
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(session, "run_resume", fake_run_resume)

    result = CliRunner().invoke(
        session.resume_cmd,
        [
            "--last",
            "--repo",
            "arthur-debert/shipit",
            "--prompt",
            "fix the bug",
        ],
    )

    assert result.exit_code == 0
    assert captured["target"] is None
    assert captured["backend_args"] == []
    assert captured["prompt"] == "fix the bug"


def test_resume_cli_last_rejects_a_native_id_as_an_explicit_target():
    result = CliRunner().invoke(
        session.resume_cmd,
        [
            "--last",
            "--repo",
            "arthur-debert/shipit",
            "019f5115-fb40-7db2-a82f-d2fc02a1da22",
        ],
    )

    assert result.exit_code == 1
    assert "pass either a target or --last, not both" in result.output


def test_run_codex_spec_matches_the_coordinator_worktreecreate_spec(
    monkeypatch, tmp_path
):
    # The parity pin (#631, ADR-0074): `run_codex` and the Claude coordinator fork of
    # the WorktreeCreate hook (`worktreecreate._create_tree(ephemeral=...)`) build the
    # SAME ephemeral TreeSpec SHAPE by shared construction only — no type or helper
    # links the two call sites. Under the flat grammar three fields legitimately differ
    # per path: `agent` (the backend binary — `codex` vs `claude`), `tree_id` (the
    # coordinator uses the harness session UUID from the payload; codex mints its own),
    # and `ephemeral` (each mints its own session id). Drive both paths and assert every
    # OTHER field agrees, so a future one-sided change — a slug, a session, a root, an
    # issue added to one path — fails here instead of silently forking the shape per host.
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
    _stub_naming(monkeypatch, session)
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
    _stub_naming(monkeypatch, worktreecreate)
    monkeypatch.setattr(worktreecreate, "create_from_source", creator("claude"))
    # The spike-verified coordinator launch payload: no `prompt_id` — the ephemeral
    # fork (ADR-0027). Under ADR-0074 it also carries the harness `session_id` (the
    # full UUID that becomes the flat dir's <id> — the resume handle); `name` is the
    # `--worktree` value that becomes the `ephemeral/<id>` BRANCH.
    payload = json.dumps(
        {
            "name": "sess-20260709-082101-4242",
            "session_id": "7f3c9d20-1a2b-4c3d-8e4f-56789abcdef0",
            "cwd": str(source),
        }
    )
    out = io.StringIO()
    assert worktreecreate.run(stdin=io.StringIO(payload), stdout=out) == 0

    codex_spec, claude_spec = specs["codex"], specs["claude"]
    # Both are the ephemeral shape, each carrying its own minted branch session id...
    assert codex_spec.ephemeral == "codex-20260709-082101-4242"
    assert claude_spec.ephemeral == "sess-20260709-082101-4242"
    # ...the coordinator arm names the dir after the HARNESS session UUID (the resume
    # handle), while codex mints its own; the <agent> is the backend binary either way.
    assert codex_spec.agent == session.bootstrap.CODEX.binary
    assert claude_spec.agent == "claude"
    assert claude_spec.tree_id == "7f3c9d20-1a2b-4c3d-8e4f-56789abcdef0"
    # ...and EVERY OTHER field is identical across the two hosts' paths — normalize the
    # three fields that legitimately differ per creation path, then compare the rest.
    normalized = dict(
        agent=claude_spec.agent,
        tree_id=claude_spec.tree_id,
        ephemeral=claude_spec.ephemeral,
    )
    assert replace(codex_spec, **normalized) == claude_spec


def test_run_claude_resume_execs_native_resume_through_worktree(
    monkeypatch, tmp_path, capsys, caplog
):
    capture = LaunchCapture()
    source = tmp_path / "source"
    repo = repo_from_slug("arthur-debert/shipit")

    _stub_naming(monkeypatch, session)
    monkeypatch.setattr(session.time, "time", lambda: 1783585261)
    monkeypatch.setattr(session.os, "getpid", lambda: 4242)

    def fake_chdir(path: str) -> None:
        capture.chdir = path

    def fake_execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        capture.exec_file = file
        capture.argv = argv
        capture.env = env

    secret_prompt = "summarize private customer alpha"
    stale_context = {
        session.logcontext.ENV_PREFIX + key.upper(): "stale"
        for key in session.logcontext.DOMAIN_KEYS
    }
    with caplog.at_level(logging.INFO, logger="shipit.session"):
        rc = session.run_claude_resume(
            "claude-native",
            ["--model", "opus"],
            repo_identity=repo,
            source_repo=str(source),
            prompt=secret_prompt,
            chdir=fake_chdir,
            execute=fake_execute,
            which=lambda binary: "/usr/local/bin/claude",
            environ={
                "PATH": "/bin",
                "PIXI_PROJECT_ROOT": "/stale/source",
                **stale_context,
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
        secret_prompt,
    ]
    assert capture.env is not None
    assert capture.env["PATH"] == "/bin"
    assert "PIXI_PROJECT_ROOT" not in capture.env
    for key in session.logcontext.DOMAIN_KEYS:
        assert session.logcontext.ENV_PREFIX + key.upper() not in capture.env
    launch = next(r for r in caplog.records if r.msg.startswith("launching claude"))
    assert secret_prompt not in launch.argv
    assert "<prompt:redacted>" in launch.argv
    assert launch.prompt_chars == len(secret_prompt)
    output = capsys.readouterr().out
    assert "claude session sess-20260709-082101-4242" in output
    assert secret_prompt not in output
    assert "<prompt:redacted>" in output


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
    _stub_naming(monkeypatch, session)
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
    _stub_naming(monkeypatch, session)
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
