"""Smoke tests for the click CLI surface."""

from shipit import cli


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


def test_help_lists_provision(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "provision" in capsys.readouterr().out


def test_provision_help_lists_lexd(capsys):
    rc = cli.main(["provision", "--help"])
    assert rc == 0
    assert "lexd" in capsys.readouterr().out


def test_provision_lexd_help(capsys):
    rc = cli.main(["provision", "lexd", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--json" in out
    assert "pixi env" in out


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
