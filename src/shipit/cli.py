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


@root.command(name="gh-setup")
@click.argument("repo", required=False)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to .shipit.toml (default: the repo root's).",
)
@click.option(
    "--checks",
    "checks",
    default=None,
    help="Comma-separated required checks (skip auto-discovery).",
)
@click.option(
    "--dry-run", is_flag=True, help="Print what would change without sending it."
)
def gh_setup_cmd(
    repo: str | None, config_path: str | None, checks: str | None, dry_run: bool
) -> None:
    """Make REPO conform to the portfolio standard (ruleset, labels, secrets).

    REPO is owner/name; omitted, it defaults to the current checkout's repo.
    Idempotent — safe to re-run for both install and update.
    """
    checks_override = (
        [c.strip() for c in checks.split(",") if c.strip()]
        if checks is not None
        else None
    )
    rc = gh_setup.run(
        repo,
        config_path=config_path,
        checks_override=checks_override,
        dry_run=dry_run,
        prompt=lambda name: click.prompt(f"secret {name}", hide_input=True),
    )
    raise SystemExit(rc)


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


@root.command(name="install")
@click.argument("path", required=False)
@click.option(
    "--pr",
    is_flag=True,
    help="Stage the managed set on the `shipit/install` branch and open a DRAFT "
    "PR (the standalone onboarding/reconcile flow).",
)
@click.option(
    "--push",
    is_flag=True,
    help="Break-glass: commit and push straight to the branch (admin), no PR.",
)
@click.option(
    "--local",
    is_flag=True,
    help="Local-only: commit the managed set on the current branch; no push, no PR "
    "(used by `tree create` provisioning).",
)
@click.option(
    "--dry-run", is_flag=True, help="Print the reconciliation plan; touch nothing."
)
def install_cmd(
    path: str | None, pr: bool, push: bool, local: bool, dry_run: bool
) -> None:
    """Vendor + reconcile shipit's managed set into the consumer at PATH.

    PATH defaults to the current directory. By default install refreshes the
    managed set IN THE WORKING TREE and stops — no commit, no branch, no push,
    no PR — so a mid-workstream refresh lands in the caller's own commit, never
    in a stray parallel PR (#359). Re-running with no changes is a clean no-op.

    ``--pr`` opts into the standalone reconcile flow: stage on the
    `shipit/install` branch and open a DRAFT PR (pull, never push); a
    consumer-edited unit is surfaced in the PR body rather than clobbered blind.

    ``--local`` commits the managed set on the current branch and stops (no push,
    no PR) — the mode Tree provisioning uses so creating a Tree never touches origin.
    """
    if sum((pr, push, local)) > 1:
        raise click.UsageError("--pr, --push, and --local are mutually exclusive.")
    rc = install.run(path, dry_run=dry_run, pr=pr, push=push, local=local)
    raise SystemExit(rc)


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


@root.command(name="logs")
@click.argument("repo", required=False)
@click.option(
    "--path",
    "path_only",
    is_flag=True,
    help='Print the absolute log file path and exit (for `cat "$(shipit logs --path)"`).',
)
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Stream appended log lines live (tail -f); ends on Ctrl-C.",
)
@click.option(
    "--raw",
    is_flag=True,
    help="Emit unmodified JSONL lines (no path header) for piping to jq.",
)
@click.option(
    "-n",
    "--lines",
    "lines",
    type=int,
    default=logs.DEFAULT_TAIL,
    show_default=True,
    help="Trailing records to print in the default (no-flag) view.",
)
@click.option(
    "--events",
    "events_only",
    is_flag=True,
    help="Only dev-cycle event records (records carrying an `event` field).",
)
@click.option(
    "--pr",
    "pr",
    type=int,
    default=None,
    metavar="N",
    help="Only records whose bound `pr` domain key equals this PR number.",
)
@click.option(
    "--session",
    "session",
    default=None,
    metavar="ID|current",
    help="Only this session's records; `current` resolves from the session "
    "environment (or the ephemeral Tree cwd).",
)
@click.option(
    "--epic",
    "epic",
    default=None,
    metavar="CODE",
    help="Only records whose bound `epic` domain key equals this code.",
)
@click.option(
    "--ws",
    "ws",
    default=None,
    metavar="N",
    help="Only this Work Stream's records; accepts 1, 01, or WS01.",
)
@click.option(
    "--agent",
    "agent",
    default=None,
    metavar="ID",
    help="Only records whose bound `agent` domain key equals this spawn id.",
)
@click.option(
    "--role",
    "role",
    default=None,
    metavar="NAME",
    help="Only records whose bound `role` domain key equals this Role name.",
)
@click.option(
    "--flow",
    is_flag=True,
    help="Render the filtered records as the session story (implies --events).",
)
@click.option(
    "--agent-ids",
    "show_agents",
    is_flag=True,
    help="Show agent ids on flow lines (always collected, displayed on request).",
)
def logs_cmd(
    repo: str | None,
    path_only: bool,
    follow: bool,
    raw: bool,
    lines: int,
    events_only: bool,
    pr: int | None,
    session: str | None,
    epic: str | None,
    ws: str | None,
    agent: str | None,
    role: str | None,
    flow: bool,
    show_agents: bool,
) -> None:
    """Locate and read shipit's durable per-repo JSONL log.

    REPO is owner/name; omitted, it defaults to the current checkout's repo. The
    path is resolved by the file sink (logsetup), the single source of truth — no
    recomputed platform location. --path prints just that absolute path so an
    agent can `cat`/`grep` it. -f/--follow streams new records; with no flag it
    prints the path plus the last N records, rendered legibly (ts LEVEL logger:
    msg, domain keys trailing); a malformed line is skipped with a stderr note.
    --raw passes the stored lines through unmodified for jq — no parsing, no
    skipping, malformed lines included — UNLESS a filter is active. --events and
    the domain-key filters (--pr/--session/--epic/--ws/--agent/--role) compose
    as AND, apply before the tail count, and work with every view; selecting on
    a field requires parsing, so under an active filter even --raw parses and
    drops a malformed line rather than passing it through. --flow renders the
    session story (intent/theme header, relative times, EPIC-WSnn prefixes;
    --agent-ids reveals agent ids) and implies --events. A log not written yet
    is reported, not crashed.
    """
    rc = logs.run(
        repo,
        path_only=path_only,
        follow=follow,
        raw=raw,
        tail=lines,
        events_only=events_only,
        pr=pr,
        session=session,
        epic=epic,
        ws=ws,
        agent=agent,
        role=role,
        flow=flow,
        show_agents=show_agents,
    )
    raise SystemExit(rc)


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
