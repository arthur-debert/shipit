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
