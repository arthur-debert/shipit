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
from .verbs import gh_setup, install


@click.group(
    help=(
        "shipit — portfolio standardization tooling.\n\n"
        "Provisioning, GitHub repo setup, lint, PR flow and release, on pixi. "
        "`--help` is the map."
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="shipit")
def root() -> None:
    """Root group; subcommands are attached below."""


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
