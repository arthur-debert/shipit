"""Unit tests for lint — routing, the registry, and the hard-fail check verb.

The Exec + git boundary is injected (``run`` takes ``discover`` / ``run_tool``,
speaking the runner's ExecResult/ExecError contract), so the orchestration is
exercised with no real linters present.
"""

import pytest

from shipit import execrun
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
    assert lint.lang_for("src/main.rs").name == "rust"


def test_lang_for_unmanaged_is_none():
    # Cargo.toml is not a routed FILE — it only roots the rust per_manifest runs.
    assert lint.lang_for("Cargo.toml") is None
    assert lint.lang_for("LICENSE") is None
    assert lint.lang_for("img.png") is None


def test_manifest_roots_every_tracked_manifest_runs():
    # Every tracked Cargo.toml dir runs — a nested manifest is NOT collapsed
    # under an ancestor (cargo does not make it a workspace member for us), so
    # an independent/excluded nested crate is never silently skipped.
    paths = ["Cargo.toml", "crates/a/Cargo.toml", "crates/a/src/lib.rs"]
    assert lint.manifest_roots(paths, ("Cargo.toml",)) == [".", "crates/a"]


def test_manifest_roots_nested_and_siblings():
    paths = ["a/Cargo.toml", "a/sub/Cargo.toml", "b/Cargo.toml", "ab/x.rs"]
    assert lint.manifest_roots(paths, ("Cargo.toml",)) == ["a", "a/sub", "b"]


def test_manifest_roots_subdir_crate_only():
    # The tauri shape: the rust path is src-tauri/, not the repo root.
    paths = ["src-tauri/Cargo.toml", "src-tauri/src/main.rs", "pixi.toml"]
    assert lint.manifest_roots(paths, ("Cargo.toml",)) == ["src-tauri"]


def test_manifest_roots_none_tracked_is_empty():
    assert lint.manifest_roots(["src/main.rs", "README.md"], ("Cargo.toml",)) == []


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
    # lexd has no fix form -> falls back to its check form in fix mode (never
    # skipped: --fix still checks everything).
    lexd = lint.LEX.tools[0]
    assert lexd.argv(fix=False) == ("check",)
    assert lexd.argv(fix=True) == ("check",)


def test_rust_tools_argv_forms():
    clippy, fmt = lint.RUST.tools
    # clippy findings are hard errors; it has no safe fix form, so --fix still
    # runs its check form (never skipped).
    assert clippy.argv(fix=False) == (
        "clippy",
        "--all",
        "--all-targets",
        "--all-features",
        "--",
        "-D",
        "warnings",
    )
    assert clippy.argv(fix=True) == clippy.argv(fix=False)
    # rustfmt is the one rust --fix leg: cargo fmt in place.
    assert fmt.argv(fix=False) == ("fmt", "--all", "--", "--check")
    assert fmt.argv(fix=True) == ("fmt", "--all")
    assert clippy.per_manifest and fmt.per_manifest


def test_every_lang_has_at_least_one_tool():
    for lang in lint.LANGS:
        assert lang.tools, f"{lang.name} has no tools"


# --------------------------------------------------------------------------
# The verb — boundary injected
# --------------------------------------------------------------------------


def _fake_discover(files):
    return lambda root: list(files)


class _Recorder:
    """Records tool invocations and returns a scripted ExecResult per binary."""

    def __init__(self, codes=None):
        self.codes = codes or {}
        self.calls = []
        self.cwds = []  # (binary, tuple(args), cwd) — for per_manifest checks

    def __call__(self, binary, args, cwd):
        self.calls.append((binary, tuple(args)))
        self.cwds.append((binary, tuple(args), cwd))
        rc = self.codes.get(binary, 0)
        if isinstance(rc, execrun.ExecError):
            raise rc
        return execrun.ExecResult(
            argv=(binary, *args), rc=rc, stdout="", stderr="", duration_ms=1
        )


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


def test_a_failing_tool_fails_the_checks(tmp_path, capsys):
    rec = _Recorder(codes={"ruff": 1})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["a.py"]), run_tool=rec)
    assert rc == 1
    out = capsys.readouterr().out
    assert "LINT: FAILED" in out
    assert "python:ruff" in out


def test_run_tool_missing_binary_raises_exec_error(tmp_path):
    # The real boundary, deterministic: a binary absent from PATH surfaces as
    # the runner's single transport error, tagged missing-binary (ADR-0028) —
    # never a raw FileNotFoundError, never a silent skip.
    with pytest.raises(execrun.ExecError) as exc_info:
        lint._run_tool("shipit-no-such-linter-xyz", ["a.py"], tmp_path)
    assert exc_info.value.cause == execrun.CAUSE_MISSING_BINARY


def test_run_tool_states_its_timeout_on_the_wire(tmp_path, monkeypatch):
    # The stated bound rides the wire (ADR-0028): it EQUALS the runner's
    # default (a full-tree linter is legitimately slow), but deliberately —
    # stated, never inherited implicitly.
    captured = {}

    def fake_run(argv, **kw):
        captured["timeout"] = kw.get("timeout")
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(lint.execrun, "run", fake_run)
    lint._run_tool("ruff", ["check", "a.py"], tmp_path)
    assert captured["timeout"] == lint.CHECK_TIMEOUT


