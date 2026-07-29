import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from shipit import execrun, lint
from shipit.verbs import lint as lint_verb

_RUFF_CFG = lint.data_path("ruff.toml")
_PRETTIER_CFG = lint.data_path("prettierrc.yaml")
_MD_CFG = lint.data_path("markdownlint.yaml")
_YAML_CFG = lint.data_path("yamllint.yaml")
_ACTIONLINT_CFG = lint.data_path("actionlint.yaml")


def test_lang_for_routes_by_extension():
    assert lint.lang_for("src/x.py").name == "python"
    assert lint.lang_for("a/b.yml").name == "yaml"
    assert lint.lang_for("a/b.yaml").name == "yaml"
    assert lint.lang_for("data.json").name == "web"
    assert lint.lang_for("src/app.ts").name == "web"
    assert lint.lang_for("src/App.tsx").name == "web"
    assert lint.lang_for("src/Widget.svelte").name == "web"
    assert lint.lang_for("README.md").name == "markdown"
    assert lint.lang_for("docs/x.lex").name == "lex"
    assert lint.lang_for("run.sh").name == "shell"
    assert lint.lang_for("src/main.rs").name == "rust"


def test_lang_for_unmanaged_is_none():
    assert lint.lang_for("Cargo.toml") is None
    assert lint.lang_for("LICENSE") is None
    assert lint.lang_for("img.png") is None


def test_manifest_roots_every_tracked_manifest_runs():
    paths = ["Cargo.toml", "crates/a/Cargo.toml", "crates/a/src/lib.rs"]
    assert lint.manifest_roots(paths, ("Cargo.toml",)) == [".", "crates/a"]


def test_manifest_roots_nested_and_siblings():
    paths = ["a/Cargo.toml", "a/sub/Cargo.toml", "b/Cargo.toml", "ab/x.rs"]
    assert lint.manifest_roots(paths, ("Cargo.toml",)) == ["a", "a/sub", "b"]


def test_manifest_roots_subdir_crate_only():
    paths = ["src-tauri/Cargo.toml", "src-tauri/src/main.rs", "pixi.toml"]
    assert lint.manifest_roots(paths, ("Cargo.toml",)) == ["src-tauri"]


def test_manifest_roots_none_tracked_is_empty():
    assert lint.manifest_roots(["src/main.rs", "README.md"], ("Cargo.toml",)) == []


def test_lang_for_extensionless_routes_by_shebang():
    assert lint.lang_for("bin/tool", "#!/usr/bin/env bash"[2:]) is not None
    assert lint.lang_for("bin/tool", "/usr/bin/env bash").name == "shell"
    assert lint.lang_for("bin/tool", "/bin/sh").name == "shell"
    assert lint.lang_for("bin/tool", "/usr/bin/python3") is None
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
    assert names == ["python", "yaml", "markdown", "lex"]
    py = dict((lang.name, paths) for lang, paths in routed)["python"]
    assert py == ["a.py", "b.py"]


def test_lex_projections_need_a_tracked_source():
    assert lint.lex_projections(["a.md", "b.lex"]) == set()
    assert lint.lex_projections(["a.md", "a.lex"]) == {"a.md"}
    assert lint.lex_projections(["docs/dev/x.md", "docs/dev/x.lex", "docs/y.md"]) == {
        "docs/dev/x.md"
    }


def test_route_skips_lex_projections():
    files = [
        "README.lex",
        "README.md",
        "docs/guide.lex",
        "docs/guide.md",
        "docs/manual.md",
    ]
    routed = dict((lang.name, paths) for lang, paths in lint.route(files))
    assert routed["markdown"] == ["docs/manual.md"]
    assert routed["lex"] == ["README.lex", "docs/guide.lex"]


def test_lang_for_never_returns_a_path_claiming_lang():
    assert lint.lang_for(".github/workflows/ci.yml").name == "yaml"
    assert lint.lang_for("a/b.yml").name == "yaml"


def test_path_claimed_langs_claims_workflow_yaml():
    claims = lint.path_claimed_langs(".github/workflows/ci.yml")
    assert [lang.name for lang in claims] == ["actions"]
    claims = lint.path_claimed_langs(".github/workflows/release.yaml")
    assert [lang.name for lang in claims] == ["actions"]


def test_path_claimed_langs_scopes_by_prefix_and_extension():
    assert lint.path_claimed_langs(".github/workflows/README.md") == []
    assert lint.path_claimed_langs(".github/dependabot.yml") == []
    assert lint.path_claimed_langs("config.yml") == []
    assert lint.path_claimed_langs(".github/workflows-old/ci.yml") == []


def test_path_claimed_langs_is_non_recursive():
    assert lint.path_claimed_langs(".github/workflows/archive/ci.yml") == []
    assert lint.path_claimed_langs(".github/workflows/old/nested/ci.yaml") == []


def test_route_workflow_files_bucket_into_yaml_and_actions():
    files = [".github/workflows/ci.yml", "config.yml", "a.py"]
    routed = dict((lang.name, paths) for lang, paths in lint.route(files))
    assert routed["yaml"] == [".github/workflows/ci.yml", "config.yml"]
    assert routed["actions"] == [".github/workflows/ci.yml"]


def test_path_ignored_gitignore_style_globs():
    assert lint.path_ignored("tests/fixtures/a.md", ["tests/fixtures/**"])
    assert lint.path_ignored("tests/fixtures/sub/b.md", ["tests/fixtures/**"])
    assert lint.path_ignored("tests/a.md", ["tests/*.md"])
    assert not lint.path_ignored("tests/a/b.md", ["tests/*.md"])
    assert not lint.path_ignored("src/x.py", ["tests/fixtures/**"])


def test_path_ignored_directory_prefix_drops_whole_subtree():
    assert lint.path_ignored("CHANGELOG/0.15.0.md", ["CHANGELOG/"])
    assert lint.path_ignored("CHANGELOG/nested/x.md", ["CHANGELOG/"])
    assert not lint.path_ignored("CHANGELOG.md", ["CHANGELOG/"])


def test_path_ignored_floating_vs_anchored():
    assert lint.path_ignored("CHANGELOG.md", ["CHANGELOG.md"])
    assert lint.path_ignored("docs/CHANGELOG.md", ["CHANGELOG.md"])
    assert lint.path_ignored("CHANGELOG.md", ["/CHANGELOG.md"])
    assert not lint.path_ignored("docs/CHANGELOG.md", ["/CHANGELOG.md"])


def test_path_ignored_empty_patterns_never_match():
    assert not lint.path_ignored("anything.md", [])


def test_path_ignored_bad_glob_is_a_no_match_not_a_crash():
    assert not lint.path_ignored("a.md", ["["])
    assert not lint.path_ignored("a.md", ["[z-a]"])
    assert lint.path_ignored("keep.md", ["[z-a]", "keep.md"])


def test_drop_ignored_removes_matches_order_preserved():
    files = ["src/x.py", "tests/fixtures/a.md", "README.md", "tests/fixtures/b.txt"]
    assert lint.drop_ignored(files, ["tests/fixtures/**"]) == ["src/x.py", "README.md"]


def test_drop_ignored_no_patterns_is_identity():
    files = ["a.py", "b.md"]
    assert lint.drop_ignored(files, []) is files


def test_drop_protected_testdata_drops_every_convention_at_any_depth():
    # fixer never rewrites a deliberately-malformed / byte-exact fixture (#500).
    files = [
        "tests/fixtures/broken.md",
        "a/b/testdata/sample.json",
        "pkg/__fixtures__/x.md",
        "internal/golden/out.txt",
        "internal/goldens/out.txt",
        "ui/__snapshots__/Comp.snap",
        "render/snapshots/frame.txt",
    ]
    assert lint.drop_protected_testdata(files) == []


def test_drop_protected_testdata_keeps_ordinary_source_order_preserved():
    files = [
        "src/x.py",
        "tests/fixtures/a.md",
        "README.md",
        "docs/fixtures-guide.md",
        "tests/testdata/b.json",
    ]
    assert lint.drop_protected_testdata(files) == [
        "src/x.py",
        "README.md",
        "docs/fixtures-guide.md",
    ]


def test_drop_protected_testdata_empty_is_identity():
    assert lint.drop_protected_testdata([]) == []


def test_protected_testdata_is_the_exact_complement_of_drop():
    files = [
        "src/lib.rs",
        "tests/fixtures/bad.rs",
        "README.md",
        "a/testdata/x.json",
    ]
    assert lint.protected_testdata(files) == [
        "tests/fixtures/bad.rs",
        "a/testdata/x.json",
    ]
    assert lint.drop_protected_testdata(files) + lint.protected_testdata(files) == [
        "src/lib.rs",
        "README.md",
        "tests/fixtures/bad.rs",
        "a/testdata/x.json",
    ]
    kept = set(lint.drop_protected_testdata(files))
    dropped = set(lint.protected_testdata(files))
    assert kept.isdisjoint(dropped)
    assert kept | dropped == set(files)


def test_tool_argv_check_and_fix_selection():
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.argv(fix=False) == ("check",)
    assert ruff_check.argv(fix=True) == ("check", "--fix")
    lexd = lint.LEX.tools[0]
    assert lexd.argv(fix=False) == ("check",)
    assert lexd.argv(fix=True) == ("check",)


