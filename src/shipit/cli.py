"""The ``shipit`` CLI root — a thin click assembler.

``shipit`` is a slim binary with git-style subcommands (architecture.lex §4).
This module builds the root group and attaches each verb; a verb's real logic
lives in ``shipit.verbs.<name>``. ``main(argv) -> int`` is the entrypoint the
console-script and ``python -m shipit`` both call.
"""

from __future__ import annotations

import logging
import os
import sys

import click

from . import __version__, buildid, events, logcontext
from .logsetup import configure_logging, reset_logging
from .verbs import build as build_verb
from .verbs import e2e as e2e_verb
from .verbs import gh_setup, install, lint, logs, verify_apps
from .verbs import test as test_verb
from .verbs._context import resolve_root_context
from .verbs.changelog import changelog as changelog_group
from .verbs.eval import eval_group
from .verbs.hook import hook as hook_group
from .verbs.logevent import log as log_group
from .verbs.pr import pr as pr_group
from .verbs.provision import provision as provision_group
from .verbs.spawn import spawn as spawn_group
from .verbs.tree import tree as tree_group
from .verbs.wf import wf as wf_group

#: The CLI entry's own logger — carries the SHIPIT_EXEC announcement's durable
#: twin (ADR-0033) through the LOG01 pipeline like any subsystem logger.
logger = logging.getLogger("shipit.cli")

#: Shown for the build sha when :func:`shipit.buildid.build_sha` resolves
#: nothing — no install record, no build-time embed, no source checkout. The
#: static package version alone "identifies nothing" (ADR-0033), so say so
#: plainly rather than printing a bare version that pretends to.
_UNKNOWN_BUILD = "unknown (not a tracked build)"


