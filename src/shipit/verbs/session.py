"""``shipit session`` — coordinator-session bootstrap/resume verbs.

The command group for launching the coordinator's OWN isolated session — the one
Tree ``shipit spawn subagent`` structurally cannot mint (it provisions Trees for
Runs the coordinator launches, not for the session itself). A fresh Claude Code
launch needs no shipit verb here: its cwd is fixed before any shipit code runs,
so its session Tree rides the ``--worktree`` pre-launch seam (the
``WorktreeCreate`` hook + ``agent-start claude``, ADR-0027). Codex has no such
seam — but shipit launches the codex process itself,
so ``shipit session codex`` CAN provision first and exec second, which is exactly
what it does (issue #604):

mint the per-launch session id → create the ephemeral session Tree
(``ephemeral/<id>`` off ``origin/main``, the SAME pure planner + orchestrator the
WorktreeCreate hook uses — never a parallel Tree implementation) → ``chdir`` into
it → ``execvpe`` interactive ``codex --cd <tree>`` in the low-friction coordinator
posture, with the session-identity env exports riding along.

The verb is thin (ADR-0030): the launch contract — id grammar, argv posture, env
scrubs/exports — is the pure core in :mod:`shipit.session.bootstrap`; this module
holds click glue, the effectful seams (Tree creation, ``chdir``/``exec``), and the
exit mapping. The managed ``./agent-start codex`` launcher (and its
``./codex-start`` compatibility shim, both laid down by ``shipit install``)
is a thin alias onto this verb.

The backend-neutral ``shipit session resume`` surface sits alongside those
launch paths. It resolves a human-facing shipit session id, backend-native id, or
``--last --repo`` from durable shipit JSONL records, then deliberately drops back
to each backend's own resume contract: Codex provisions a fresh Tree explicitly
and execs ``codex resume --cd <tree> …``; Claude execs
``claude --worktree <new-session> --resume …`` from a deterministic source
checkout so the WorktreeCreate hook remains the Tree creator.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import click

from .. import execrun, git, identity, logcontext, logsetup
from ..agent.backend import CLAUDE
from ..session import bootstrap, resume
from ..spawn.launch import scrub_tree_env
from ..tree.create import Tree, create_from_source, new_agent_hash
from ..tree.layout import TreeSpec, plan
from ._errors import cli_errors
from ._params import REPO_SLUG

#: The session axis' logger (ADR-0029): the launch narrates its milestone here —
#: the exec replaces this process, so the record written BEFORE it is the durable
#: trace that this session id/Tree pair was launched at all.
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
    """Launch an interactive Codex coordinator session in a fresh session Tree.

    Mints a recognizable per-launch session id (``codex-<utc-stamp>-<pid>``),
    provisions the central-root ephemeral Tree for it (branch ``ephemeral/<id>``,
    base ``origin/main`` — ADR-0027, the same Tree machinery every shape uses),
    then REPLACES this process with interactive ``codex --cd <tree>`` in the
    low-friction coordinator posture (unsandboxed — the Tree is the external
    isolation; ADR-0020 §codex). Auth is whatever codex already holds: the
    ChatGPT login stays first-class (the API-billing env keys are scrubbed,
    ``CODEX_ACCESS_TOKEN`` passes through). Extra CODEX_ARGS are forwarded to
    codex verbatim (``shipit session codex --model foo``).

    On success this command never returns — codex takes the terminal over.
    Exits 127 when the codex binary is missing from PATH; exits 1 (clean stderr
    message) when run outside a git checkout, when Tree creation fails, or when
    the codex binary cannot be exec'd.
    """
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
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def resume_cmd(
    last: bool, repo_identity: identity.Repo | None, args: tuple[str, ...]
) -> None:
    """Resume a coordinator session by shipit session id or backend-native id.

    The resolver reads durable shipit records to map the requested identity to a
    repository, backend, and backend-native conversation id. The actual launch
    remains backend-specific: Codex uses the existing re-rooted
    ``codex resume --cd <fresh-tree>`` path; Claude uses native
    ``claude --worktree <fresh-session> --resume <native-id>`` so the
    WorktreeCreate hook still provisions the Tree.
    """

    # One variadic argument avoids Click assigning the first unknown backend
    # flag to an optional ``target`` positional. Under ``--last`` every token
    # is backend argv; otherwise the first token is the requested identity.
    target = None if last or not args else args[0]
    backend_args = args if last else args[1:]
    raise SystemExit(
        run_resume(
            target,
            last=last,
            repo_identity=repo_identity,
            backend_args=list(backend_args),
        )
    )


@cli_errors
def run_resume(
    target: str | None,
    *,
    last: bool = False,
    repo_identity: identity.Repo | None = None,
    backend_args: Sequence[str] = (),
    resolver: Callable[..., resume.ResumeTarget] = resume.resolve,
    source_locator: Callable[..., str] = resume.source_checkout_for_repo,
    codex_runner: Callable[..., int] | None = None,
    claude_runner: Callable[..., int] | None = None,
) -> int:
    """Resolve a backend-neutral resume target and launch the matching backend.

    Returns only on launch failure; successful launches replace the process.
    ``resolver`` and the backend runners are injectable so tests assert the
    resolver precedence and argv/env contracts without starting real CLIs.
    """

    resolved = resolver(target, repo=repo_identity, last=last)
    logcontext.bind(repo=resolved.repo.slug)
    logsetup.configure_logging(repo=resolved.repo)
    source_repo = source_locator(resolved.repo)

    if resolved.backend == resume.CODEX_BACKEND:
        runner = codex_runner or run_codex
        return runner(
            backend_args,
            resume_thread_id=resolved.native_session_id,
            resumed_session_id=resolved.shipit_session_id,
            repo_identity=resolved.repo,
            source_repo=source_repo,
        )
    if resolved.backend == resume.CLAUDE_BACKEND:
        runner = claude_runner or run_claude_resume
        return runner(
            resolved.native_session_id,
            backend_args,
            repo_identity=resolved.repo,
            source_repo=source_repo,
            resumed_session_id=resolved.shipit_session_id,
        )
    raise resume.ResumeError(f"unsupported backend {resolved.backend!r}")


def run_codex(
    codex_args: Sequence[str],
    *,
    resume_thread_id: str | None = None,
    resumed_session_id: str | None = None,
    repo_identity: identity.Repo | None = None,
    source_repo: str | None = None,
    creator: Callable[..., Tree] = create_from_source,
    chdir: Callable[[str], None] = os.chdir,
    execute: Callable[[str, list[str], dict[str, str]], None] = os.execvpe,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
    activation_runner: Callable[..., execrun.ExecResult] = execrun.run,
) -> int:
    """Mint id → create the ephemeral Tree → chdir → exec codex. Returns only on failure.

    ``creator``/``chdir``/``execute``/``which``/``environ`` are the injectable
    effect seams (defaults: the real Tree orchestrator, ``os.chdir``,
    ``os.execvpe``, ``shutil.which``, ``os.environ``) so tests assert the whole
    launch contract — the
    :class:`TreeSpec` the Tree is minted from, the cwd handoff, the argv and env
    the exec receives — without cloning a repo or spawning codex. With the real
    seams a successful call never returns (``execvpe`` replaces the process);
    the ``return 0`` tail exists for injected non-replacing executors.

    Failure mapping (mirrors ``tree create``'s run): not-a-checkout, a rejected
    spec (``ValueError``), a git/provisioning Exec failure, a filesystem error,
    a missing ``codex`` binary (preflighted before provisioning, exit 127), and a
    failed exec each print one clean ``session codex: …`` line to stderr — plus
    the durable ERROR record for post-provisioning failures. No traceback for a
    known refusal.
    """
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
            agent_hash=new_agent_hash(),
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
    print(bootstrap.format_launch(session_id, tree.path, argv), flush=True)
    # The launch milestone, written BEFORE the exec replaces this process: the
    # last record this pid can leave, and the one that joins the session id/Tree
    # pair to the codex launch in the flow log. (The Tree's own birth already
    # emitted `tree.created` with the session bound — ADR-0027/0032.)
    with logcontext.scoped(session=session_id, tree=tree.path):
        logger.info(
            "launching codex coordinator session %s in %s",
            session_id,
            tree.path,
            extra={
                "argv": shlex.join(argv),
                "backend": resume.CODEX_BACKEND,
                "resumed_session": resumed_session_id,
                **({"codex_thread": resume_thread_id} if resume_thread_id else {}),
            },
        )
    # chdir FIRST: codex hook commands and child shells inherit the process cwd,
    # so it must agree with --cd's agent root — both point at the Tree.
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
    chdir: Callable[[str], None] = os.chdir,
    execute: Callable[[str, list[str], dict[str, str]], None] = os.execvpe,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Exec Claude's native resume through the WorktreeCreate session-Tree seam.

    Claude owns Tree provisioning for top-level sessions through
    ``--worktree``. This path therefore mints a new shipit session id, changes
    into a deterministic source checkout for the target repo, and execs
    ``claude --worktree <new-id> --resume <native-id>``. The hook then creates
    the fresh ephemeral Tree from ``origin/main`` and SessionStart preserves the
    usual Claude env-file, hook, permission, and guard behavior.
    """

    if which(CLAUDE.binary) is None:
        print(
            "session resume: the claude CLI is not on PATH — install Claude Code first.",
            file=sys.stderr,
        )
        return 127
    session_id = (
        f"sess-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(time.time()))}-{os.getpid()}"
    )
    spec = TreeSpec(
        repo=repo_identity, agent_hash=new_agent_hash(), ephemeral=session_id
    )
    expected_tree = plan(spec).dir
    argv = [
        CLAUDE.binary,
        "--worktree",
        session_id,
        "--resume",
        native_session_id,
        *claude_args,
    ]
    env = _claude_resume_env(os.environ if environ is None else environ)
    print(_format_claude_resume_launch(session_id, expected_tree, argv), flush=True)
    with logcontext.scoped(session=session_id, tree=str(expected_tree)):
        logger.info(
            "launching claude coordinator session %s for resume in %s",
            session_id,
            expected_tree,
            extra={
                "argv": shlex.join(argv),
                "backend": resume.CLAUDE_BACKEND,
                "session_id": native_session_id,
                "resumed_session": resumed_session_id,
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


def _claude_resume_env(parent_env: Mapping[str, str]) -> dict[str, str]:
    """Claude resume env: preserve Claude's session seams, scrub stale Tree identity."""

    env = scrub_tree_env(dict(parent_env))
    for key in ("ROLE", "AGENT", "RUN", "SESSION", "TREE"):
        env.pop(logcontext.ENV_PREFIX + key, None)
    return env


def _format_claude_resume_launch(
    session_id: str, tree: str | Path, argv: Sequence[str]
) -> str:
    """Human scrollback line-set before Claude takes over the terminal."""

    return f"claude session {session_id}\ntree {tree}\nexec {shlex.join(list(argv))}"
