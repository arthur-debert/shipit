"""Unit tests for the ADR-0030 CLI seam machinery (CLI01-WS01).

The four seam pieces, each tested prstate-style — typed value in, typed value
out, boundaries injected — plus the wiring smoke tests that prove the root
context is resolved exactly ONCE per invocation and threaded via click's
context through the real CLI entry:

- :mod:`shipit.verbs._context` — RootContext + the one best-effort resolution
- :mod:`shipit.verbs._params`  — value objects minted at parse; usage = exit 2
- :mod:`shipit.verbs._errors`  — the runtime error shell; ``error: …`` + exit 1
- :mod:`shipit.verbs._render`  — ``emit`` over a pure ``format_*`` renderer
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from shipit.config import ConfigError
from shipit.execrun import ExecError
from shipit.identity import Revision, WorkingDir, repo_from_slug
from shipit.prstate.errors import PrStateError
from shipit.prstate.reviewers_config import RequiredReviewersConfigError
from shipit.verbs import _context
from shipit.verbs._context import (
    NoAmbientRepoError,
    RootContext,
    current_root_context,
    resolve_root_context,
)
from shipit.verbs._errors import cli_errors
from shipit.verbs._params import (
    REPO_SLUG,
    dry_run_option,
    json_option,
    path_argument,
    repo_argument,
)
from shipit.verbs._render import emit

WD = WorkingDir(
    path="/work/checkout", repo=repo_from_slug("Acme/Widget"), revision=Revision()
)


# --- RootContext (_context) ---------------------------------------------------


def test_resolve_root_context_wraps_the_working_dir(monkeypatch):
    monkeypatch.setattr(_context, "resolve_working_dir", lambda cwd: WD)
    root = resolve_root_context()
    assert root.working_dir is WD
    assert root.repo.slug == "acme/widget"  # canonical, lowercased identity


@pytest.mark.parametrize("boom", [ExecError(["git"], rc=1), ValueError("bad url")])
def test_resolve_root_context_is_best_effort_outside_a_checkout(monkeypatch, boom):
    """No origin remote / unparseable remote degrades to an EMPTY context —
    whether that is fatal is each verb's decision, not the root's."""

    def raise_boom(cwd):
        raise boom

    monkeypatch.setattr(_context, "resolve_working_dir", raise_boom)
    root = resolve_root_context()
    assert root.working_dir is None
    assert root.repo is None


def test_require_repo_outside_a_checkout_is_the_one_uniform_refusal():
    """A verb needing a repo outside a checkout fails with ONE uniform error —
    a runtime refusal (the error shell's exit 1), not a usage error."""
    empty = RootContext(working_dir=None)
    with pytest.raises(NoAmbientRepoError) as exc:
        empty.require_repo()
    assert "not inside a repository checkout" in str(exc.value)
    with pytest.raises(NoAmbientRepoError):
        empty.require_working_dir()


def test_require_repo_returns_the_ambient_identity():
    root = RootContext(working_dir=WD)
    assert root.require_working_dir() is WD
    assert root.require_repo() is WD.repo


def test_default_path_explicit_wins_over_ambient():
    assert RootContext(working_dir=WD).default_path("sub/dir") == "sub/dir"


def test_default_path_falls_back_to_ambient_then_cwd():
    assert RootContext(working_dir=WD).default_path() == "/work/checkout"
    assert RootContext(working_dir=None).default_path() == "."


def test_current_root_context_outside_click_is_empty():
    """A direct (non-click) run() call still gets a usable, empty context."""
    root = current_root_context()
    assert root.working_dir is None


# --- wiring: resolved ONCE at the root, threaded via click ctx ------------------


def test_root_context_resolved_once_and_threaded_via_the_cli_entry(monkeypatch, capsys):
    """Through the REAL CLI entry: the root callback resolves the context exactly
    once per invocation and a downstream command reads THE SAME object off
    click's context via `current_root_context()`."""
    from shipit import cli

    calls: list[int] = []

    def fake_resolve() -> RootContext:
        calls.append(1)
        return RootContext(working_dir=WD)

    monkeypatch.setattr(cli, "resolve_root_context", fake_resolve)
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: None)

    @click.command(name="ctx-probe")
    def probe() -> None:
        click.echo(current_root_context().repo.slug)

    cli.root.add_command(probe)
    try:
        rc = cli.main(["ctx-probe"])
    finally:
        cli.root.commands.pop("ctx-probe", None)

    assert rc == 0
    assert calls == [1]  # resolved exactly ONCE per invocation
    assert capsys.readouterr().out.strip() == "acme/widget"


def test_pr_status_invocation_resolves_the_root_context_once(monkeypatch):
    """A real verb invocation pays exactly one ambient resolution — the root's."""
    from shipit import cli
    from shipit.verbs.pr import status as status_verb

    calls: list[int] = []

    def fake_resolve() -> RootContext:
        calls.append(1)
        return RootContext(working_dir=None)

    monkeypatch.setattr(cli, "resolve_root_context", fake_resolve)
    monkeypatch.setattr(cli, "configure_logging", lambda **kw: None)
    monkeypatch.setattr(status_verb, "resolve_pr", lambda pr: None)

    rc = cli.main(["pr", "status", "--json"])

    assert rc == 0
    assert calls == [1]


# --- the parameter library (_params) -------------------------------------------


def _seam_group(root: RootContext) -> click.Group:
    """A minimal group whose callback threads ``root`` the way the CLI root does."""

    @click.group()
    @click.pass_context
    def grp(ctx: click.Context) -> None:
        ctx.obj = root

    return grp