def test_rust_tools_argv_forms():
    clippy, fmt = lint.RUST.tools
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
    cfg = lint._RUSTFMT_CONFIG_PATH
    assert fmt.argv(fix=False) == (
        "fmt",
        "--all",
        "--",
        "--check",
        "--config-path",
        cfg,
    )
    assert fmt.argv(fix=True) == ("fmt", "--all", "--", "--config-path", cfg)
    assert clippy.per_manifest and fmt.per_manifest


def test_lua_lang_routes_and_uses_stylua_and_selene():
    lua = lint.lang_for("lua/plugin/init.lua")
    assert lua is not None and lua.name == "lua"
    assert [tool.binary for tool in lua.tools] == ["stylua", "selene"]


def test_lua_tools_argv_forms():
    stylua, selene = lint.LUA.tools
    assert stylua.argv(fix=False) == ("--check",)
    assert stylua.argv(fix=True) == ()
    assert selene.argv(fix=False) == ()
    assert selene.argv(fix=True) == ()


def test_every_lang_has_at_least_one_tool():
    for lang in lint.LANGS:
        assert lang.tools, f"{lang.name} has no tools"


def test_tracks_editorconfig_root_only():
    def root_true():
        return "root = true\n"

    def not_root():
        return "[*]\nindent_style = space\n"

    def root_false():
        return "root = false\n"

    def must_not_read():
        raise AssertionError("reader must not run when .editorconfig is untracked")

    assert lint.tracks_editorconfig([".editorconfig", "a.sh"], root_true)
    assert not lint.tracks_editorconfig([".editorconfig", "a.sh"], not_root)
    assert not lint.tracks_editorconfig([".editorconfig", "a.sh"], root_false)
    assert not lint.tracks_editorconfig(
        ["sub/dir/.editorconfig", "sub/dir/a.sh"], must_not_read
    )
    assert not lint.tracks_editorconfig(["a.sh", "b.json", "README.md"], must_not_read)
    assert not lint.tracks_editorconfig(
        ["my.editorconfig.bak", "x.editorconfig.md"], must_not_read
    )


def test_editorconfig_declares_root():
    assert lint.editorconfig_declares_root("root = true\n")
    assert lint.editorconfig_declares_root("Root = True\n")
    assert lint.editorconfig_declares_root("# comment\n; also\n\nroot=true\n[*]\n")
    assert not lint.editorconfig_declares_root("root = false\n")
    assert not lint.editorconfig_declares_root("")
    assert not lint.editorconfig_declares_root("[*]\nindent_style = space\n")
    assert not lint.editorconfig_declares_root("[*]\nroot = true\n")
    assert not lint.editorconfig_declares_root("root = true\nroot = false\n")
    assert lint.editorconfig_declares_root("root = false\nroot = true\n")


def test_read_editorconfig_strips_utf8_bom(tmp_path):
    cfg = tmp_path / ".editorconfig"
    cfg.write_bytes(b"\xef\xbb\xbfroot = true\n")
    body = lint._read_editorconfig(cfg)
    assert body is not None
    assert lint.editorconfig_declares_root(body)


def test_tracks_root_editorconfig_reads_repo_root_not_target(monkeypatch):
    seen: dict[str, str] = {}
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: "/repo")

    def fake_ls(*, cwd):
        seen["cwd"] = cwd
        return [".editorconfig", "src/app.py"]

    monkeypatch.setattr(lint.git, "ls_files", fake_ls)
    monkeypatch.setattr(lint, "_read_editorconfig", lambda path: "root = true\n")
    assert lint._tracks_root_editorconfig(Path("/repo/src")) is True
    assert seen["cwd"] == "/repo"


def test_tracks_root_editorconfig_requires_root_true_content(monkeypatch):
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: "/repo")
    monkeypatch.setattr(lint.git, "ls_files", lambda *, cwd: [".editorconfig", "a.sh"])
    monkeypatch.setattr(
        lint, "_read_editorconfig", lambda path: "[*]\nindent_style = space\n"
    )
    assert lint._tracks_root_editorconfig(Path("/repo")) is False
    monkeypatch.setattr(lint, "_read_editorconfig", lambda path: "root = true\n")
    assert lint._tracks_root_editorconfig(Path("/repo")) is True


def test_tracks_root_editorconfig_nested_only_is_pinned(monkeypatch):
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: "/repo")
    monkeypatch.setattr(
        lint.git, "ls_files", lambda *, cwd: ["sub/.editorconfig", "sub/a.sh"]
    )
    assert lint._tracks_root_editorconfig(Path("/repo/sub")) is False


def test_tracks_root_editorconfig_outside_checkout_is_pinned(monkeypatch):
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: None)

    def must_not_query(*, cwd):
        raise AssertionError("ls_files must not run without a repo root")

    monkeypatch.setattr(lint.git, "ls_files", must_not_query)
    assert lint._tracks_root_editorconfig(Path("/tmp/not-a-repo")) is False


def test_shfmt_pin_gated_on_tracked_editorconfig():
    shfmt = lint.SHELL.tools[1]
    assert shfmt.binary == "shfmt"
    assert shfmt.argv(fix=False, pin_editorconfig=True) == ("-i", "0", "-d")
    assert shfmt.argv(fix=True, pin_editorconfig=True) == ("-i", "0", "-w")
    assert shfmt.argv(fix=False, pin_editorconfig=False) == ("-d",)
    assert shfmt.argv(fix=True, pin_editorconfig=False) == ("-w",)


def test_prettier_pin_gated_on_tracked_editorconfig():
    prettier = lint.WEB.tools[0]
    assert prettier.binary == "prettier"
    assert prettier.argv(fix=False, pin_editorconfig=True) == (
        "--no-editorconfig",
        "--check",
        "--log-level",
        "warn",
    )
    assert prettier.argv(fix=True, pin_editorconfig=True) == (
        "--no-editorconfig",
        "--write",
    )
    assert prettier.argv(fix=False, pin_editorconfig=False) == (
        "--check",
        "--log-level",
        "warn",
    )


def test_non_editorconfig_tool_ignores_the_pin():
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.editorconfig_pin == ()
    assert ruff_check.argv(fix=False, pin_editorconfig=True) == ("check",)


def _fake_discover(files):
    return lambda root: list(files)


class _Recorder:
    def __init__(self, codes=None):
        self.codes = codes or {}
        self.calls = []
        self.cwds = []

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
    assert ("ruff", ("--config", _RUFF_CFG, "check", "a.py")) in rec.calls
    assert ("ruff", ("--config", _RUFF_CFG, "format", "--check", "a.py")) in rec.calls
    assert ("markdownlint", ("--config", _MD_CFG, "b.md")) in rec.calls


def test_workflow_file_runs_yamllint_and_actionlint(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover([".github/workflows/ci.yml"]),
        run_tool=rec,
    )
    assert rc == 0
    assert (
        "yamllint",
        ("-c", _YAML_CFG, "--strict", ".github/workflows/ci.yml"),
    ) in rec.calls
    assert (
        "actionlint",
        ("-config-file", _ACTIONLINT_CFG, ".github/workflows/ci.yml"),
    ) in rec.calls


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
    with pytest.raises(execrun.ExecError) as exc_info:
        lint._run_tool("shipit-no-such-linter-xyz", ["a.py"], tmp_path)
    assert exc_info.value.cause == execrun.CAUSE_MISSING_BINARY


def test_run_tool_states_its_timeout_on_the_wire(tmp_path, monkeypatch):
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
    assert ("ruff", ("--config", _RUFF_CFG, "check", "--fix", "a.py")) in rec.calls
    assert ("ruff", ("--config", _RUFF_CFG, "format", "a.py")) in rec.calls
    assert ("lexd", ("check", "d.lex")) in rec.calls


