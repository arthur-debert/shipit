"""The ``shipit`` CLI root — a thin click assembler.

``shipit`` is a slim binary with git-style subcommands (architecture.lex §4).
This module builds the root group and attaches each verb; a verb's real logic
lives in ``shipit.verbs.<name>``. ``main(argv) -> int`` is the entrypoint the
console-script and ``python -m shipit`` both call.
"""

from __future__ import annotations

import sys

import click

from . import __version__, logcontext
from .logsetup import configure_logging, reset_logging
from .verbs import gh_setup, install, lint, logs, verify_apps
from .verbs._context import resolve_root_context
from .verbs.eval import eval_group
from .verbs.hook import hook as hook_group
from .verbs.logevent import log as log_group
from .verbs.pr import pr as pr_group
from .verbs.spawn import spawn as spawn_group
from .verbs.tree import tree as tree_group


@click.group(
    help=(
        "shipit — portfolio standardization tooling.\n\n"
        "Provisioning, GitHub repo setup, lint, PR flow and release, on pixi. "
        "`--help` is the map."
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="shipit")
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


# The `logs` reader (LOG01/LOG04, promoted onto the ADR-0030 contract in
# CLI02): the command, its query minting, and the renderers live in the verb
# module; the read engine in the `logread` domain package.
root.add_command(logs.logs_cmd)


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
