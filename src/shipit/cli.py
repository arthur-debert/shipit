"""The ``shipit`` CLI root — a thin click assembler.

Builds the root group and attaches each verb; a verb's logic lives in ``shipit.verbs.<name>``.
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
from .verbs import gh_setup, install, lint, logs, opportunities, provision, verify_apps
from .verbs import stage as stage_verb
from .verbs import test as test_verb
from .verbs._context import resolve_root_context
from .verbs._help import register_long_help
from .verbs.changelog import changelog as changelog_group
from .verbs.ci import ci as ci_group
from .verbs.eval import eval_group
from .verbs.fleet import fleet as fleet_group
from .verbs.hook import hook as hook_group
from .verbs.lab import lab_group
from .verbs.logevent import log as log_group
from .verbs.pr import pr as pr_group
from .verbs.release import release as release_group
from .verbs.repo import repo as repo_group
from .verbs.review import review as review_group
from .verbs.session import session as session_group
from .verbs.spawn import spawn as spawn_group
from .verbs.tree import tree as tree_group
from .verbs.wf import wf as wf_group
from .verbs.wf_canary import verify_canary_cmd

logger = logging.getLogger("shipit.cli")

_UNKNOWN_BUILD = "unknown (not a tracked build)"


def version_string() -> str:
    """The ``shipit --version`` line, carrying the running build's commit; degrades to :data:`_UNKNOWN_BUILD` rather than raising."""
    sha = buildid.build_sha()
    build = sha.value if sha is not None else _UNKNOWN_BUILD
    return f"shipit {__version__} (build {build})"


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """Eager ``--version`` callback: print :func:`version_string` and exit."""
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
    """Root group; subcommands are attached below."""
    reset_logging()
    root_ctx = resolve_root_context()
    ctx.obj = root_ctx
    repo = root_ctx.repo
    if repo is not None:
        logcontext.bind(repo=repo.slug)
    configure_logging(verbose=verbose, repo=repo)
    shipit_exec = os.environ.get("SHIPIT_EXEC")
    if shipit_exec:
        events.emit(
            logger,
            "launcher.overridden",
            "running under SHIPIT_EXEC=%s — the repo's shipit pin is bypassed",
            shipit_exec,
            extra={"shipit_exec": shipit_exec},
        )


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
    """Verify each local-agent reviewer App is LIVE on REPO (installed + checks:write)."""
    rc = verify_apps.run(repo, agents=list(agents) or None)
    raise SystemExit(rc)


root.add_command(install.cmd)


root.add_command(provision.cmd)


root.add_command(stage_verb.cmd)


@root.command(name="lint")
@click.argument("path", required=False)
@click.option(
    "--fix",
    is_flag=True,
    help="Apply formatters in place (opt-in). Default is a check-only hard-fail check.",
)
def lint_cmd(path: str | None, fix: bool) -> None:
    """Run the standardized multi-language checks over the tree at PATH."""
    rc = lint.run(path, fix=fix)
    raise SystemExit(rc)


