"""The ``shipit`` CLI root — a thin click assembler.

``shipit`` is a slim binary with git-style subcommands (architecture.lex §4).
This module builds the root group and attaches each verb; a verb's real logic
lives in ``shipit.verbs.<name>``. ``main(argv) -> int`` is the entrypoint the
console-script and ``python -m shipit`` both call.
"""

from __future__ import annotations

import sys

import click

from . import __version__
from .logsetup import configure_logging, resolve_current_owner_repo
from .verbs import gh_setup, install, lint, logs, verify_apps
from .verbs.hook import hook as hook_group
from .verbs.pr import pr as pr_group


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
def root(verbose: bool) -> None:
    """Root group; subcommands are attached below.

    Configures logging before any subcommand runs, so every verb is covered:
    the quiet stderr console (raised by ``-v``), the CI sinks when in CI, and the
    durable per-repo file sink. The repo is resolved best-effort, so a run outside
    a checkout just skips the file sink rather than failing.
    """
    configure_logging(verbose=verbose, owner_repo=resolve_current_owner_repo())


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
    "--push",
    is_flag=True,
    help="Break-glass: commit and push straight to the branch (admin), no PR.",
)
@click.option(
    "--dry-run", is_flag=True, help="Print the reconciliation plan; touch nothing."
)
def install_cmd(path: str | None, push: bool, dry_run: bool) -> None:
    """Vendor + reconcile shipit's managed set into the consumer at PATH.

    PATH defaults to the current directory. By default install opens a DRAFT PR
    with the changes (pull, never push); a consumer-edited unit is surfaced in
    the PR rather than clobbered. Re-running with no changes is a clean no-op.
    """
    rc = install.run(path, dry_run=dry_run, push=push)
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
    "-n",
    "--lines",
    "lines",
    type=int,
    default=logs.DEFAULT_TAIL,
    show_default=True,
    help="Trailing lines to print in the default (no-flag) view.",
)
def logs_cmd(repo: str | None, path_only: bool, follow: bool, lines: int) -> None:
    """Locate and read shipit's durable per-repo log.

    REPO is owner/name; omitted, it defaults to the current checkout's repo. The
    path is resolved by the file sink (logsetup), the single source of truth — no
    recomputed platform location. --path prints just that absolute path so an
    agent can `cat`/`grep` it. -f/--follow streams new lines; with no flag it
    prints the path plus the last N lines. A log not written yet is reported, not
    crashed.
    """
    rc = logs.run(repo, path_only=path_only, follow=follow, tail=lines)
    raise SystemExit(rc)


# The nested `pr` group (PR flow) is a click.Group assembled in its own package
# (verbs/pr/), so its verbs register there rather than as inline commands here;
# attach the whole group to the root.
root.add_command(pr_group)

# The nested `hook` group (Claude Code lifecycle-hook entrypoints) — the binary
# side of the agent harness (ADR-0012); attached the same way as `pr`.
root.add_command(hook_group)


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