def version_string() -> str:
    """The ``shipit --version`` line, carrying the running build's commit.

    ``shipit`` releases as a single ``0.0.1`` package version; the load-bearing
    identity is the git commit of the build actually running (ADR-0033), which
    :func:`shipit.buildid.build_sha` resolves. Surfacing it here lets an
    operator answer "WHICH build is this?" — the introspection gap that made
    ADP01-WS01's stamped-pin investigation guesswork. Degrades to
    :data:`_UNKNOWN_BUILD` (never raises) when no identity resolves.
    """
    sha = buildid.build_sha()
    build = sha.value if sha is not None else _UNKNOWN_BUILD
    return f"shipit {__version__} (build {build})"


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """Eager ``--version`` callback: print :func:`version_string` and exit.

    Hand-rolled instead of ``click.version_option`` so the build sha is resolved
    ONLY when ``--version`` is passed — never as import-time work on every
    ordinary invocation.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(version_string())
    ctx.exit()


@click.group(
    help=(
        "shipit — portfolio standardization tooling.\n\n"
        "Provisioning, GitHub repo setup, lint, PR flow and release, on pixi. "
        "`--help` is the map."
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_print_version,
    help="Show the version and running build's commit sha, then exit.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Raise the console log level so INFO/DEBUG detail appears.",
)
@click.pass_context
def root(ctx: click.Context, verbose: bool) -> None:
    """Root group; subcommands are attached below.

    Resolves the ambient identity ONCE per invocation (ADR-0030): the current
    checkout's ``WorkingDir`` (offline, origin-derived per ADR-0024) becomes the
    frozen ``RootContext`` on click's context, the single source shared params
    and verbs read instead of re-deriving identity. Resolution is best-effort —
    outside a checkout the context is empty and each verb decides whether that
    is fatal.

    The same resolution then configures logging before any subcommand runs, so
    every verb is covered: the quiet stderr console (raised by ``-v``), the CI
    sinks when in CI, and the durable per-repo file sink (skipped outside a
    checkout rather than failing).

    This is also the CLI-entry half of the domain-key context (ADR-0029): the
    resolved repo binds as the ``repo`` correlation key BEFORE logging setup, so
    every record of the run carries it — and so a parent-exported
    ``SHIPIT_LOG_CTX_*`` key (rebound inside ``configure_logging``, the child
    half of the seam) deliberately wins over this best-effort cwd resolution.
    """
    # Start from a clean slate: detach any sinks a prior in-process invocation
    # left attached, so identity resolution below runs quiet (its bootstrap
    # `exec` DEBUG records must not leak to a stale stderr sink) before
    # `configure_logging` re-wires this invocation's own. A no-op in a one-shot
    # production process; load-bearing when invocations share a process.
    reset_logging()
    root_ctx = resolve_root_context()
    ctx.obj = root_ctx
    repo = root_ctx.repo
    if repo is not None:
        logcontext.bind(repo=repo.slug)
    configure_logging(verbose=verbose, repo=repo)
    # The SHIPIT_EXEC announcement's durable half (ADR-0033): the managed
    # launcher already said it on stderr; the exec'd build — this process —
    # leaves the flow-log twin, so `shipit logs --flow` shows every run that
    # bypassed the repo's pin. Env-keyed (not launcher-keyed) on purpose: any
    # invocation running under the override is a pin bypass worth recording.
    shipit_exec = os.environ.get("SHIPIT_EXEC")
    if shipit_exec:
        events.emit(
            logger,
            "launcher.overridden",
            "running under SHIPIT_EXEC=%s — the repo's shipit pin is bypassed",
            shipit_exec,
            extra={"shipit_exec": shipit_exec},
        )


# `gh-setup` is ADR-0030 glue assembled in its own verb module (CLI02-WS04):
# click command + pure renderer there, the three passes in the shipit.ghsetup
# domain; attach the finished command like the nested groups below.
root.add_command(gh_setup.cmd)


@root.command(name="verify-apps")
@click.argument("repo", required=False)
@click.option(
    "--agent",
    "agents",
    multiple=True,
    type=click.Choice(verify_apps.known_agents()),
    help=(
        "Local-agent reviewer App to verify (repeatable). "
        "Default: every known App reviewer."
    ),
)
def verify_apps_cmd(repo: str | None, agents: tuple[str, ...]) -> None:
    """Verify each local-agent reviewer App is LIVE on REPO (installed + checks:write).

    REPO is owner/name; omitted, it defaults to the current checkout's repo. For
    each App (adr-codex-review / adr-agy-review) this mints the App installation
    token and checks the granted permissions carry `checks: write` — a cheap read,
    not a check-run create. Prints a pass-or-instruct line per App and exits 0 only
    when ALL are live, 1 otherwise, so a rollout can branch on it mechanically. It
    only VERIFIES; the one-time install/consent is per docs/dev/review-app-provisioning.md.
    """
    rc = verify_apps.run(repo, agents=list(agents) or None)
    raise SystemExit(rc)


# The install family is promoted onto the ADR-0030 contract (CLI02-WS01): its
# command lives with its renderers in verbs/install.py; the domain (plan/apply)
# is the shipit.install package.
root.add_command(install.cmd)


@root.command(name="lint")
@click.argument("path", required=False)
@click.option(
    "--fix",
    is_flag=True,
    help="Apply formatters in place (opt-in). Default is a check-only hard-fail check.",
)
def lint_cmd(path: str | None, fix: bool) -> None:
    """Run the standardized multi-language checks over the tree at PATH.

    PATH defaults to the current directory. The same invocation CI and the
    pre-commit hook run — one binary, one config. A missing tool fails the checks
    (they never skip); a clean tree exits 0, any failure exits 1.
    """
    rc = lint.run(path, fix=fix)
    raise SystemExit(rc)


@root.command(name="test", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def test_cmd(args: tuple[str, ...]) -> None:
    """Run this repo's test legs: `shipit test [LEG] [-- ARGS...]`.

    Walks the `.shipit.toml [toolchains]` path->toolchain map and dispatches
    each leg to its test-producing command (registry default per toolchain, or
    the entry's per-path override). Bare `shipit test` runs EVERY leg — the
    hooks' and CI's form. LEG selects one (a toolchain name, or a map path
    when one toolchain has several); args after `--` are forwarded verbatim to
    that leg's command (`shipit test rust -- --no-capture`) and require
    exactly one selected leg. Exit: 0 all legs pass, 1 any leg fails (a
    missing tool binary hard-fails, never skips), 2 usage.
    """
    raise SystemExit(test_verb.run(list(args)))


@root.command(name="build", context_settings={"ignore_unknown_options": True})
@click.option(
    "--version",
    "version",
    default=None,
    help="The release version to inject where a build target declares it "
    "(go's -ldflags -X, ADR-0041). Supplied, never computed; absent keeps "
    "the embedded default.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def build_cmd(version: str | None, args: tuple[str, ...]) -> None:
    """Run this repo's build legs: `shipit build [--version VERSION] [LEG] [-- ARGS...]`.

    Walks the `.shipit.toml [toolchains]` path->toolchain map and dispatches
    each build leg to its REAL builder (cargo / go build / uv build / the npm
    build script — pixi provisions, never builds): the registry default per
    toolchain, or the entry's per-path override, narrowed to the
    `[artifacts]` map's declared build targets when the repo declares any.
    Bare `shipit build` runs EVERY leg. LEG selects one (a toolchain name, or
    a map path); args after `--` are forwarded verbatim to that leg's builder
    (`shipit build npm -- --workspace web`) and require exactly one selected
    leg. `--version` supplies the release version injected where a go target
    declares its var (ADR-0041). Exit: 0 all steps build, 1 any step fails (a
    missing builder hard-fails, never skips), 2 usage.
    """
    raise SystemExit(build_verb.run(list(args), version=version))


<<<<<<< HEAD
@root.command(name="e2e", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def e2e_cmd(args: tuple[str, ...]) -> None:
    """Run this repo's declared e2e harnesses: `shipit e2e [ARTIFACT] [-- ARGS...]`.

    The artifact-consuming tool: for every `[artifacts.<name>]` declaring an
    `e2e` table, resolves the artifact's binary (locally built via the repo's
    build legs — the artifact-source seam's one source today), injects its
    absolute path into the declared harness (default: the repo's
    bin/check-e2e bats runner) as `<NAME>_BIN`, and runs the harness from the
    repo root. No e2e declaration means no e2e lane: reports and exits 0.
    ARTIFACT selects one declared artifact; args after `--` are forwarded
    verbatim to that harness and require exactly one selected artifact.
    Exit: 0 all harnesses pass, 1 any fails or its artifact can't be built,
    2 usage.
    """
    raise SystemExit(e2e_verb.run(list(args)))
=======
# The nested `changelog` group (TOL01-WS06) — the language-agnostic
# release-notes tool over CHANGELOG/ fragments: the PR-time fragment-sync
# `check` (the changelog-sync lane's run) and the cut-time `coalesce`.
root.add_command(changelog_group)
>>>>>>> refs/remotes/origin/TOL01_WS02


# The `logs` reader (LOG01/LOG04, promoted onto the ADR-0030 contract in
# CLI02): the command, its query minting, and the renderers live in the verb
# module; the read engine in the `logread` domain package.
root.add_command(logs.logs_cmd)


# The nested `provision` group (ADP00-WS03) — pinned external tools delivered
# by the binary (`shipit provision lexd`): the consumer's managed task line
# invokes it, so no provisioning script is ever distributed or reconciled.
root.add_command(provision_group)


# The nested `pr` group (PR flow) is a click.Group assembled in its own package
# (verbs/pr/), so its verbs register there rather than as inline commands here;
# attach the whole group to the root.
root.add_command(pr_group)

# The nested `hook` group (Claude Code lifecycle-hook entrypoints) — the binary
# side of the agent harness (ADR-0012); attached the same way as `pr`.
root.add_command(hook_group)

# The nested `eval` group (HAR02) — the READER side of the harness eval store the
# `hook` events write; `shipit eval report` aggregates it. Attached like `pr`.
root.add_command(eval_group)

# The nested `log` group (LOG04) — the constrained dev-cycle WRITE path:
# `shipit log event <name>` records a registered milestone (ADR-0032). The
# reader stays the flat `logs` verb above; write and read are separate verbs.
root.add_command(log_group)

# The nested `tree` group (TRE01) — isolated Trees: independent dissociated
# clones a write-session works in (ADR-0014). Attached like `pr`.
root.add_command(tree_group)

# The nested `spawn` group (TRE03) — shipit-owned subagent spawning: create a
# write Tree and launch a backend-agent Run rooted in it (ADR-0017/0019).
root.add_command(spawn_group)

# The nested `wf` group (TOL01-WS04) — workflow tools: `shipit wf test` runs
# one workflow/job under act in a container against a crafted event, so a
# workflow edit is validated locally before any push (PRD stories 40/41).
root.add_command(wf_group)


def main(argv: list[str] | None = None) -> int:
    """Build-and-run the click root, returning an int exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        root.main(args=args, prog_name="shipit", standalone_mode=False)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except click.exceptions.ClickException as exc:
        exc.show()
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