def test_repo_slug_param_mints_the_canonical_repo():
    @click.command()
    @click.argument("repo", type=REPO_SLUG)
    def probe(repo) -> None:
        click.echo(repo.slug)

    result = CliRunner().invoke(probe, ["Acme/Widget"])
    assert result.exit_code == 0
    assert result.output.strip() == "acme/widget"  # the ONE canonical parser


def test_repo_slug_param_malformed_is_a_usage_error_exit_2():
    """Construction-is-validation: a malformed slug is click's usage error —
    exit 2 at parse, never verb-body code."""

    @click.command()
    @click.argument("repo", type=REPO_SLUG)
    def probe(repo) -> None:  # pragma: no cover - never reached
        click.echo(repo.slug)

    result = CliRunner().invoke(probe, ["not-a-slug"])
    assert result.exit_code == 2
    assert "not an owner/name slug" in result.output


def test_repo_argument_defaults_to_the_ambient_repo():
    grp = _seam_group(RootContext(working_dir=WD))

    @grp.command()
    @repo_argument
    def show(repo) -> None:
        click.echo("none" if repo is None else repo.slug)

    result = CliRunner().invoke(grp, ["show"])
    assert result.exit_code == 0
    assert result.output.strip() == "acme/widget"


def test_repo_argument_explicit_overrides_the_ambient_default():
    grp = _seam_group(RootContext(working_dir=WD))

    @grp.command()
    @repo_argument
    def show(repo) -> None:
        click.echo(repo.slug)

    result = CliRunner().invoke(grp, ["show", "Other/Thing"])
    assert result.exit_code == 0
    assert result.output.strip() == "other/thing"


def test_repo_argument_outside_a_checkout_defaults_to_none():
    """No ambient repo -> the verb receives None; whether that is fatal is the
    verb's require_repo() call, not the parser's."""
    grp = _seam_group(RootContext(working_dir=None))

    @grp.command()
    @repo_argument
    def show(repo) -> None:
        click.echo("none" if repo is None else repo.slug)

    result = CliRunner().invoke(grp, ["show"])
    assert result.exit_code == 0
    assert result.output.strip() == "none"


def test_path_argument_defaults_to_the_ambient_checkout_root():
    grp = _seam_group(RootContext(working_dir=WD))

    @grp.command()
    @path_argument
    def show(path: str) -> None:
        click.echo(path)

    result = CliRunner().invoke(grp, ["show"])
    assert result.exit_code == 0
    assert result.output.strip() == "/work/checkout"


def test_path_argument_explicit_overrides_and_cwd_fallback():
    grp = _seam_group(RootContext(working_dir=None))

    @grp.command()
    @path_argument
    def show(path: str) -> None:
        click.echo(path)

    explicit = CliRunner().invoke(grp, ["show", "some/where"])
    assert explicit.output.strip() == "some/where"
    fallback = CliRunner().invoke(grp, ["show"])
    assert fallback.output.strip() == "."


def test_shared_flag_decorators_are_reusable_across_commands():
    """--json / --dry-run are defined ONCE and applied per command — each
    application binds its own option instance."""

    @click.command()
    @json_option
    @dry_run_option
    def one(as_json: bool, dry_run: bool) -> None:
        click.echo(f"{as_json} {dry_run}")

    @click.command()
    @json_option
    @dry_run_option
    def two(as_json: bool, dry_run: bool) -> None:
        click.echo(f"{as_json} {dry_run}")

    runner = CliRunner()
    assert runner.invoke(one, ["--json"]).output.strip() == "True False"
    assert runner.invoke(two, ["--dry-run"]).output.strip() == "False True"


# --- the error shell (_errors) ---------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        ExecError(["gh"], rc=1, stderr="gh exploded"),
        PrStateError("bad state"),
        ConfigError("bad config"),
        RequiredReviewersConfigError("unknown reviewer"),
        NoAmbientRepoError("not inside a repository checkout"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_cli_errors_maps_the_known_set_to_error_line_and_exit_1(capsys, boom):
    @cli_errors
    def run() -> int:  # the verb's run() shape
        raise boom

    assert run() == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert str(boom).splitlines()[0] in err


def test_cli_errors_passes_the_success_return_through(capsys):
    @cli_errors
    def run(value: int, *, flag: bool = False) -> int:
        return 0 if flag else value

    assert run(3) == 3
    assert run(3, flag=True) == 0
    assert capsys.readouterr().err == ""


def test_cli_errors_lets_an_unknown_exception_propagate():
    """An exception outside the known set is a BUG — a loud traceback, never
    dressed up as a clean runtime failure."""

    @cli_errors
    def run() -> int:
        raise ValueError("programming error")

    with pytest.raises(ValueError):
        run()


# --- the render seam (_render) ----------------------------------------------------


class _Result:
    """A minimal typed result: to_dict() is the JSON surface."""

    def to_dict(self) -> dict:
        return {"state": "ready", "pr": 42}


def test_emit_json_serializes_the_results_to_dict(capsys):
    emit(_Result(), lambda r: "unused", as_json=True)
    out = capsys.readouterr().out
    assert json.loads(out) == {"state": "ready", "pr": 42}
    assert out == json.dumps({"state": "ready", "pr": 42}, indent=2) + "\n"


def test_emit_text_prints_the_pure_renderers_string(capsys):
    calls: list[object] = []

    def format_result(result: _Result) -> str:
        calls.append(result)
        return "line one\nline two"

    emit(_Result(), format_result, as_json=False)
    assert capsys.readouterr().out == "line one\nline two\n"
    assert len(calls) == 1


def test_emit_json_never_calls_the_text_renderer(capsys):
    def format_result(result: _Result) -> str:  # pragma: no cover - must not run
        raise AssertionError("text renderer called on the JSON path")

    emit(_Result(), format_result, as_json=True)
    assert json.loads(capsys.readouterr().out)["pr"] == 42
