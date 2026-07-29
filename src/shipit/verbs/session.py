"""``shipit session`` — coordinator-session bootstrap and resume verbs.

See docs/adr/0027-coordinator-session-tree-ephemeral.md.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence

import click

from .. import execrun, git, identity, logcontext, logsetup, workenv
from ..agent.backend import CLAUDE
from ..session import bootstrap, resume
from ..spawn.launch import scrub_tree_env
from ..tree.create import Tree, create_from_source, new_tree_naming
from ..tree.layout import TreeSpec
from ._errors import cli_errors
from ._params import REPO_SLUG

logger = logging.getLogger("shipit.session")


@click.group(
    name="session",
    help=(
        "Coordinator session bootstrap — launch an isolated, Tree-rooted "
        "top-level session, or resume one by durable session identity.\n\n"
        "Claude launches ride `./agent-start claude` (the --worktree hook seam); "
        "`session codex` is the Codex counterpart and provisions explicitly. "
        "`session resume` resolves shipit/native ids and dispatches to the right "
        "backend resume path. `--help` is the map."
    ),
)
def session() -> None:
    """Root of the ``session`` subcommand group; verbs are attached below."""


@session.command(name="codex", context_settings={"ignore_unknown_options": True})
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
def codex_cmd(codex_args: tuple[str, ...]) -> None:
    """Launch an interactive Codex coordinator session in a fresh session Tree."""
    args = list(codex_args)
    if len(args) >= 2 and args[0] == "resume":
        raise SystemExit(run_codex(args[2:], resume_thread_id=args[1]))
    raise SystemExit(run_codex(args))


@session.command(name="resume", context_settings={"ignore_unknown_options": True})
@click.option(
    "--last",
    is_flag=True,
    help="Resume the latest known session for --repo.",
)
@click.option(
    "--repo",
    "repo_identity",
    type=REPO_SLUG,
    default=None,
    help="Target repository as owner/name; required for --last and no-cwd use.",
)
@click.option(
    "--prompt",
    default=None,
    help="Intentional initial backend prompt; avoids ambiguity with a resume target.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def resume_cmd(
    last: bool,
    repo_identity: identity.Repo | None,
    prompt: str | None,
    args: tuple[str, ...],
) -> None:
    """Resume a coordinator session by shipit session id or backend-native id."""

    last_with_target = last and bool(args) and not args[0].startswith("-")
    target = args[0] if last_with_target or (not last and args) else None
    backend_args = args[1:] if target is not None else args
    raise SystemExit(
        run_resume(
            target,
            last=last,
            repo_identity=repo_identity,
            backend_args=list(backend_args),
            prompt=prompt,
        )
    )


@cli_errors
def run_resume(
    target: str | None,
    *,
    last: bool = False,
    repo_identity: identity.Repo | None = None,
    backend_args: Sequence[str] = (),
    prompt: str | None = None,
    resolver: Callable[..., resume.ResumeTarget] = resume.resolve,
    source_locator: Callable[..., str] = resume.source_checkout_for_repo,
    codex_runner: Callable[..., int] | None = None,
    claude_runner: Callable[..., int] | None = None,
) -> int:
    """Resolve a backend-neutral resume target and launch the matching backend."""

    resolved = resolver(target, repo=repo_identity, last=last)
    logcontext.unbind(*logcontext.DOMAIN_KEYS)
    logcontext.bind(repo=resolved.repo.slug)
    logsetup.configure_logging(
        repo=resolved.repo,
        env=logcontext.scrub_env(os.environ),
    )
    source_repo = source_locator(resolved.repo)

    if resolved.backend == resume.CODEX_BACKEND:
        runner = codex_runner or run_codex
        return runner(
            backend_args,
            resume_thread_id=resolved.native_session_id,
            resumed_session_id=resolved.shipit_session_id,
            repo_identity=resolved.repo,
            source_repo=source_repo,
            prompt=prompt,
        )
    if resolved.backend == resume.CLAUDE_BACKEND:
        runner = claude_runner or run_claude_resume
        return runner(
            resolved.native_session_id,
            backend_args,
            repo_identity=resolved.repo,
            source_repo=source_repo,
            resumed_session_id=resolved.shipit_session_id,
            prompt=prompt,
        )
    raise resume.ResumeError(f"unsupported backend {resolved.backend!r}")


def run_codex(
    codex_args: Sequence[str],
    *,
    resume_thread_id: str | None = None,
    resumed_session_id: str | None = None,
    prompt: str | None = None,
    repo_identity: identity.Repo | None = None,
    source_repo: str | None = None,
    creator: Callable[..., Tree] = create_from_source,
    chdir: Callable[[str], None] = os.chdir,
    execute: Callable[[str, list[str], dict[str, str]], None] = os.execvpe,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
    activation_runner: Callable[..., execrun.ExecResult] = execrun.run,
) -> int:
    """Create the ephemeral Tree and exec codex in it; returns only on failure."""
    root = source_repo if source_repo is not None else git.repo_root()
    if not root:
        print("session codex: not inside a git checkout", file=sys.stderr)
        return 1
    if which(bootstrap.CODEX.binary) is None:
        print(
            "session codex: the codex CLI is not on PATH — install Codex first.",
            file=sys.stderr,
        )
        return 127
    session_id = bootstrap.mint_session_id(now=time.time(), pid=os.getpid())
    try:
        spec = TreeSpec(
            repo=repo_identity or identity.resolve_repo(root),
            **new_tree_naming(bootstrap.CODEX.binary),
            ephemeral=session_id,
        )
        tree = creator(spec, source_repo=root)
    except (ValueError, execrun.ExecError, OSError) as exc:
        logger.error("session codex: tree creation failed", exc_info=True)
        print(f"session codex: {exc}", file=sys.stderr)
        return 1

    argv = (
        bootstrap.codex_resume_argv(tree.path, resume_thread_id, codex_args)
        if resume_thread_id is not None
        else bootstrap.codex_argv(tree.path, codex_args)
    )
    if prompt is not None:
        argv = [*argv, prompt]
    display_argv = _display_argv(argv, prompt=prompt)
    try:
        activation = bootstrap.activation_for_tree(
            tree.path,
            runner=activation_runner,
        )
    except (execrun.ExecError, ValueError, OSError) as exc:
        logger.warning(
            "session codex: pixi activation failed open; launching unactivated",
            exc_info=True,
        )
        print(f"session codex: activation skipped: {exc}", file=sys.stderr)
        activation = None
    env = bootstrap.codex_env(
        os.environ if environ is None else environ,
        session_id=session_id,
        tree=tree.path,
        activation=activation,
    )
    print(bootstrap.format_launch(session_id, tree.path, display_argv), flush=True)
    with logcontext.scoped(session=session_id, tree=tree.path):
        session_env = workenv.resolve_session_env(
            repo=spec.repo,
            tree_path=tree.path,
            branch=tree.branch,
            base=tree.base,
            activation=activation,
        )
        logger.info(
            "session codex: work env resolved — %s routing for coordinator session",
            session_env.routing.value,
            extra=workenv.resolution_record(
                session_env,
                boundary="session.codex-launch",
                role="coordinator",
                extra={"backend": resume.CODEX_BACKEND},
            ),
        )
        logger.info(
            "launching codex coordinator session %s in %s",
            session_id,
            tree.path,
            extra={
                "argv": shlex.join(display_argv),
                **({"prompt_chars": len(prompt)} if prompt is not None else {}),
                "backend": resume.CODEX_BACKEND,
                **(
                    {"resumed_session": resumed_session_id}
                    if resumed_session_id
                    else {}
                ),
                **({"codex_thread": resume_thread_id} if resume_thread_id else {}),
            },
        )
    try:
        chdir(tree.path)
    except OSError as exc:
        logger.error("session codex: could not enter Tree", exc_info=True)
        print(
            f"session codex: could not enter Tree {tree.path!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        execute(argv[0], argv, env)
    except OSError as exc:
        logger.error("session codex: exec failed", exc_info=True)
        print(f"session codex: could not exec {argv[0]!r}: {exc}", file=sys.stderr)
        return 1
    return 0


def run_claude_resume(
    native_session_id: str,
    claude_args: Sequence[str],
    *,
    repo_identity: identity.Repo,
    source_repo: str,
    resumed_session_id: str | None = None,
    prompt: str | None = None,
    chdir: Callable[[str], None] = os.chdir,
    execute: Callable[[str, list[str], dict[str, str]], None] = os.execvpe,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Exec Claude's native resume through the WorktreeCreate session-Tree seam."""

    if which(CLAUDE.binary) is None:
        print(
            "session resume: the claude CLI is not on PATH — install Claude Code first.",
            file=sys.stderr,
        )
        return 127
    session_id = (
        f"sess-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(time.time()))}-{os.getpid()}"
    )
    argv = [
        CLAUDE.binary,
        "--worktree",
        session_id,
        "--resume",
        native_session_id,
        *claude_args,
    ]
    if prompt is not None:
        argv.append(prompt)
    display_argv = _display_argv(argv, prompt=prompt)
    env = _claude_resume_env(os.environ if environ is None else environ, session_id)
    print(
        _format_claude_resume_launch(session_id, display_argv),
        flush=True,
    )
    with logcontext.scoped(session=session_id):
        logger.info(
            "launching claude coordinator session %s for resume",
            session_id,
            extra={
                "argv": shlex.join(display_argv),
                **({"prompt_chars": len(prompt)} if prompt is not None else {}),
                "backend": resume.CLAUDE_BACKEND,
                "session_id": native_session_id,
                **(
                    {"resumed_session": resumed_session_id}
                    if resumed_session_id
                    else {}
                ),
                "repo": repo_identity.slug,
            },
        )
    try:
        chdir(source_repo)
    except OSError as exc:
        logger.error("session resume: could not enter source checkout", exc_info=True)
        print(
            f"session resume: could not enter source checkout {source_repo!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        execute(argv[0], argv, env)
    except OSError as exc:
        logger.error("session resume: exec failed", exc_info=True)
        print(f"session resume: could not exec {argv[0]!r}: {exc}", file=sys.stderr)
        return 1
    return 0


def _claude_resume_env(
    parent_env: Mapping[str, str], session_id: str
) -> dict[str, str]:
    """Scrub stale Tree/log identity and export the minted session id."""

    env = scrub_tree_env(dict(parent_env))
    env = logcontext.scrub_env(env)
    env[logcontext.ENV_PREFIX + "SESSION"] = session_id
    return env


def _display_argv(argv: Sequence[str], *, prompt: str | None) -> list[str]:
    """Argv safe for scrollback/logging while execution keeps the real prompt."""

    return [*argv[:-1], "<prompt:redacted>"] if prompt is not None else list(argv)


def _format_claude_resume_launch(session_id: str, argv: Sequence[str]) -> str:
    """Human scrollback line-set before Claude takes over the terminal."""

    return f"claude session {session_id}\nexec {shlex.join(list(argv))}"