def test_fix_mode_never_rewrites_a_protected_fixture(tmp_path, capsys):
    # #500: `--fix` must not hand a deliberately-malformed / byte-exact fixture
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["README.md", "tests/fixtures/broken.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert ("markdownlint", ("--config", _MD_CFG, "--fix", "README.md")) in rec.calls
    assert not any("tests/fixtures/broken.md" in args for _, args in rec.calls)


def test_fix_mode_reports_the_post_drop_count_in_note_and_log(tmp_path, capsys, caplog):
    rec = _Recorder()
    with caplog.at_level("DEBUG", logger="shipit.lint"):
        rc = lint.run(
            str(tmp_path),
            fix=True,
            discover=_fake_discover(["README.md", "tests/fixtures/broken.md"]),
            run_tool=rec,
        )
    assert rc == 0
    assert "markdownlint --config" in (out := capsys.readouterr().out)
    assert "--fix (1 file)" in out
    finished = [
        r
        for r in caplog.records
        if r.getMessage() == "lint tool finished"
        and getattr(r, "tool", None) == "markdownlint"
    ]
    assert finished, "expected a 'lint tool finished' record for markdownlint"
    assert all(r.files == 1 for r in finished)


def test_check_mode_still_passes_protected_fixtures_to_the_checkers(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["tests/fixtures/broken.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert (
        "markdownlint",
        ("--config", _MD_CFG, "tests/fixtures/broken.md"),
    ) in rec.calls


def test_fix_mode_check_form_tool_still_sees_a_protected_fixture(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["src/ok.sh", "tests/fixtures/bad.sh"]),
        run_tool=rec,
    )
    assert rc == 0
    assert (
        "shellcheck",
        ("--norc", "--severity=info", "src/ok.sh", "tests/fixtures/bad.sh"),
    ) in rec.calls
    shfmt_calls = [args for binary, args in rec.calls if binary == "shfmt"]
    assert shfmt_calls, "shfmt should have run its fix form"
    assert all("src/ok.sh" in args for args in shfmt_calls)
    assert all("tests/fixtures/bad.sh" not in args for args in shfmt_calls)


def test_fix_mode_skips_fixer_when_every_file_is_a_protected_fixture(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["tests/fixtures/a.md", "tests/fixtures/b.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert not any(binary == "markdownlint" for binary, _ in rec.calls)


def test_rust_runs_per_manifest_without_file_batches(tmp_path, capsys):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs", "src/lib.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    clippy = lint.RUST.tools[0].check
    fmt_check = lint.RUST.tools[1].check
    assert clippy == (
        "clippy",
        "--all",
        "--all-targets",
        "--all-features",
        "--",
        "-D",
        "warnings",
    )
    assert rec.calls.count(("cargo", clippy)) == 1
    assert rec.calls.count(("cargo", fmt_check)) == 1
    assert all(cwd == tmp_path for b, _, cwd in rec.cwds if b == "cargo")
    assert "rust" in capsys.readouterr().out


def test_rust_every_tracked_manifest_gets_its_own_run(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(
            [
                "Cargo.toml",
                "crates/a/Cargo.toml",
                "crates/a/src/lib.rs",
                "src-tauri/x.rs",
            ]
        ),
        run_tool=rec,
    )
    assert rc == 0
    cargo_cwds = [cwd for b, _, cwd in rec.cwds if b == "cargo"]
    assert set(cargo_cwds) == {tmp_path, tmp_path / "crates" / "a"}
    assert len(cargo_cwds) == 4


def test_rust_subdir_crate_runs_cwd_in_that_dir(tmp_path):
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
    rec = _Recorder(codes={"cargo": 101})
    rc = lint.run(str(tmp_path), discover=_fake_discover(["main.rs"]), run_tool=rec)
    assert rc == 1
    assert {cwd for b, _, cwd in rec.cwds if b == "cargo"} == {tmp_path}


def test_no_rust_paths_run_no_cargo(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "a.py", "README.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert all(binary != "cargo" for binary, _ in rec.calls)


def test_parse_cargo_version():
    assert lint.parse_cargo_version("cargo 1.96.0 (d1b87f7c9 2026-01-05)") == "1.96.0"
    assert (
        lint.parse_cargo_version("cargo 1.98.0-nightly (abc123 2026-06-01)") == "1.98.0"
    )
    assert lint.parse_cargo_version("warning: x\ncargo 1.90.2\n") == "1.90.2"
    assert lint.parse_cargo_version("rustc 1.96.0") is None
    assert lint.parse_cargo_version("") is None


def test_rust_pin_satisfied_common_shapes():
    assert lint.rust_pin_satisfied("1.96.0", "1.96.*")
    assert lint.rust_pin_satisfied("1.96.3", "1.96.*")
    assert not lint.rust_pin_satisfied("1.97.0", "1.96.*")
    assert not lint.rust_pin_satisfied("1.96.0", "1.9.*")
    assert lint.rust_pin_satisfied("1.96.0", "==1.96.0")
    assert not lint.rust_pin_satisfied("1.96.1", "==1.96.0")
    assert lint.rust_pin_satisfied("1.96.0", "1.96")
    assert lint.rust_pin_satisfied("1.96.0", "=1.96")
    assert lint.rust_pin_satisfied("2.0.0", "*")


def test_rust_pin_satisfied_unmodelled_shapes_never_claim_skew():
    assert lint.rust_pin_satisfied("1.80.0", ">=1.90")
    assert lint.rust_pin_satisfied("1.80.0", ">=1.90,<2")
    assert lint.rust_pin_satisfied("1.80.0", "~=1.96")
    assert lint.rust_pin_satisfied("1.80.0", "^1.96")
    assert lint.rust_pin_satisfied("1.80.0", "@ file:///opt/rust")
    assert lint.rust_pin_satisfied("1.96.0", "nightly")
    assert lint.rust_pin_satisfied("1.96.0", "stable")
    assert lint.rust_pin_satisfied("1.96.0", "1.96.0-nightly")
    assert lint.rust_pin_satisfied("1.96.0", "=")


def test_rust_pin_from_manifest_lint_feature_wins():
    data = {
        "feature": {"lint": {"dependencies": {"rust": "1.96.*"}}},
        "dependencies": {"rust": "1.90.*"},
    }
    assert lint.rust_pin_from_manifest(data) == "1.96.*"


def test_rust_pin_from_manifest_default_deps_and_dict_form():
    assert lint.rust_pin_from_manifest({"dependencies": {"rust": "1.90.*"}}) == "1.90.*"
    assert (
        lint.rust_pin_from_manifest(
            {"dependencies": {"rust": {"version": "1.90.*", "channel": "conda-forge"}}}
        )
        == "1.90.*"
    )
    assert lint.rust_pin_from_manifest({"dependencies": {}}) is None
    assert lint.rust_pin_from_manifest({}) is None
    assert lint.rust_pin_from_manifest("not a table") is None


def test_pinned_rust_spec_reads_pixi_toml(tmp_path):
    (tmp_path / "pixi.toml").write_text(
        '[feature.lint.dependencies]\nrust = "1.96.*"\n', encoding="utf-8"
    )
    assert lint._pinned_rust_spec(tmp_path) == "1.96.*"


def test_pinned_rust_spec_missing_or_malformed_manifest_is_none(tmp_path):
    assert lint._pinned_rust_spec(tmp_path) is None
    (tmp_path / "pixi.toml").write_text("not = [toml", encoding="utf-8")
    assert lint._pinned_rust_spec(tmp_path) is None


def test_detect_rust_skew():
    probe = "cargo 1.97.0 (abc123 2026-05-01)"
    note = lint.detect_rust_skew("1.96.*", probe)
    assert note is not None
    assert "1.97.0" in note and "1.96.*" in note
    assert lint.detect_rust_skew("1.97.*", probe) is None
    assert lint.detect_rust_skew(None, probe) is None
    assert lint.detect_rust_skew("1.96.*", None) is None
    assert lint.detect_rust_skew("1.96.*", "garbled") is None


class _SkewRecorder(_Recorder):
    def __init__(self, codes=None, cargo_version="cargo 1.97.0 (abc123 2026-05-01)"):
        super().__init__(codes)
        self.cargo_version = cargo_version

    def __call__(self, binary, args, cwd):
        if binary == "cargo" and list(args) == ["--version"]:
            self.calls.append((binary, tuple(args)))
            self.cwds.append((binary, tuple(args), cwd))
            return execrun.ExecResult(
                argv=(binary, *args),
                rc=0,
                stdout=self.cargo_version,
                stderr="",
                duration_ms=1,
            )
        return super().__call__(binary, args, cwd)


def _write_rust_pin(tmp_path, spec="1.96.*"):
    (tmp_path / "pixi.toml").write_text(
        f'[feature.lint.dependencies]\nrust = "{spec}"\n', encoding="utf-8"
    )


def test_rust_skew_downgrades_cargo_failure_to_warning(tmp_path, capsys):
    _write_rust_pin(tmp_path)
    rec = _SkewRecorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "TOOLCHAIN SKEW" in out
    assert "1.97.0" in out and "1.96.*" in out
    assert "LINT: OK" in out
    probes = [a for b, a in rec.calls if b == "cargo" and a == ("--version",)]
    assert len(probes) == 1


def test_rust_skew_probes_cargo_in_the_manifest_dir_not_root(tmp_path, capsys):
    _write_rust_pin(tmp_path)
    rec = _SkewRecorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["src-tauri/Cargo.toml", "src-tauri/src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    assert "TOOLCHAIN SKEW" in capsys.readouterr().out
    probe_cwds = [
        cwd for b, args, cwd in rec.cwds if b == "cargo" and args == ("--version",)
    ]
    assert probe_cwds == [tmp_path / "src-tauri"]


class _NoisySkewRecorder(_SkewRecorder):
    def __call__(self, binary, args, cwd):
        if binary == "cargo" and list(args) != ["--version"]:
            self.calls.append((binary, tuple(args)))
            self.cwds.append((binary, tuple(args), cwd))
            return execrun.ExecResult(
                argv=(binary, *args),
                rc=self.codes.get("cargo", 0),
                stdout="error[E0308]: mismatched types\n --> src/main.rs:3:5",
                stderr="",
                duration_ms=1,
            )
        return super().__call__(binary, args, cwd)


def test_rust_skew_note_carries_the_failing_cargo_output(tmp_path, capsys):
    _write_rust_pin(tmp_path)
    rec = _NoisySkewRecorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "TOOLCHAIN SKEW" in out
    assert "error[E0308]: mismatched types" in out
    assert "LINT: OK" in out


def test_rust_skew_never_masks_a_non_cargo_failure(tmp_path, capsys):
    _write_rust_pin(tmp_path)
    rec = _SkewRecorder(codes={"cargo": 101, "ruff": 1})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs", "a.py"]),
        run_tool=rec,
    )
    assert rc == 1
    assert "python:ruff" in capsys.readouterr().out


def test_rust_matching_pin_keeps_the_hard_fail(tmp_path, capsys):
    _write_rust_pin(tmp_path, "1.97.*")
    rec = _SkewRecorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 1
    assert "rust:cargo" in capsys.readouterr().out


def test_rust_no_pin_keeps_the_hard_fail_and_skips_the_probe(tmp_path):
    rec = _Recorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 1
    assert ("cargo", ("--version",)) not in rec.calls


def test_rust_skew_passing_cargo_run_prints_no_note(tmp_path, capsys):
    _write_rust_pin(tmp_path)
    rec = _SkewRecorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 0
    assert "TOOLCHAIN SKEW" not in capsys.readouterr().out


def test_rust_probe_failure_never_claims_skew(tmp_path, capsys):
    _write_rust_pin(tmp_path)
    boom = execrun.ExecError(["cargo"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)
    rec = _Recorder(codes={"cargo": boom})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 1
    assert "not found on PATH" in capsys.readouterr().out


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
    assert ("cargo", lint.RUST.tools[1].fix) in rec.calls
    assert (
        "cargo",
        ("clippy", "--all", "--all-targets", "--all-features", "--", "-D", "warnings"),
    ) in rec.calls


class _FakeCargoFmt:
    def __init__(self):
        self.calls = []

    def __call__(self, binary, args, cwd):
        self.calls.append((binary, tuple(args)))
        mutating_fmt = binary == "cargo" and "fmt" in args and "--check" not in args
        if mutating_fmt:
            for rs in Path(cwd).rglob("*.rs"):
                rs.write_text("// reformatted by the fixer\n")
        return execrun.ExecResult(
            argv=(binary, *args), rc=0, stdout="", stderr="", duration_ms=1
        )


def test_fix_mode_restores_a_mod_included_rust_fixture_cargo_fmt_rewrote(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    real = tmp_path / "src" / "lib.rs"
    fixture = tmp_path / "tests" / "fixtures" / "bad.rs"
    real.write_text("pub fn a()->i32{1}\n")
    original = "pub fn v()->i32{      1    }\n"
    fixture.write_text(original)

    fake = _FakeCargoFmt()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["Cargo.toml", "src/lib.rs", "tests/fixtures/bad.rs"]),
        run_tool=fake,
    )
    assert rc == 0
    assert fixture.read_bytes() == original.encode()
    assert real.read_text() == "// reformatted by the fixer\n"


def test_fix_mode_restores_a_fixture_that_is_itself_a_crate(tmp_path):
    (tmp_path / "tests" / "fixtures" / "bad-crate" / "src").mkdir(parents=True)
    fixture = tmp_path / "tests" / "fixtures" / "bad-crate" / "src" / "lib.rs"
    original = "pub fn v()->i32{      1    }\n"
    fixture.write_text(original)

    fake = _FakeCargoFmt()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(
            [
                "tests/fixtures/bad-crate/Cargo.toml",
                "tests/fixtures/bad-crate/src/lib.rs",
            ]
        ),
        run_tool=fake,
    )
    assert rc == 0
    assert fixture.read_bytes() == original.encode()


def test_check_mode_does_not_snapshot_or_touch_rust_fixtures(tmp_path):
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    fixture = tmp_path / "tests" / "fixtures" / "bad.rs"
    original = "pub fn v()->i32{      1    }\n"
    fixture.write_text(original)

    fake = _FakeCargoFmt()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "tests/fixtures/bad.rs"]),
        run_tool=fake,
    )
    assert rc == 0
    assert fixture.read_bytes() == original.encode()