@root.command(name="test", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def test_cmd(args: tuple[str, ...]) -> None:
    """Run this repo's test legs: `shipit test [LEG] [-- ARGS...]`."""
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
@click.option(
    "--target",
    "target",
    metavar="TRIPLE",
    default=None,
    help="Cross-compile rust legs to TRIPLE (`cargo build --target TRIPLE`), "
    "landing the binary in target/TRIPLE/release/ (TOL02-WS11). For the cross "
    "platforms a native runner cannot build natively (darwin-x86_64, musl); "
    "absent builds native into target/release/. No-op for go/python/npm.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def build_cmd(version: str | None, target: str | None, args: tuple[str, ...]) -> None:
    """Run this repo's build legs: `shipit build [--version VERSION] [--target TRIPLE] [LEG] [-- ARGS...]`."""
    raise SystemExit(build_verb.run(list(args), version=version, target=target))


@root.command(name="e2e", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def e2e_cmd(args: tuple[str, ...]) -> None:
    """Run this repo's declared e2e harnesses: `shipit e2e [ARTIFACT] [-- ARGS...]`."""
    raise SystemExit(e2e_verb.run(list(args)))


root.add_command(changelog_group)

root.add_command(release_group)


root.add_command(ci_group)


root.add_command(logs.logs_cmd)


root.add_command(repo_group)


root.add_command(pr_group)

root.add_command(review_group)

root.add_command(hook_group)

root.add_command(eval_group)

root.add_command(lab_group)

root.add_command(log_group)

root.add_command(opportunities.opportunities)

root.add_command(tree_group)

root.add_command(spawn_group)

root.add_command(session_group)

root.add_command(fleet_group)

wf_group.add_command(verify_canary_cmd)
root.add_command(wf_group)


_HELP_RESOURCES = {
    (): ("shipit", "shipit_help.txt"),
    ("gh-setup",): ("shipit.verbs", "gh_setup_help.txt"),
    ("verify-apps",): ("shipit.verbs", "verify_apps_help.txt"),
    ("install",): ("shipit.verbs", "install_help.txt"),
    ("stage",): ("shipit.verbs", "stage_help.txt"),
    ("lint",): ("shipit.verbs", "lint_help.txt"),
    ("test",): ("shipit.verbs", "test_help.txt"),
    ("build",): ("shipit.verbs", "build_help.txt"),
    ("e2e",): ("shipit.verbs", "e2e_help.txt"),
    ("changelog",): ("shipit.verbs", "changelog_help.txt"),
    ("changelog", "check"): ("shipit.verbs", "changelog_check_help.txt"),
    ("changelog", "check-fragment"): (
        "shipit.verbs",
        "changelog_check_fragment_help.txt",
    ),
    ("changelog", "render"): ("shipit.verbs", "changelog_render_help.txt"),
    ("changelog", "coalesce"): ("shipit.verbs", "changelog_coalesce_help.txt"),
    ("release",): ("shipit.verbs", "release_help.txt"),
    ("release", "preflight"): ("shipit.verbs", "release_preflight_help.txt"),
    ("release", "prepare"): ("shipit.verbs", "release_prepare_help.txt"),
    ("release", "notes"): ("shipit.verbs", "release_notes_help.txt"),
    ("release", "bundle"): ("shipit.verbs", "release_bundle_help.txt"),
    ("release", "assert-bundle"): ("shipit.verbs", "release_assert_bundle_help.txt"),
    ("release", "sign"): ("shipit.verbs", "release_sign_help.txt"),
    ("release", "publish"): ("shipit.verbs", "release_publish_help.txt"),
    ("ci",): ("shipit.verbs", "ci_help.txt"),
    ("ci", "plan"): ("shipit.verbs", "ci_plan_help.txt"),
    ("logs",): ("shipit.verbs", "logs_help.txt"),
    ("repo",): ("shipit.verbs", "repo_help.txt"),
    ("repo", "new"): ("shipit.verbs", "repo_new_help.txt"),
    ("pr",): ("shipit.verbs.pr", "pr_help.txt"),
    ("pr", "status"): ("shipit.verbs.pr", "pr_status_help.txt"),
    ("pr", "review"): ("shipit.verbs.pr", "pr_review_help.txt"),
    ("pr", "review", "request"): ("shipit.verbs.pr", "pr_review_request_help.txt"),
    ("pr", "review", "replay"): ("shipit.verbs.pr", "pr_review_replay_help.txt"),
    ("pr", "next"): ("shipit.verbs.pr", "pr_next_help.txt"),
    ("pr", "ready"): ("shipit.verbs.pr", "pr_ready_help.txt"),
    ("pr", "classify"): ("shipit.verbs.pr", "pr_classify_help.txt"),
    ("pr", "wait"): ("shipit.verbs.pr", "pr_wait_help.txt"),
    ("review",): ("shipit.verbs", "review_help.txt"),
    ("review", "validate"): ("shipit.verbs", "review_validate_help.txt"),
    ("hook",): ("shipit.verbs.hook", "hook_help.txt"),
    ("hook", "pretooluse"): ("shipit.verbs.hook", "hook_pretooluse_help.txt"),
    ("hook", "stop"): ("shipit.verbs.hook", "hook_stop_help.txt"),
    ("hook", "subagent-stop"): ("shipit.verbs.hook", "hook_subagent_stop_help.txt"),
    ("hook", "sessionstart"): ("shipit.verbs.hook", "hook_sessionstart_help.txt"),
    ("hook", "worktreecreate"): ("shipit.verbs.hook", "hook_worktreecreate_help.txt"),
    ("hook", "worktreeremove"): ("shipit.verbs.hook", "hook_worktreeremove_help.txt"),
    ("eval",): ("shipit.verbs.eval", "eval_help.txt"),
    ("eval", "report"): ("shipit.verbs.eval", "eval_report_help.txt"),
    ("eval", "score"): ("shipit.verbs.eval", "eval_score_help.txt"),
    ("eval", "bank"): ("shipit.verbs.eval", "eval_bank_help.txt"),
    ("eval", "bank", "label"): ("shipit.verbs.eval", "eval_bank_label_help.txt"),
    ("eval", "bank", "alias"): ("shipit.verbs.eval", "eval_bank_alias_help.txt"),
    ("lab",): ("shipit.verbs.lab", "lab_help.txt"),
    ("lab", "run"): ("shipit.verbs.lab", "lab_run_help.txt"),
    ("lab", "report"): ("shipit.verbs.lab", "lab_report_help.txt"),
    ("log",): ("shipit.verbs", "log_help.txt"),
    ("log", "event"): ("shipit.verbs", "log_event_help.txt"),
    ("opportunities",): ("shipit.verbs", "opportunities_help.txt"),
    ("opportunities", "create"): ("shipit.verbs", "opportunities_create_help.txt"),
    ("tree",): ("shipit.verbs", "tree_help.txt"),
    ("tree", "create"): ("shipit.verbs", "tree_create_help.txt"),
    ("tree", "list"): ("shipit.verbs", "tree_list_help.txt"),
    ("tree", "remove"): ("shipit.verbs", "tree_remove_help.txt"),
    ("tree", "gc"): ("shipit.verbs", "tree_gc_help.txt"),
    ("spawn",): ("shipit.verbs", "spawn_help.txt"),
    ("spawn", "subagent"): ("shipit.verbs", "spawn_subagent_help.txt"),
    ("spawn", "brief"): ("shipit.verbs", "spawn_brief_help.txt"),
    ("session",): ("shipit.verbs", "session_help.txt"),
    ("session", "codex"): ("shipit.verbs", "session_codex_help.txt"),
    ("session", "resume"): ("shipit.verbs", "session_resume_help.txt"),
    ("fleet",): ("shipit.verbs", "fleet_help.txt"),
    ("fleet", "sweep"): ("shipit.verbs", "fleet_sweep_help.txt"),
    ("wf",): ("shipit.verbs", "wf_help.txt"),
    ("wf", "test"): ("shipit.verbs", "wf_test_help.txt"),
    ("wf", "verify-canary"): ("shipit.verbs", "wf_verify_canary_help.txt"),
}

register_long_help(root, _HELP_RESOURCES)


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
