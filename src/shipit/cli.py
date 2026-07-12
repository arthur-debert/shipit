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
from .verbs._help import register_long_help
from .verbs.changelog import changelog as changelog_group
from .verbs.ci import ci as ci_group
from .verbs.eval import eval_group
from .verbs.fleet import fleet as fleet_group
from .verbs.hook import hook as hook_group
from .verbs.lab import lab_group
from .verbs.logevent import log as log_group
from .verbs.pr import pr as pr_group
from .verbs.provision import provision as provision_group
from .verbs.release import release as release_group
from .verbs.session import session as session_group
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
    """Run this repo's build legs: `shipit build [--version VERSION] [--target TRIPLE] [LEG] [-- ARGS...]`.

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
    missing builder hard-fails, never skips), 2 usage. `--target` cross-compiles
    rust legs to a triple (target/TRIPLE/release/) for the cross platforms a
    native runner cannot build natively (TOL02-WS11).
    """
    raise SystemExit(build_verb.run(list(args), version=version, target=target))


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


# The nested `changelog` group (TOL01-WS06) — the language-agnostic
# release-notes tool over CHANGELOG/ fragments: the PR-time fragment-sync
# `check` (the changelog-sync lane's run) and the cut-time `coalesce`.
root.add_command(changelog_group)

# The nested `release` group (TOL02) — the release pipeline's independently
# invocable stages (PRD story 19): `preflight` (WS02: the planner + secrets
# derivation), `prepare` (WS01: version resolve, bump projection, changelog
# roll, commit + annotated tag + push, ADR-0041), `bundle` + `assert-bundle`
# (WS03: unsigned Artifact composition and the scar-#2 integrity guard),
# `sign` (WS04: the consumer-agnostic mac signer unit, workflows.lex §3.1),
# and `publish` (WS05: the terminal endpoint-adapter dispatch, scar-#3 gate).
root.add_command(release_group)


# The nested `ci` group (TOL01-WS05) — the PR-time routing surface: `ci plan`
# is the one lane-planner invocation whose JSON matrix the wf-checks workflow
# block fans into `pixi run <run>` jobs (ADR-0040: zero logic in the block).
root.add_command(ci_group)


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

# The nested `lab` group (RVW03, ADR-0049) — the Review Lab's experiment
# surface: `lab run` executes a declarative cell over the offline replay
# driver, `lab report` renders its convergence curve. Attached like `eval`.
root.add_command(lab_group)

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

# The nested `session` group (CDX01 #604) — coordinator-session bootstrap for
# backends with no pre-launch cwd seam: `session codex` provisions the
# ephemeral session Tree (ADR-0027) and execs interactive codex rooted in it.
root.add_command(session_group)

# The nested `fleet` group (TOL01-WS07) — fleet-wide verification over the
# declared [project.portfolio]: `shipit fleet sweep` runs every applicable
# tool verb in a fresh Tree per portfolio repo under the candidate build
# (SHIPIT_EXEC, ADR-0033) and emits the per-tool × per-repo matrix report —
# the TOL01 exit gate and ADP02's adoption-readiness seed.
root.add_command(fleet_group)

# The nested `wf` group (TOL01-WS04) — workflow tools: `shipit wf test` runs
# one workflow/job under act in a container against a crafted event, so a
# workflow edit is validated locally before any push (PRD stories 40/41).
root.add_command(wf_group)


_HELP_RESOURCES = {
    (): ("shipit", "shipit_help.txt"),
    ("gh-setup",): ("shipit.verbs", "gh_setup_help.txt"),
    ("verify-apps",): ("shipit.verbs", "verify_apps_help.txt"),
    ("install",): ("shipit.verbs", "install_help.txt"),
    ("lint",): ("shipit.verbs", "lint_help.txt"),
    ("test",): ("shipit.verbs", "test_help.txt"),
    ("build",): ("shipit.verbs", "build_help.txt"),
    ("e2e",): ("shipit.verbs", "e2e_help.txt"),
    ("changelog",): ("shipit.verbs", "changelog_help.txt"),
    ("changelog", "check"): ("shipit.verbs", "changelog_check_help.txt"),
    ("changelog", "render"): ("shipit.verbs", "changelog_render_help.txt"),
    ("changelog", "coalesce"): ("shipit.verbs", "changelog_coalesce_help.txt"),
    ("release",): ("shipit.verbs", "release_help.txt"),
    ("release", "preflight"): ("shipit.verbs", "release_preflight_help.txt"),
    ("release", "prepare"): ("shipit.verbs", "release_prepare_help.txt"),
    ("release", "bundle"): ("shipit.verbs", "release_bundle_help.txt"),
    ("release", "assert-bundle"): ("shipit.verbs", "release_assert_bundle_help.txt"),
    ("release", "sign"): ("shipit.verbs", "release_sign_help.txt"),
    ("release", "publish"): ("shipit.verbs", "release_publish_help.txt"),
    ("ci",): ("shipit.verbs", "ci_help.txt"),
    ("ci", "plan"): ("shipit.verbs", "ci_plan_help.txt"),
    ("logs",): ("shipit.verbs", "logs_help.txt"),
    ("provision",): ("shipit.verbs", "provision_help.txt"),
    ("provision", "lexd"): ("shipit.verbs", "provision_lexd_help.txt"),
    ("pr",): ("shipit.verbs.pr", "pr_help.txt"),
    ("pr", "status"): ("shipit.verbs.pr", "pr_status_help.txt"),
    ("pr", "review"): ("shipit.verbs.pr", "pr_review_help.txt"),
    ("pr", "review", "request"): ("shipit.verbs.pr", "pr_review_request_help.txt"),
    ("pr", "review", "replay"): ("shipit.verbs.pr", "pr_review_replay_help.txt"),
    ("pr", "next"): ("shipit.verbs.pr", "pr_next_help.txt"),
    ("pr", "ready"): ("shipit.verbs.pr", "pr_ready_help.txt"),
    ("pr", "classify"): ("shipit.verbs.pr", "pr_classify_help.txt"),
    ("pr", "wait"): ("shipit.verbs.pr", "pr_wait_help.txt"),
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
