"""``shipit session`` — coordinator-session bootstrap verbs (ADR-0027 / CDX01).

The command group for launching the coordinator's OWN isolated session — the one
Tree ``shipit spawn subagent`` structurally cannot mint (it provisions Trees for
Runs the coordinator launches, not for the session itself). Claude Code needs no
verb here: its cwd is fixed before any shipit code runs, so its session Tree rides
the ``--worktree`` pre-launch seam (the ``WorktreeCreate`` hook +
``agent-start claude``, ADR-0027). Codex has no such seam — but shipit launches the codex process itself,
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
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence

import click

from .. import execrun, git, identity, logcontext
from ..session import bootstrap
from ..tree.create import Tree, create_from_source, new_agent_hash
from ..tree.layout import TreeSpec

#: The session axis' logger (ADR-0029): the launch narrates its milestone here —
#: the exec replaces this process, so the record written BEFORE it is the durable
#: trace that this session id/Tree pair was launched at all.
logger = logging.getLogger("shipit.session")


@click.group(
    name="session",
    help=(
        "Coordinator session bootstrap — launch an isolated, Tree-rooted "
        "top-level session.\n\nClaude sessions ride `./agent-start claude` "
        "(the --worktree hook seam); `session codex` is the Codex counterpart: it "
        "provisions the ephemeral session Tree explicitly, then execs codex "
        "rooted in it. `--help` is the map."
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
    Exits 1 (clean stderr message) when run outside a git checkout, when Tree
    creation fails, or when the codex binary cannot be exec'd.
    """
    raise SystemExit(run_codex(list(codex_args)))


def run_codex(
    codex_args: Sequence[str],
    *,
    creator: Callable[..., Tree] = create_from_source,
    chdir: Callable[[str], None] = os.chdir,
    execute: Callable[[str, list[str], dict[str, str]], None] = os.execvpe,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
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
    root = git.repo_root()
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
            repo=identity.resolve_repo(root),
            agent_hash=new_agent_hash(),
            ephemeral=session_id,
        )
        tree = creator(spec, source_repo=root)
    except (ValueError, execrun.ExecError, OSError) as exc:
        logger.error("session codex: tree creation failed", exc_info=True)
        print(f"session codex: {exc}", file=sys.stderr)
        return 1

    argv = bootstrap.codex_argv(tree.path, codex_args)
    env = bootstrap.codex_env(
        os.environ if environ is None else environ,
        session_id=session_id,
        tree=tree.path,
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
            extra={"argv": argv},
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