def test_shell_routed_by_shebang_runs_shellcheck(tmp_path, capsys):
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    rec = _Recorder()
    rc = lint.run(str(tmp_path), discover=_fake_discover(["tool"]), run_tool=rec)
    assert rc == 0
    assert any(binary == "shellcheck" for binary, _ in rec.calls)
    assert any(binary == "shfmt" for binary, _ in rec.calls)


def _fail_when_file_present(dirty_binary, dirty_file):
    calls = []

    def run_tool(binary, args, cwd):
        calls.append((binary, tuple(args)))
        rc = 1 if binary == dirty_binary and dirty_file in args else 0
        return execrun.ExecResult(
            argv=(binary, *args), rc=rc, stdout="", stderr="dirty", duration_ms=1
        )

    run_tool.calls = calls
    return run_tool


def test_lint_ignore_excludes_dirty_fixture_gate_green(tmp_path, capsys):
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = ["tests/fixtures/**"]\n')
    run_tool = _fail_when_file_present("markdownlint", "tests/fixtures/ref.md")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["README.md", "tests/fixtures/ref.md"]),
        run_tool=run_tool,
    )
    assert rc == 0
    assert "LINT: OK" in capsys.readouterr().out
    md_batches = [args for binary, args in run_tool.calls if binary == "markdownlint"]
    assert md_batches == [("--config", _MD_CFG, "README.md")]


def test_same_fixture_un_ignored_gate_red(tmp_path, capsys):
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = ["other/**"]\n')
    run_tool = _fail_when_file_present("markdownlint", "tests/fixtures/ref.md")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["README.md", "tests/fixtures/ref.md"]),
        run_tool=run_tool,
    )
    assert rc == 1
    assert "LINT: FAILED" in capsys.readouterr().out


def test_lint_ignore_is_lang_agnostic(tmp_path, capsys):
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = ["vendor/**"]\n')
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["src/app.py", "vendor/synced.py"]),
        run_tool=rec,
    )
    assert rc == 0
    ruff_batches = [args for binary, args in rec.calls if binary == "ruff"]
    assert ruff_batches and all("vendor/synced.py" not in args for args in ruff_batches)
    assert all("src/app.py" in args for args in ruff_batches)


def test_no_shipit_toml_means_no_ignore(tmp_path, capsys):
    run_tool = _fail_when_file_present("markdownlint", "tests/fixtures/ref.md")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["tests/fixtures/ref.md"]),
        run_tool=run_tool,
    )
    assert rc == 1


def test_lint_ignore_directory_prefix_drops_generated_subtree(tmp_path, capsys):
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = ["CHANGELOG/"]\n')
    run_tool = _fail_when_file_present("markdownlint", "CHANGELOG/0.15.0.md")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["README.md", "CHANGELOG/0.15.0.md"]),
        run_tool=run_tool,
    )
    assert rc == 0
    assert "LINT: OK" in capsys.readouterr().out
    md_batches = [args for binary, args in run_tool.calls if binary == "markdownlint"]
    assert md_batches == [("--config", _MD_CFG, "README.md")]


def test_run_pins_shfmt_and_prettier_when_no_editorconfig_tracked(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["run.sh", "data.json"]),
        run_tool=rec,
        tracks_root_editorconfig=lambda root: False,
    )
    assert rc == 0
    assert ("shfmt", ("-i", "0", "-d", "run.sh")) in rec.calls
    assert (
        "prettier",
        (
            "--config",
            _PRETTIER_CFG,
            "--no-editorconfig",
            "--check",
            "--log-level",
            "warn",
            "data.json",
        ),
    ) in rec.calls


def test_run_does_not_pin_when_repo_tracks_editorconfig(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["run.sh", "data.json"]),
        run_tool=rec,
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 0
    assert ("shfmt", ("-d", "run.sh")) in rec.calls
    assert (
        "prettier",
        ("--config", _PRETTIER_CFG, "--check", "--log-level", "warn", "data.json"),
    ) in rec.calls


def test_run_pin_decision_independent_of_lint_ignore(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = [".editorconfig"]\n')
    (tmp_path / ".editorconfig").write_text("root = true\n")
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: str(tmp_path))
    monkeypatch.setattr(
        lint.git, "ls_files", lambda *, cwd: [".editorconfig", "run.sh"]
    )
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["run.sh", "data.json"]),
        run_tool=rec,
    )
    assert rc == 0
    assert ("shfmt", ("-d", "run.sh")) in rec.calls
    assert (
        "prettier",
        ("--config", _PRETTIER_CFG, "--check", "--log-level", "warn", "data.json"),
    ) in rec.calls


@pytest.mark.skipif(
    shutil.which("shfmt") is None or shutil.which("shellcheck") is None,
    reason="shell linters (shfmt/shellcheck) not on PATH in this env",
)
def test_shfmt_verdict_is_hermetic_across_ambient_editorconfig(tmp_path):
    (tmp_path / "script.sh").write_text("#!/bin/bash\nif true; then\n\techo hi\nfi\n")
    discover = _fake_discover(["script.sh"])
    pinned = {"tracks_root_editorconfig": lambda root: False}

    assert lint.run(str(tmp_path), discover=discover, **pinned) == 0

    (tmp_path / ".editorconfig").write_text(
        "root = true\n[*]\nindent_style = space\nindent_size = 2\n"
    )
    assert lint.run(str(tmp_path), discover=discover, **pinned) == 0


@pytest.mark.skipif(
    shutil.which("shfmt") is None or shutil.which("shellcheck") is None,
    reason="shell linters (shfmt/shellcheck) not on PATH in this env",
)
def test_tracked_non_root_editorconfig_keeps_pin_on_hostile_ancestor(
    tmp_path, monkeypatch
):
    base = tmp_path / "base"
    repo = base / "repo"
    repo.mkdir(parents=True)
    (repo / "t.sh").write_text("#!/bin/bash\nif true; then\n\techo hi\nfi\n")
    (repo / ".editorconfig").write_text(
        "[*.md]\nindent_style = space\nindent_size = 2\n"
    )
    (base / ".editorconfig").write_text(_HOSTILE_EDITORCONFIG)

    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: str(repo))
    monkeypatch.setattr(lint.git, "ls_files", lambda *, cwd: [".editorconfig", "t.sh"])

    assert lint._tracks_root_editorconfig(repo) is False

    runs: list[lint.ToolRun] = []
    rc = lint.run(str(repo), discover=_fake_discover(["t.sh"]), runs_out=runs)
    shfmt_run = next(r for r in runs if r.binary == "shfmt")
    assert shfmt_run.ok is True
    assert rc == 0


