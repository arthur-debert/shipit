"""Unit tests for lint — routing, the registry, and the hard-gate verb.

The subprocess + git boundary is injected (``run`` takes ``discover`` /
``run_tool``), so the orchestration is exercised with no real linters present.
"""

from shipit.verbs import lint


# --------------------------------------------------------------------------
# Pure routing
# --------------------------------------------------------------------------


def test_lang_for_routes_by_extension():
    assert lint.lang_for("src/x.py").name == "python"
    assert lint.lang_for("a/b.yml").name == "yaml"
    assert lint.lang_for("a/b.yaml").name == "yaml"
    assert lint.lang_for("data.json").name == "json"
    assert lint.lang_for("README.md").name == "markdown"
    assert lint.lang_for("docs/x.lex").name == "lex"
    assert lint.lang_for("run.sh").name == "shell"


def test_lang_for_unmanaged_is_none():
    assert lint.lang_for("Cargo.toml") is None
    assert lint.lang_for("LICENSE") is None
    assert lint.lang_for("img.png") is None


def test_lang_for_extensionless_routes_by_shebang():
    assert lint.lang_for("bin/tool", "#!/usr/bin/env bash"[2:]) is not None
    # The shebang passed to lang_for is the body (without #!), mirroring _shebang.
    assert lint.lang_for("bin/tool", "/usr/bin/env bash").name == "shell"
    assert lint.lang_for("bin/tool", "/bin/sh").name == "shell"
    assert (
        lint.lang_for("bin/tool", "/usr/bin/python3") is None
    )  # python has no shebang leg
    assert lint.lang_for("bin/tool", None) is None


def test_interp_strips_env_and_path():
    assert lint._interp("/usr/bin/env bash") == "bash"
    assert lint._interp("/bin/sh") == "sh"
    assert lint._interp("/usr/bin/env  python3 -u") == "python3"
    assert lint._interp(None) is None
    assert lint._interp("") is None


def test_route_buckets_in_registry_order():
    files = ["z.lex", "a.py", "m.md", "b.py", "c.yml"]
    routed = lint.route(files)
    names = [lang.name for lang, _ in routed]
    # python before yaml before markdown before lex (registry order), not input order.
    assert names == ["python", "yaml", "markdown", "lex"]
    py = dict((lang.name, paths) for lang, paths in routed)["python"]
    assert py == ["a.py", "b.py"]


# --------------------------------------------------------------------------
# The registry / Tool.argv
# --------------------------------------------------------------------------


def test_tool_argv_check_and_fix_selection():
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.argv(fix=False) == ("check",)
    assert ruff_check.argv(fix=True) == ("check", "--fix")
    # lexd has no fix form -> skipped in fix mode.
    lexd = lint.LEX.tools[0]
    assert lexd.argv(fix=False) == ("check",)
    assert lexd.argv(fix=True) is None


def test_every_lang_has_at_least_one_tool():
    for lang in lint.LANGS:
        assert lang.tools, f"{lang.name} has no tools"


# --------------------------------------------------------------------------
# The verb — boundary injected
# --------------------------------------------------------------------------


def _fake_discover(files):
    return lambda root: list(files)


class _Recorder:
    """Records tool invocations and returns a scripted exit code per binary."""

    def __init__(self, codes=None):
        self.codes = codes or {}
        self.calls = []

    def __call__(self, binary, args, cwd):
        self.calls.append((binary, tuple(args)))
        return self.codes.get(binary, 0), ""


def test_clean_tree_passes(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path), discover=_fake_discover(["a.py", "b.md"]), run_tool=rec
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "LINT: OK" in out
    # ruff (check + format), markdownlint all ran, files appended.
    assert ("ruff", ("check", "a.py")) in rec.calls
    assert ("ruff", ("format", "--check", "a.py")) in rec.calls
    assert ("markdownlint", ("b.md",)) in rec.calls


def test_no_recognized_files_is_clean(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path), discover=_fake_discover(["LICENSE", "x.toml"]), run_tool=rec
    )
    assert rc == 0
    assert rec.calls == []
    assert "nothing to check" in capsys.readouterr().out


def test_a_failing_tool_fails_the_gate(tmp_path, capsys):
    rec = _Recorder(codes={"ruff": 1})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["a.py"]), run_tool=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "LINT: FAILED" in out
    assert "python:ruff" in out


def test_run_tool_missing_binary_is_hard_127(tmp_path):
    # The real boundary, deterministic: a binary absent from PATH is 127 + a
    # clear note, never a silent skip (the hard-gate contract).
    rc, out = lint._run_tool("shipit-no-such-linter-xyz", ["a.py"], tmp_path)
    assert rc == 127
    assert "not found on PATH" in out


def test_missing_tool_propagates_to_a_failed_gate(tmp_path, capsys):
    # A 127 from any leg fails the whole gate (hard, never skips).
    rec = _Recorder(codes={"markdownlint": 127})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["b.md"]), run_tool=rec)
    assert rc == 1
    assert "LINT: FAILED" in capsys.readouterr().out


def test_fix_mode_uses_fix_argv_and_skips_unfixable(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["a.py", "d.lex"]),
        run_tool=rec,
    )
    assert rc == 0
    # ruff runs its fix forms.
    assert ("ruff", ("check", "--fix", "a.py")) in rec.calls
    assert ("ruff", ("format", "a.py")) in rec.calls
    # lexd has no fixer -> never invoked in fix mode.
    assert not any(binary == "lexd" for binary, _ in rec.calls)


def test_shell_routed_by_shebang_runs_shellcheck(tmp_path, capsys):
    # An extensionless tracked file with a bash shebang routes to shell.
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    rec = _Recorder()
    rc = lint.run(str(tmp_path), discover=_fake_discover(["tool"]), run_tool=rec)
    assert rc == 0
    assert any(binary == "shellcheck" for binary, _ in rec.calls)
    assert any(binary == "shfmt" for binary, _ in rec.calls)