def test_missing_binary_is_hard_127_in_the_report(tmp_path, capsys):
    # The orchestrator renders a missing-binary ExecError as the hard-fail 127
    # note and fails the whole check run (hard, never skips).
    boom = execrun.ExecError(
        ["markdownlint"], rc=None, cause=execrun.CAUSE_MISSING_BINARY
    )
    rec = _Recorder(codes={"markdownlint": boom})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["b.md"]), run_tool=rec)
    out = capsys.readouterr().out
    assert rc == 1
    assert "LINT: FAILED" in out
    assert "not found on PATH" in out


def test_unlaunchable_tool_is_hard_127_with_the_error_detail(tmp_path, capsys):
    # Any other launch failure (permissions, a bad cwd) also hard-fails, carrying
    # the transport error's detail rather than the missing-binary note.
    boom = execrun.ExecError(
        ["markdownlint"], rc=None, stderr="Permission denied", cause=execrun.CAUSE_OS
    )
    rec = _Recorder(codes={"markdownlint": boom})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["b.md"]), run_tool=rec)
    out = capsys.readouterr().out
    assert rc == 1
    assert "LINT: FAILED" in out
    assert "could not run" in out
    assert "Permission denied" in out


def test_missing_tool_propagates_to_failed_checks(tmp_path, capsys):
    # A 127 from any leg fails the whole check run (hard, never skips).
    rec = _Recorder(codes={"markdownlint": 127})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["b.md"]), run_tool=rec)
    assert rc == 1
    assert "LINT: FAILED" in capsys.readouterr().out


def test_fix_mode_fixes_what_it_can_and_still_checks_the_rest(tmp_path, capsys):
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
    # lexd has no fixer -> it still runs its CHECK form (the checks never skip a
    # leg in fix mode, so --fix can't pass while lex is broken).
    assert ("lexd", ("check", "d.lex")) in rec.calls


def test_rust_runs_per_manifest_without_file_batches(tmp_path, capsys):
    # .rs files trigger the rust leg; cargo runs once per tracked Cargo.toml
    # dir with NO files appended (cargo speaks to the crate, not a file list).
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs", "src/lib.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    clippy = (
        "clippy",
        "--all",
        "--all-targets",
        "--all-features",
        "--",
        "-D",
        "warnings",
    )
    fmt_check = ("fmt", "--all", "--", "--check")
    assert rec.calls.count(("cargo", clippy)) == 1
    assert rec.calls.count(("cargo", fmt_check)) == 1
    # A root manifest runs at the lint root itself.
    assert all(cwd == tmp_path for b, _, cwd in rec.cwds if b == "cargo")
    assert "rust" in capsys.readouterr().out


def test_rust_every_tracked_manifest_gets_its_own_run(tmp_path):
    # Every tracked Cargo.toml dir runs, cwd'd in — a nested manifest is not
    # collapsed under the root, so an independent/excluded nested crate is
    # never silently skipped (the tools' --all still covers true members).
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(
            [
                "Cargo.toml",
                "crates/a/Cargo.toml",
                "crates/a/src/lib.rs",
                "src-tauri/x.rs",  # rust file outside any manifest still triggers rust
            ]
        ),
        run_tool=rec,
    )
    assert rc == 0
    cargo_cwds = [cwd for b, _, cwd in rec.cwds if b == "cargo"]
    # Both manifest dirs run, clippy + fmt once each: 4 cargo invocations.
    assert set(cargo_cwds) == {tmp_path, tmp_path / "crates" / "a"}
    assert len(cargo_cwds) == 4


def test_rust_subdir_crate_runs_cwd_in_that_dir(tmp_path):
    # The tauri shape: the manifest lives in src-tauri/, so cargo runs there.
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["src-tauri/Cargo.toml", "src-tauri/src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    cargo_cwds = {cwd for b, _, cwd in rec.cwds if b == "cargo"}
    assert cargo_cwds == {tmp_path / "src-tauri"}


def test_rust_files_without_manifest_run_at_root(tmp_path):
    # No tracked Cargo.toml: cargo still runs (at the root) and its own error
    # is the hard verdict — the checks never silently skip a rust path.
    rec = _Recorder(codes={"cargo": 101})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["main.rs"]), run_tool=rec)
    assert rc == 1
    assert {cwd for b, _, cwd in rec.cwds if b == "cargo"} == {tmp_path}


def test_no_rust_paths_run_no_cargo(tmp_path):
    # A Cargo.toml with no .rs files routes nowhere: non-rust repos (and this
    # fixture shape) are entirely unaffected by the rust legs.
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "a.py", "README.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert all(binary != "cargo" for binary, _ in rec.calls)


def test_rust_findings_hard_fail(tmp_path, capsys):
    rec = _Recorder(codes={"cargo": 1})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/lib.rs"]),
        run_tool=rec,
    )
    assert rc == 1
    assert "rust:cargo" in capsys.readouterr().out


def test_fix_mode_applies_rustfmt_and_still_checks_clippy(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["Cargo.toml", "src/lib.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    # fmt runs its in-place fix form; clippy still runs its check form.
    assert ("cargo", ("fmt", "--all")) in rec.calls
    assert (
        "cargo",
        ("clippy", "--all", "--all-targets", "--all-features", "--", "-D", "warnings"),
    ) in rec.calls


def test_shell_routed_by_shebang_runs_shellcheck(tmp_path, capsys):
    # An extensionless tracked file with a bash shebang routes to shell.
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    rec = _Recorder()
    rc = lint.run(str(tmp_path), discover=_fake_discover(["tool"]), run_tool=rec)
    assert rc == 0
    assert any(binary == "shellcheck" for binary, _ in rec.calls)
    assert any(binary == "shfmt" for binary, _ in rec.calls)