def test_malformed_shipit_toml_fails_clean_not_traceback(tmp_path, capsys):
    (tmp_path / ".shipit.toml").write_text("[lint]\nignore = 42\n")
    rc = lint_verb.run(
        str(tmp_path),
        discover=_fake_discover(["README.md"]),
        run_tool=_Recorder(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "list of glob strings" in err


def _prettier_output(rc, output):

    def run_tool(binary, args, cwd):
        if binary == "prettier":
            return execrun.ExecResult(
                argv=(binary, *args), rc=rc, stdout="", stderr=output, duration_ms=1
            )
        return execrun.ExecResult(
            argv=(binary, *args), rc=0, stdout="", stderr="", duration_ms=1
        )

    return run_tool


def test_is_prettier_plugin_load_failure_matches_the_resolver_class():
    pkg = (
        "[error] Cannot find package 'prettier-plugin-svelte' imported from /x/noop.js"
    )
    mod = "[error] Cannot find module 'prettier-plugin-tailwindcss' imported from /x/noop.js"
    assert lint.is_prettier_plugin_load_failure("prettier", 1, pkg)
    assert lint.is_prettier_plugin_load_failure("prettier", 1, mod)


def test_is_prettier_plugin_load_failure_never_swallows_a_real_failure():
    dirty = "[warn] data.json\n[warn] Code style issues found in the above file."
    assert not lint.is_prettier_plugin_load_failure("prettier", 1, dirty)
    assert not lint.is_prettier_plugin_load_failure("prettier", 0, "")
    other = "Cannot find package 'x' imported from y"
    assert not lint.is_prettier_plugin_load_failure("markdownlint", 1, other)
    assert not lint.is_prettier_plugin_load_failure(
        "prettier", 1, "Cannot find module 'x'\nRequire stack: ..."
    )


def test_partition_plugin_scoped_splits_by_extension():
    paths = ["a.json", "src/W.svelte", "b.ts", "c.tsx", "d/E.svelte"]
    free, scoped = lint.partition_plugin_scoped(paths, (".svelte",))
    assert free == ["a.json", "b.ts", "c.tsx"]
    assert scoped == ["src/W.svelte", "d/E.svelte"]


def test_partition_plugin_scoped_no_declared_extensions_is_all_free():
    paths = ["a.py", "b.rs"]
    free, scoped = lint.partition_plugin_scoped(paths, ())
    assert free == paths
    assert scoped == []


def test_prettier_svelte_leg_fails_open(tmp_path, capsys):
    err = (
        "[error] Cannot find package 'prettier-plugin-svelte' imported from /x/noop.js"
    )
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Widget.svelte"]),
        run_tool=_prettier_output(1, err),
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "LINT: OK" in out
    assert "web:prettier" not in out
    assert "not installed" in out
    assert "ok   web" in out


def test_prettier_json_leg_never_fails_open(tmp_path, capsys):
    err = (
        "[error] Cannot find package 'prettier-plugin-svelte' imported from /x/noop.js"
    )
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        run_tool=_prettier_output(1, err),
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "LINT: FAILED" in out
    assert "web:prettier" in out


def test_prettier_svelte_abort_does_not_mask_dirty_json(tmp_path, capsys):
    dirty = "[warn] data.json\n[warn] Code style issues found in the above file."
    plugin_abort = (
        "[error] Cannot find package 'prettier-plugin-svelte' imported from /x/noop.js"
    )

    def run_tool(binary, args, cwd):
        if binary == "prettier":
            svelte = any(a.endswith(".svelte") for a in args)
            return execrun.ExecResult(
                argv=(binary, *args),
                rc=1,
                stdout="",
                stderr=plugin_abort if svelte else dirty,
                duration_ms=1,
            )
        return execrun.ExecResult(
            argv=(binary, *args), rc=0, stdout="", stderr="", duration_ms=1
        )

    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json", "Widget.svelte"]),
        run_tool=run_tool,
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "LINT: FAILED" in out
    assert "web:prettier" in out
    assert "not installed" in out


def test_prettier_dirty_json_still_fails_in_orchestrator(tmp_path, capsys):
    dirty = "[warn] data.json\n[warn] Code style issues found in the above file."
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        run_tool=_prettier_output(1, dirty),
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "LINT: FAILED" in out
    assert "web:prettier" in out


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_missing_plugin_fixture_fails_open_real(tmp_path):
    (tmp_path / ".prettierrc").write_text(
        '{\n  "plugins": ["prettier-plugin-absent-498"]\n}\n'
    )
    (tmp_path / "data.json").write_text('{ "a": 1 }\n')
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 0


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_dirty_json_still_fails_real(tmp_path):
    (tmp_path / "data.json").write_text('{"a":      1}\n')
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        tracks_root_editorconfig=lambda root: True,
        canonical_config=lambda tool, root: None,
    )
    assert rc == 1


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_dirty_json_still_fails_under_real_canonical_config(tmp_path):
    (tmp_path / "data.json").write_text('{"a":      1}\n')
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 1


def test_config_inject_omitted_when_no_path_is_resolved():
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.config_inject == ("--config", lint.CONFIG_PLACEHOLDER)
    assert ruff_check.argv(fix=False) == ("check",)
    assert ruff_check.argv(fix=False, config_path=None) == ("check",)


def test_config_inject_substitutes_and_prepends_the_path_unconditionally():
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.argv(fix=False, config_path="/canon/ruff.toml") == (
        "--config",
        "/canon/ruff.toml",
        "check",
    )
    assert ruff_check.argv(fix=True, config_path="/canon/ruff.toml") == (
        "--config",
        "/canon/ruff.toml",
        "check",
        "--fix",
    )


def test_config_inject_inline_fragment_is_always_applied():
    tool = lint.Tool("demo", ("--check",), config_inject=("--std",))
    assert tool.argv(fix=False) == ("--std", "--check")
    assert tool.argv(fix=False, config_path="/ignored") == ("--std", "--check")


def test_config_inject_substring_placeholder_form():
    tool = lint.Tool("demo", ("--check",), config_inject=("--config={config}",))
    assert tool.argv(fix=False, config_path="/canon/demo.toml") == (
        "--config=/canon/demo.toml",
        "--check",
    )
    assert tool.argv(fix=False) == ("--check",)
    assert tool.argv(fix=False, config_path=None) == ("--check",)


def test_config_inject_coexists_with_the_editorconfig_pin():
    prettier = lint.WEB.tools[0]
    assert prettier.config_inject == ("--config", lint.CONFIG_PLACEHOLDER)
    assert prettier.editorconfig_pin == ("--no-editorconfig",)
    assert prettier.argv(
        fix=False, pin_editorconfig=True, config_path="/canon/.prettierrc"
    ) == (
        "--config",
        "/canon/.prettierrc",
        "--no-editorconfig",
        "--check",
        "--log-level",
        "warn",
    )


def test_every_file_config_tool_declares_its_injection_point():
    file_config_binaries = {"ruff", "prettier", "markdownlint", "yamllint"}
    for lang in lint.LANGS:
        for tool in lang.tools:
            if tool.binary in file_config_binaries:
                assert any(
                    lint.CONFIG_PLACEHOLDER in tok for tok in tool.config_inject
                ), tool.binary


def test_run_injects_the_resolved_canonical_config_path(tmp_path):
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["a.py"]),
        run_tool=rec,
        canonical_config=lambda tool, root: "/canon/ruff.toml",
    )
    assert rc == 0
    assert ("ruff", ("--config", "/canon/ruff.toml", "check", "a.py")) in rec.calls
    assert (
        "ruff",
        ("--config", "/canon/ruff.toml", "format", "--check", "a.py"),
    ) in rec.calls


def test_run_default_resolver_injects_the_canonical_configs(tmp_path):
    rec = _Recorder()
    lint.run(str(tmp_path), discover=_fake_discover(["a.py", "d.lex"]), run_tool=rec)
    assert ("ruff", ("--config", _RUFF_CFG, "check", "a.py")) in rec.calls
    assert ("ruff", ("--config", _RUFF_CFG, "format", "--check", "a.py")) in rec.calls
    assert ("lexd", ("check", "d.lex")) in rec.calls


def test_canonical_config_maps_file_config_tools_and_only_those(tmp_path):
    resolved = {}
    for lang in lint.LANGS:
        for tool in lang.tools:
            resolved[tool.binary] = lint._canonical_config(tool, tmp_path)
    for binary in ("ruff", "prettier", "markdownlint", "yamllint"):
        path = resolved[binary]
        assert path is not None and Path(path).is_file(), binary
    for binary in ("shellcheck", "shfmt", "cargo", "lexd"):
        assert resolved[binary] is None, binary


def test_shipped_actionlint_config_declares_the_org_runner_labels():
    body = Path(lint.data_path("actionlint.yaml")).read_text(encoding="utf-8")
    config = yaml.safe_load(body)
    assert "gpu_t4" in config["self-hosted-runner"]["labels"]


