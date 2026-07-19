"""Smoke tests for the click CLI surface."""

from shipit import cli


def _public_command_paths(command, path=()):
    if not hasattr(command, "commands"):
        return
    for name, child in command.commands.items():
        if name == "help" or getattr(child, "hidden", False):
            continue
        child_path = (*path, name)
        yield child_path
        yield from _public_command_paths(child, child_path)


def test_root_long_form_help(capsys):
    rc = cli.main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit` is the portfolio standardization tool." in out
    assert "shipit <command> help" in out


def test_group_long_form_help(capsys):
    rc = cli.main(["pr", "review", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit pr review` contains reviewer-facing actions." in out
    assert "_run" not in out


def test_leaf_long_form_help_preserves_click_help(capsys):
    rc = cli.main(["build", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit build` runs the repository's declared build legs." in out
    assert "Usage:" not in out

    rc = cli.main(["build", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage:" in out
    assert "declared build legs" not in out


def test_long_form_help_resources_cover_public_commands():
    expected = {(), *_public_command_paths(cli.root)}
    assert set(cli._HELP_RESOURCES) == expected
    assert ("pr", "review", "_run") not in cli._HELP_RESOURCES


def test_long_form_help_resource_filename_convention():
    for path, (_, resource) in cli._HELP_RESOURCES.items():
        assert resource.endswith("_help.txt")
        command_name = "shipit" if not path else path[-1].replace("-", "_")
        assert command_name in resource.removesuffix("_help.txt")


def test_help_lists_gh_setup(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gh-setup" in out


def test_gh_setup_help(capsys):
    rc = cli.main(["gh-setup", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ruleset" in out.lower()
    assert "--dry-run" in out


def test_help_lists_lint(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "lint" in capsys.readouterr().out


def test_lint_help(capsys):
    rc = cli.main(["lint", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--fix" in out
    assert "checks" in out.lower()


def test_version():
    rc = cli.main(["--version"])
    assert rc == 0


def test_version_shows_the_build_sha(capsys, monkeypatch):
    # ADR-0033: --version must surface the running build's commit so an operator
    # can tell WHICH build this is — not just the static package version.
    from shipit import buildid
    from shipit.identity import Sha

    sha = Sha("a" * 40)
    monkeypatch.setattr(buildid, "build_sha", lambda: sha)
    rc = cli.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert sha.value in out
    assert "0.0.1" in out


def test_version_handles_unresolved_build(capsys, monkeypatch):
    # No install record, no embed, no checkout: say so plainly rather than
    # crash or print a bare version that "identifies nothing".
    from shipit import buildid

    monkeypatch.setattr(buildid, "build_sha", lambda: None)
    rc = cli.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "unknown" in out.lower()


def test_help_lists_tree(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "tree" in capsys.readouterr().out


def test_tree_help_lists_create(capsys):
    rc = cli.main(["tree", "--help"])
    assert rc == 0
    assert "create" in capsys.readouterr().out


def test_tree_create_help(capsys):
    rc = cli.main(["tree", "create", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--issue" in out


def test_help_lists_session(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "session" in capsys.readouterr().out


def test_session_help_lists_codex(capsys):
    rc = cli.main(["session", "--help"])
    assert rc == 0
    assert "codex" in capsys.readouterr().out


def test_install_mode_flags_are_mutually_exclusive():
    # --pr, --push, and --local are mutually exclusive modes; passing any two
    # is a usage error (click exits 2), not a silently-resolved precedence.
    from click.testing import CliRunner

    for pair in (["--local", "--push"], ["--pr", "--local"], ["--pr", "--push"]):
        result = CliRunner().invoke(cli.root, ["install", *pair, "."])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output


def test_shipit_exec_override_emits_the_flow_event(monkeypatch):
    # ADR-0033: an invocation running under the SHIPIT_EXEC override announces
    # the pin bypass durably — the flow-log twin of the launcher's stderr line,
    # emitted by the exec'd build itself at CLI entry.
    calls: list[str] = []
    monkeypatch.setattr(
        cli.events, "emit", lambda log, name, msg, *a, **k: calls.append(name)
    )
    monkeypatch.setenv("SHIPIT_EXEC", "/builds/dev-shipit")
    rc = cli.main(["log", "--help"])
    assert rc == 0
    assert calls == ["launcher.overridden"]


def test_no_override_event_without_shipit_exec(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        cli.events, "emit", lambda log, name, msg, *a, **k: calls.append(name)
    )
    monkeypatch.delenv("SHIPIT_EXEC", raising=False)
    rc = cli.main(["log", "--help"])
    assert rc == 0
    assert calls == []