def test_rust_fmt_injects_the_shipped_rustfmt_config():
    fmt = lint.RUST.tools[1]
    assert "--config-path" in fmt.check and "--config-path" in fmt.fix
    assert fmt.check[fmt.check.index("--config-path") + 1] == lint._RUSTFMT_CONFIG_PATH
    assert Path(lint._RUSTFMT_CONFIG_PATH).is_file()


def test_data_path_resolves_a_real_file_and_fails_fast_when_missing():
    path = lint._data_path("ruff.toml")
    assert Path(path).is_file()
    with pytest.raises(FileNotFoundError):
        lint._data_path("no-such-canonical-config.toml")


def test_shipped_ruff_toml_matches_the_repo_root_carve_out():
    data = Path(lint.data_path("ruff.toml")).read_bytes()
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / "ruff.toml").read_bytes() == data
    pyproject_lines = (repo_root / "pyproject.toml").read_text().splitlines()
    assert not any(line.lstrip().startswith("[tool.ruff") for line in pyproject_lines)


def test_is_ambient_config_var_matches_the_leaky_keys():
    for leaky in (
        "HOME",
        "SHELLCHECK_OPTS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "YAMLLINT_CONFIG_FILE",
        "RUFF_CONFIG",
        "CARGO_HOME",
        "CLIPPY_CONF_DIR",
    ):
        assert lint._is_ambient_config_var(leaky), leaky
    # PATH and the tool runtime must survive — scrubbing them would break launch.
    for kept in (
        "PATH",
        "LANG",
        "TERM",
        "PIXI_PROJECT_ROOT",
        "PKG_CONFIG_PATH",
        "FONTCONFIG_PATH",
    ):
        assert not lint._is_ambient_config_var(kept), kept


def test_scrubbed_env_drops_ambient_config_keeps_path(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/someone/.config")
    monkeypatch.setenv("YAMLLINT_CONFIG_FILE", "/home/someone/hostile.yml")
    monkeypatch.setenv("SHELLCHECK_OPTS", "--enable=all")
    monkeypatch.setenv("CARGO_HOME", "/home/someone/.cargo")
    monkeypatch.setenv("CLIPPY_CONF_DIR", "/home/someone/clippy")
    monkeypatch.setenv("RUFF_CONFIG", "/home/someone/hostile-ruff.toml")
    monkeypatch.setenv("PATH", "/usr/bin")
    # Standard build vars that MUST survive — the old `_CONFIG` substring stripped
    monkeypatch.setenv("PKG_CONFIG_PATH", "/usr/lib/pkgconfig")
    monkeypatch.setenv("FONTCONFIG_PATH", "/etc/fonts")
    scrubbed = lint._scrubbed_env()
    assert "HOME" not in scrubbed
    assert "XDG_CONFIG_HOME" not in scrubbed
    assert "YAMLLINT_CONFIG_FILE" not in scrubbed
    assert "SHELLCHECK_OPTS" not in scrubbed
    assert "CARGO_HOME" not in scrubbed
    assert "CLIPPY_CONF_DIR" not in scrubbed
    assert "RUFF_CONFIG" not in scrubbed
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["PKG_CONFIG_PATH"] == "/usr/lib/pkgconfig"
    assert scrubbed["FONTCONFIG_PATH"] == "/etc/fonts"


def test_run_tool_passes_scrubbed_env_with_replace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("PATH", "/usr/bin")
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(lint.execrun, "run", fake_run)
    lint._run_tool("ruff", ["check", "a.py"], tmp_path)
    assert captured["replace_env"] is True
    assert "HOME" not in captured["env"]
    assert captured["env"]["PATH"] == "/usr/bin"


@dataclass(frozen=True)
class _Vector:
    name: str
    xfail: str | None = None
    kind: str | None = None
    hostile_name: str | None = None
    hostile_body: str | None = None
    home_rel: str | None = None

    @property
    def source(self) -> str:
        return self.kind or self.name


@dataclass(frozen=True)
class _ToolSpec:
    lang: str
    tag: str
    target_check: tuple[str, ...]
    binaries: tuple[str, ...]
    fixture: tuple[tuple[str, str], ...]
    hostile_name: str
    hostile_body: str
    vectors: tuple[_Vector, ...]
    expected_ok: bool
    env_var: str | None = None
    env_kind: str = "file"
    env_flags: str | None = None
    home_rel: str | None = None
    home_via_lexd: bool = False
    pluginless_prettier: bool = False


_RUST_CARGO_TOML = '[package]\nname = "hermtest"\nversion = "0.0.0"\nedition = "2021"\n'
_RUST_LIB = "pub fn f(a: i32, b: i32, c: i32) -> i32 {\n    a + b + c\n}\n"
_RUST_FIXTURE = (("Cargo.toml", _RUST_CARGO_TOML), ("src/lib.rs", _RUST_LIB))
_RUST_BINS = ("cargo", "cargo-clippy", "cargo-fmt", "clippy-driver", "rustfmt")
_SHFMT_FIXTURE = (("t.sh", "#!/bin/bash\nif true; then\n\techo hi\nfi\n"),)
_HOSTILE_EDITORCONFIG = "root = true\n[*.sh]\nindent_style = space\nindent_size = 2\n"
_LEX_DOC = ":: lex.totallybogusdirective ::\n\nBody.\n"
_LEX_HOSTILE = (
    '[diagnostics.rules]\nunknown_lex_canonical = "allow"\n\n'
    '[diagnostics.rules.schema]\nunknown_label = "allow"\n'
)

_HERM_SPECS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        lang="python",
        tag="ruff-check",
        target_check=("check",),
        binaries=("ruff",),
        fixture=(("m.py", "x = 1\n"),),
        hostile_name="ruff.toml",
        hostile_body='line-length = 1\n[lint]\nselect = ["E501"]\n',
        vectors=(_Vector("env"), _Vector("home"), _Vector("ancestor")),
        expected_ok=True,
        env_var="RUFF_CONFIG",
        env_kind="file",
        home_rel=".config/ruff/ruff.toml",
    ),
    _ToolSpec(
        lang="python",
        tag="ruff-format",
        target_check=("format", "--check"),
        binaries=("ruff",),
        fixture=(("m.py", 'x = "hi"\n'),),
        hostile_name="ruff.toml",
        hostile_body='[format]\nquote-style = "single"\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=True,
    ),
    _ToolSpec(
        lang="rust",
        tag="cargo-clippy",
        target_check=(
            "clippy",
            "--all",
            "--all-targets",
            "--all-features",
            "--",
            "-D",
            "warnings",
        ),
        binaries=_RUST_BINS,
        fixture=_RUST_FIXTURE,
        hostile_name="clippy.toml",
        hostile_body="too-many-arguments-threshold = 2\n",
        vectors=(
            _Vector("env"),
            _Vector(
                "ancestor",
                xfail="#526 (item 2): clippy walks ancestors for clippy.toml; env-scrub insufficient",
            ),
            _Vector(
                "ancestor-cargo-config",
                kind="ancestor",
                xfail="#526 (item 3): cargo walks ancestors for .cargo/config.toml; env-scrub insufficient",
                hostile_name=".cargo/config.toml",
                hostile_body='[build]\nrustflags = ["-Dclippy::arithmetic_side_effects"]\n',
            ),
        ),
        expected_ok=True,
        env_var="CLIPPY_CONF_DIR",
        env_kind="dir",
    ),
    _ToolSpec(
        lang="rust",
        tag="cargo-fmt",
        target_check=(
            "fmt",
            "--all",
            "--",
            "--check",
            "--config-path",
            lint._RUSTFMT_CONFIG_PATH,
        ),
        binaries=_RUST_BINS,
        fixture=_RUST_FIXTURE,
        hostile_name="rustfmt.toml",
        hostile_body="tab_spaces = 2\n",
        vectors=(_Vector("ancestor"),),
        expected_ok=True,
    ),
    _ToolSpec(
        lang="shell",
        tag="shellcheck",
        target_check=("--norc", "--severity=info"),
        binaries=("shellcheck",),
        fixture=(("s.sh", "#!/bin/bash\necho $1\n"),),
        hostile_name=".shellcheckrc",
        hostile_body="disable=SC2086\n",
        vectors=(_Vector("env"), _Vector("home"), _Vector("ancestor")),
        expected_ok=False,
        env_var="SHELLCHECK_OPTS",
        env_kind="flags",
        env_flags="--exclude=SC2086",
        home_rel=".shellcheckrc",
    ),
    _ToolSpec(
        lang="shell",
        tag="shfmt",
        target_check=("-d",),
        binaries=("shfmt",),
        fixture=_SHFMT_FIXTURE,
        hostile_name=".editorconfig",
        hostile_body=_HOSTILE_EDITORCONFIG,
        vectors=(_Vector("ancestor"),),
        expected_ok=True,
    ),
    _ToolSpec(
        lang="yaml",
        tag="yamllint",
        target_check=("--strict",),
        binaries=("yamllint",),
        fixture=(("d.yml", "b: 2\na: 1\n"),),
        hostile_name=".yamllint",
        hostile_body="extends: default\nrules:\n  key-ordering: enable\n",
        vectors=(_Vector("env"), _Vector("home"), _Vector("ancestor")),
        expected_ok=True,
        env_var="YAMLLINT_CONFIG_FILE",
        env_kind="file",
        home_rel=".config/yamllint/config",
    ),
    _ToolSpec(
        lang="actions",
        tag="actionlint",
        target_check=(),
        binaries=("actionlint",),
        fixture=(
            (".git/HEAD", "ref: refs/heads/main\n"),
            (
                ".github/workflows/ci.yml",
                "on: push\n"
                "jobs:\n"
                "  j:\n"
                "    runs-on: my-gpu-box\n"
                "    steps:\n"
                "      - run: echo ok\n",
            ),
        ),
        hostile_name=".github/actionlint.yaml",
        hostile_body="self-hosted-runner:\n  labels: [my-gpu-box]\n",
        vectors=(_Vector("cwd"),),
        expected_ok=False,
    ),
    _ToolSpec(
        lang="web",
        tag="prettier",
        target_check=("--check", "--log-level", "warn"),
        binaries=("prettier",),
        fixture=(("data.json", '{\n  "a": {\n    "b": 1\n  }\n}\n'),),
        hostile_name=".prettierrc",
        hostile_body='{"tabWidth": 8}\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=True,
    ),
    _ToolSpec(
        lang="markdown",
        tag="markdownlint",
        target_check=(),
        binaries=("markdownlint",),
        fixture=(("r.md", "# Title\n\nHello world, this is a line of prose.\n"),),
        hostile_name=".markdownlint.json",
        hostile_body='{"default": false, "MD013": {"line_length": 3}}\n',
        vectors=(_Vector("cwd"),),
        expected_ok=True,
    ),
    _ToolSpec(
        lang="lex",
        tag="lexd",
        target_check=("check",),
        binaries=("lexd",),
        fixture=(("doc.lex", _LEX_DOC),),
        hostile_name=".lex.toml",
        hostile_body=_LEX_HOSTILE,
        vectors=(
            _Vector("home"),
            _Vector(
                "ancestor",
                xfail="#526: lexd walks ancestors for .lex.toml; env-scrub insufficient",
            ),
        ),
        expected_ok=False,
        home_via_lexd=True,
    ),
    _ToolSpec(
        lang="lua",
        tag="stylua",
        target_check=("--check",),
        binaries=("stylua",),
        fixture=(
            (
                "m.lua",
                "local M = {}\n\nlocal function f()\n\treturn 1\nend\n\nreturn M\n",
            ),
        ),
        hostile_name="stylua.toml",
        hostile_body='indent_type = "Spaces"\nindent_width = 2\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=True,
    ),
    _ToolSpec(
        lang="lua",
        tag="selene",
        target_check=(),
        binaries=("selene",),
        fixture=(("m.lua", "local unused = 5\nprint(1)\n"),),
        hostile_name="selene.toml",
        hostile_body='std = "lua51"\n[rules]\nunused_variable = "allow"\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=False,
    ),
)


def _spec(tag: str) -> _ToolSpec:
    return next(s for s in _HERM_SPECS if s.tag == tag)


def _tool_for(spec: _ToolSpec) -> lint.Tool:
    lang = next(lang for lang in lint.LANGS if lang.name == spec.lang)
    return next(t for t in lang.tools if t.check == spec.target_check)


def _target_run(runs: list[lint.ToolRun], tool: lint.Tool) -> lint.ToolRun:
    n = len(tool.check)
    expected = " ".join(tool.check)
    matches = [
        r
        for r in runs
        if r.binary == tool.binary
        and (n == 0 or r.label.endswith(f" {expected}") or r.label == expected)
    ]
    assert len(matches) == 1, (
        f"expected exactly one run for {tool.binary} {tool.check}, got "
        f"{len(matches)} — labels seen: {[r.label for r in runs]}"
    )
    return matches[0]


def _none_resolver(tool: lint.Tool, root: Path) -> str | None:
    return None


def _pluginless_resolver(pluginless_path: Path):

    def resolve(tool: lint.Tool, root: Path) -> str | None:
        if tool.binary == "prettier":
            return str(pluginless_path)
        return lint._canonical_config(tool, root)

    return resolve


def _ambient_runs(base, spec, vector, *, planted, monkeypatch, canonical):
    base = Path(base)
    repo = base / "repo"
    repo.mkdir(parents=True)
    for rel, content in spec.fixture:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    hostile_name = (
        vector.hostile_name if vector.hostile_name is not None else spec.hostile_name
    )
    hostile_body = (
        vector.hostile_body if vector.hostile_body is not None else spec.hostile_body
    )

    def _plant(dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(hostile_body)

    if spec.env_var:
        monkeypatch.delenv(spec.env_var, raising=False)

    if vector.source == "ancestor":
        if planted:
            _plant(base / hostile_name)
    elif vector.source == "cwd":
        if planted:
            _plant(repo / hostile_name)
    elif vector.source == "env":
        if planted:
            if spec.env_kind == "flags":
                monkeypatch.setenv(spec.env_var, spec.env_flags)
            elif spec.env_kind == "dir":
                d = base / "envdir"
                d.mkdir()
                _plant(d / hostile_name)
                monkeypatch.setenv(spec.env_var, str(d))
            else:
                cfg = base / hostile_name
                _plant(cfg)
                monkeypatch.setenv(spec.env_var, str(cfg))
    elif vector.source == "home":
        home = base / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        if planted:
            if spec.home_via_lexd:
                for rule in (
                    "diagnostics.rules.unknown_lex_canonical",
                    "diagnostics.rules.schema.unknown_label",
                ):
                    subprocess.run(
                        ["lexd", "config", "set", "--scope", "user", rule, "allow"],
                        env={**os.environ, "HOME": str(home)},
                        check=True,
                        capture_output=True,
                    )
            else:
                home_rel = (
                    vector.home_rel if vector.home_rel is not None else spec.home_rel
                )
                _plant(home / home_rel)
    else:  # pragma: no cover - guarded by the param builder
        raise AssertionError(f"unknown vector {vector.source!r}")

    runs: list[lint.ToolRun] = []
    lint.run(
        str(repo),
        discover=_fake_discover([rel for rel, _ in spec.fixture]),
        tracks_root_editorconfig=lambda root: False,
        canonical_config=canonical,
        runs_out=runs,
    )
    return runs


def _tool_id(lang: lint.Lang, tool: lint.Tool) -> str:
    disc = "-".join(tool.check) if tool.check else "noargs"
    return f"{lang.name}-{tool.binary}-{disc}"


def _hermeticity_cases():
    specs = {(s.lang, s.target_check): s for s in _HERM_SPECS}
    cases = []
    for lang in lint.LANGS:
        for tool in lang.tools:
            spec = specs.get((lang.name, tool.check))
            if spec is None:
                cases.append(
                    pytest.param(None, None, id=f"{_tool_id(lang, tool)}-NO-SPEC")
                )
                continue
            missing = [b for b in spec.binaries if shutil.which(b) is None]
            skip = pytest.mark.skipif(
                bool(missing), reason=f"{spec.tag}: {missing} not on PATH"
            )
            for vec in spec.vectors:
                marks = [skip]
                if vec.xfail:
                    marks.append(pytest.mark.xfail(strict=True, reason=vec.xfail))
                cases.append(
                    pytest.param(
                        spec,
                        vec,
                        id=f"{spec.lang}-{spec.tag}-{vec.name}",
                        marks=marks,
                    )
                )
    return cases


CORE_LINT_TOOLS = (
    "ruff",
    "shellcheck",
    "shfmt",
    "yamllint",
    "actionlint",
    "prettier",
    "markdownlint",
)


def test_core_lint_tools_present_so_hermeticity_cases_cannot_silently_skip():
    missing = [t for t in CORE_LINT_TOOLS if shutil.which(t) is None]
    assert not missing, (
        f"core lint tool(s) missing from PATH: {missing}. The hermeticity gate "
        f"`skipif`s an absent tool, so a missing CORE binary makes its cases skip "
        f"and the gate pass for the WRONG reason. Run in shipit's pixi env "
        f"(`pixi run test`), where pixi.toml provisions these."
    )


def test_every_registered_tool_has_a_hermeticity_spec():
    registered = {(lang.name, tool.check) for lang in lint.LANGS for tool in lang.tools}
    specced = {(s.lang, s.target_check) for s in _HERM_SPECS}
    assert registered <= specced, (
        f"registered tool(s) with no _ToolSpec (add one — each tool must prove "
        f"hermeticity or xfail its leak): "
        f"{sorted((lang, ' '.join(chk)) for lang, chk in registered - specced)}"
    )


@pytest.mark.parametrize("spec,vector", _hermeticity_cases())
def test_lint_tool_is_ambient_config_blind(spec, vector, tmp_path, monkeypatch):
    if spec is None:
        pytest.fail(
            "registered tool has no _ToolSpec — add one so it is subject to the "
            "hermeticity invariant (see test_every_registered_tool_...)"
        )
    tool = _tool_for(spec)
    canonical = lint._canonical_config
    if spec.pluginless_prettier:
        pluginless = tmp_path / "pluginless-prettierrc.yaml"
        pluginless.write_text(
            "singleQuote: true\ntabWidth: 2\nsemi: false\ntrailingComma: none\n"
        )
        canonical = _pluginless_resolver(pluginless)

    clean = _target_run(
        _ambient_runs(
            tmp_path / "clean",
            spec,
            vector,
            planted=False,
            monkeypatch=monkeypatch,
            canonical=canonical,
        ),
        tool,
    )
    assert clean.ok == spec.expected_ok, (
        f"{spec.tag} clean baseline ok={clean.ok}, expected {spec.expected_ok} — "
        f"the fixture drifted; the per-tool invariance would be VACUOUS. Fix the "
        f"fixture or expected_ok, don't let a hollow pass hide a broken gate"
    )
    hostile = _target_run(
        _ambient_runs(
            tmp_path / "hostile",
            spec,
            vector,
            planted=True,
            monkeypatch=monkeypatch,
            canonical=canonical,
        ),
        tool,
    )
    assert clean.ok == hostile.ok, (
        f"{spec.tag} verdict moved under a hostile {vector.name} config "
        f"(clean.ok={clean.ok}, hostile.ok={hostile.ok}) — the gate leaks that source"
    )


def _teeth_target_oks(tmp_path, spec, tool, monkeypatch, canonical):
    clean = _target_run(
        _ambient_runs(
            tmp_path / "c",
            spec,
            _Vector("ancestor"),
            planted=False,
            monkeypatch=monkeypatch,
            canonical=canonical,
        ),
        tool,
    )
    hostile = _target_run(
        _ambient_runs(
            tmp_path / "h",
            spec,
            _Vector("ancestor"),
            planted=True,
            monkeypatch=monkeypatch,
            canonical=canonical,
        ),
        tool,
    )
    return clean.ok, hostile.ok


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_gate_has_teeth_removing_ruff_check_config_reddens(tmp_path, monkeypatch):
    spec = _spec("ruff-check")
    oks = _teeth_target_oks(
        tmp_path, spec, _tool_for(spec), monkeypatch, _none_resolver
    )
    assert oks == (True, False)


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_gate_has_teeth_removing_ruff_format_config_reddens(tmp_path, monkeypatch):
    spec = _spec("ruff-format")
    oks = _teeth_target_oks(
        tmp_path, spec, _tool_for(spec), monkeypatch, _none_resolver
    )
    assert oks == (True, False)


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not on PATH")
def test_gate_has_teeth_removing_actionlint_config_file_greens(tmp_path, monkeypatch):
    spec = _spec("actionlint")
    tool = _tool_for(spec)
    clean = _target_run(
        _ambient_runs(
            tmp_path / "c",
            spec,
            _Vector("cwd"),
            planted=False,
            monkeypatch=monkeypatch,
            canonical=_none_resolver,
        ),
        tool,
    )
    hostile = _target_run(
        _ambient_runs(
            tmp_path / "h",
            spec,
            _Vector("cwd"),
            planted=True,
            monkeypatch=monkeypatch,
            canonical=_none_resolver,
        ),
        tool,
    )
    assert (clean.ok, hostile.ok) == (False, True)


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_gate_is_not_vacuous_behind_the_fail_open(tmp_path, monkeypatch):
    spec = _spec("prettier")
    oks = _teeth_target_oks(
        tmp_path, spec, _tool_for(spec), monkeypatch, _none_resolver
    )
    assert oks == (True, False)


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_ts_leg_is_ambient_config_blind_and_config_governed(
    tmp_path, monkeypatch
):
    ts_spec = _ToolSpec(
        lang="web",
        tag="prettier-ts",
        target_check=("--check", "--log-level", "warn"),
        binaries=("prettier",),
        fixture=(("app.ts", "const x = {\n  a: { b: 1 }\n}\n"),),
        hostile_name=".prettierrc",
        hostile_body='{"tabWidth": 8}\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=True,
        pluginless_prettier=True,
    )
    tool = _tool_for(ts_spec)
    vec = _Vector("ancestor")

    def _run_ts(sub: str, body: str, *, planted: bool) -> lint.ToolRun:
        cfg = tmp_path / f"{sub}.yaml"
        cfg.write_text(body)
        return _target_run(
            _ambient_runs(
                tmp_path / sub,
                ts_spec,
                vec,
                planted=planted,
                monkeypatch=monkeypatch,
                canonical=_pluginless_resolver(cfg),
            ),
            tool,
        )

    canon = "singleQuote: true\ntabWidth: 2\nsemi: false\ntrailingComma: none\n"
    clean = _run_ts("clean", canon, planted=False)
    assert clean.ok is True, "TS clean baseline must genuinely pass (no fail-open)"
    hostile = _run_ts("hostile", canon, planted=True)
    assert clean.ok == hostile.ok, "ambient ancestor .prettierrc leaked into the TS leg"
    wide = "singleQuote: true\ntabWidth: 8\nsemi: false\ntrailingComma: none\n"
    governed = _run_ts("governed", wide, planted=False)
    assert governed.ok is False, "injected --config does not govern the TS leg"


@pytest.mark.skipif(
    any(shutil.which(b) is None for b in _RUST_BINS),
    reason="rust toolchain not on PATH",
)
def test_gate_has_teeth_removing_cargo_fmt_config_path_reddens(tmp_path, monkeypatch):
    no_path_fmt = lint.Tool(
        "cargo", ("fmt", "--all", "--", "--check"), per_manifest=True
    )
    rust = replace(
        lint.RUST,
        tools=tuple(
            no_path_fmt if t.check[:1] == ("fmt",) else t for t in lint.RUST.tools
        ),
    )
    monkeypatch.setattr(
        lint,
        "LANGS",
        tuple(rust if lang.name == "rust" else lang for lang in lint.LANGS),
    )
    oks = _teeth_target_oks(
        tmp_path, _spec("cargo-fmt"), no_path_fmt, monkeypatch, lint._canonical_config
    )
    assert oks == (True, False)


@pytest.mark.skipif(
    any(shutil.which(b) is None for b in _RUST_BINS),
    reason="rust toolchain not on PATH",
)
def test_cargo_subtree_crate_run_is_ambient_config_blind(tmp_path):
    fixture = (
        ("src-tauri/Cargo.toml", _RUST_CARGO_TOML),
        ("src-tauri/src/lib.rs", _RUST_LIB),
    )
    assert lint.manifest_roots([rel for rel, _ in fixture], ("Cargo.toml",)) == [
        "src-tauri"
    ]

    def _fmt_run(sub: str, *, planted: bool) -> lint.ToolRun:
        repo = tmp_path / sub
        repo.mkdir(parents=True)
        for rel, content in fixture:
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        if planted:
            (repo / "rustfmt.toml").write_text("tab_spaces = 2\n")
        runs: list[lint.ToolRun] = []
        lint.run(
            str(repo),
            discover=_fake_discover([rel for rel, _ in fixture]),
            tracks_root_editorconfig=lambda root: False,
            canonical_config=lint._canonical_config,
            runs_out=runs,
        )
        return _target_run(runs, _tool_for(_spec("cargo-fmt")))

    clean = _fmt_run("clean", planted=False)
    assert clean.ok is True, (
        "the 4-space subdir crate must be clean under the shipped rustfmt.toml — "
        "else the invariance below would be vacuous"
    )
    hostile = _fmt_run("hostile", planted=True)
    assert clean.ok == hostile.ok, (
        "a repo-root rustfmt.toml leaked into the subdir crate's per-manifest fmt run"
    )


@pytest.mark.skipif(
    shutil.which("shellcheck") is None or shutil.which("shfmt") is None,
    reason="shell linters not on PATH",
)
def test_gate_has_teeth_removing_shellcheck_norc_reddens(tmp_path, monkeypatch):
    pre_norc = lint.Tool("shellcheck", ("--severity=info",))
    shell = replace(
        lint.SHELL,
        tools=tuple(
            pre_norc if t.binary == "shellcheck" else t for t in lint.SHELL.tools
        ),
    )
    monkeypatch.setattr(
        lint,
        "LANGS",
        tuple(shell if lang.name == "shell" else lang for lang in lint.LANGS),
    )
    oks = _teeth_target_oks(
        tmp_path, _spec("shellcheck"), pre_norc, monkeypatch, lint._canonical_config
    )
    assert oks == (False, True)


@pytest.mark.skipif(
    shutil.which("shellcheck") is None or shutil.which("shfmt") is None,
    reason="shell linters not on PATH",
)
def test_per_tool_assertion_catches_the_shfmt_leak_a_lang_aggregate_masks(
    tmp_path, monkeypatch
):
    depinned = replace(_tool_for(_spec("shfmt")), editorconfig_pin=())
    shell = replace(
        lint.SHELL,
        tools=tuple(depinned if t.binary == "shfmt" else t for t in lint.SHELL.tools),
    )
    monkeypatch.setattr(
        lint,
        "LANGS",
        tuple(shell if lang.name == "shell" else lang for lang in lint.LANGS),
    )
    spec = replace(
        _spec("shfmt"),
        fixture=(("m.sh", "#!/bin/bash\nif true; then\n\techo $1\nfi\n"),),
    )
    tool = _tool_for(spec)
    clean_runs = _ambient_runs(
        tmp_path / "c",
        spec,
        _Vector("ancestor"),
        planted=False,
        monkeypatch=monkeypatch,
        canonical=lint._canonical_config,
    )
    hostile_runs = _ambient_runs(
        tmp_path / "h",
        spec,
        _Vector("ancestor"),
        planted=True,
        monkeypatch=monkeypatch,
        canonical=lint._canonical_config,
    )
    assert lint.verdict(clean_runs) == 1
    assert lint.verdict(hostile_runs) == 1
    assert _target_run(clean_runs, tool).ok is True
    assert _target_run(hostile_runs, tool).ok is False
