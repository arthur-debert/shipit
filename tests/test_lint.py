"""Unit tests for lint — routing, the registry, and the hard-fail check verb.

The Exec + git boundary is injected (``run`` takes ``discover`` / ``run_tool``,
speaking the runner's ExecResult/ExecError contract), so the orchestration is
exercised with no real linters present.
"""

import shutil
from pathlib import Path

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


def test_lex_projections_need_a_tracked_source():
    """X.md is a projection ONLY when its X.lex sibling is tracked — an .md
    with no source, or a .lex with no projection, changes nothing."""
    assert lint.lex_projections(["a.md", "b.lex"]) == set()
    assert lint.lex_projections(["a.md", "a.lex"]) == {"a.md"}
    assert lint.lex_projections(["docs/dev/x.md", "docs/dev/x.lex", "docs/y.md"]) == {
        "docs/dev/x.md"
    }


def test_route_skips_lex_projections():
    """A tracked X.md with a tracked X.lex sibling is generated output, gated
    at its source by the lexd leg — the markdown leg never lints it. This is
    the consumer-generic rule that replaces per-projection repo-local
    .markdownlintignore entries (ADP00-WS10, #436): the managed ignore file
    stays managed-paths-only while any repo's projections are still skipped."""
    files = [
        "README.lex",
        "README.md",
        "docs/guide.lex",
        "docs/guide.md",
        "docs/manual.md",
    ]
    routed = dict((lang.name, paths) for lang, paths in lint.route(files))
    assert routed["markdown"] == ["docs/manual.md"]
    # The sources still route to the lexd leg — the projection is linted there.
    assert routed["lex"] == ["README.lex", "docs/guide.lex"]


# --------------------------------------------------------------------------
# The consumer-owned lint-ignore seam (#484) — pure filtering
# --------------------------------------------------------------------------


def test_path_ignored_gitignore_style_globs():
    # GENUINE .gitignore semantics (shipit's .treeinclude engine), not the old
    # anchored full-path glob: ** matches any run of segments, * never crosses /.
    assert lint.path_ignored("tests/fixtures/a.md", ["tests/fixtures/**"])
    assert lint.path_ignored("tests/fixtures/sub/b.md", ["tests/fixtures/**"])
    assert lint.path_ignored("tests/a.md", ["tests/*.md"])
    assert not lint.path_ignored("tests/a/b.md", ["tests/*.md"])  # * stops at /
    assert not lint.path_ignored("src/x.py", ["tests/fixtures/**"])


def test_path_ignored_directory_prefix_drops_whole_subtree():
    # The lex case the old full_match could NOT express: a trailing-slash
    # DIRECTORY pattern matches everything under it (a built CHANGELOG/ tree).
    assert lint.path_ignored("CHANGELOG/0.15.0.md", ["CHANGELOG/"])
    assert lint.path_ignored("CHANGELOG/nested/x.md", ["CHANGELOG/"])
    assert not lint.path_ignored(
        "CHANGELOG.md", ["CHANGELOG/"]
    )  # the dir, not the file


def test_path_ignored_floating_vs_anchored():
    # An unanchored name floats to any depth (real gitignore); a leading / anchors
    # it to the repo root.
    assert lint.path_ignored("CHANGELOG.md", ["CHANGELOG.md"])
    assert lint.path_ignored("docs/CHANGELOG.md", ["CHANGELOG.md"])
    assert lint.path_ignored("CHANGELOG.md", ["/CHANGELOG.md"])
    assert not lint.path_ignored("docs/CHANGELOG.md", ["/CHANGELOG.md"])


def test_path_ignored_empty_patterns_never_match():
    assert not lint.path_ignored("anything.md", [])


def test_path_ignored_bad_glob_is_a_no_match_not_a_crash():
    # A malformed pattern narrows nothing rather than crashing the gate OR
    # disabling a valid sibling entry.
    assert not lint.path_ignored("a.md", ["["])
    assert not lint.path_ignored("a.md", ["[z-a]"])  # invalid regex char range
    assert lint.path_ignored("keep.md", ["[z-a]", "keep.md"])


def test_drop_ignored_removes_matches_order_preserved():
    files = ["src/x.py", "tests/fixtures/a.md", "README.md", "tests/fixtures/b.txt"]
    assert lint.drop_ignored(files, ["tests/fixtures/**"]) == ["src/x.py", "README.md"]


def test_drop_ignored_no_patterns_is_identity():
    files = ["a.py", "b.md"]
    assert lint.drop_ignored(files, []) is files


# --------------------------------------------------------------------------
# The built-in --fix mutation guard for test-data dirs (#500)
# --------------------------------------------------------------------------


def test_drop_protected_testdata_drops_every_convention_at_any_depth():
    # Each convention floats to any depth and drops its whole subtree, so a
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
        "docs/fixtures-guide.md",  # a file NAMED like the dir is not a dir match
        "tests/testdata/b.json",
    ]
    # Real source is kept, in order; only files UNDER a protected dir are dropped.
    assert lint.drop_protected_testdata(files) == [
        "src/x.py",
        "README.md",
        "docs/fixtures-guide.md",
    ]


def test_drop_protected_testdata_empty_is_identity():
    assert lint.drop_protected_testdata([]) == []


def test_protected_testdata_is_the_exact_complement_of_drop():
    # `protected_testdata` returns what `drop_protected_testdata` removes — the
    # two partition the input, order preserved. It is the snapshot set for the
    # per-manifest cargo-fmt guard (#502).
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
    # complement, no overlap
    kept = set(lint.drop_protected_testdata(files))
    dropped = set(lint.protected_testdata(files))
    assert kept.isdisjoint(dropped)
    assert kept | dropped == set(files)


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
# Editorconfig hermeticity pin (#493) — pure signal + argv gating
# --------------------------------------------------------------------------


def test_tracks_editorconfig_root_only():
    # Only a ROOT .editorconfig owns the tree's config (the pin runs once at the
    # root). A repo that commits a root .editorconfig is honored (pin OFF).
    assert lint.tracks_editorconfig([".editorconfig", "a.sh"])
    # A NESTED tracked .editorconfig does NOT disable the pin: honoring it would
    # need per-scope batch-splitting (deliberately not done), and keying on it
    # would open a hermeticity hole for files outside its scope (#493, codex).
    assert not lint.tracks_editorconfig(["sub/dir/.editorconfig", "sub/dir/a.sh"])
    # A repo that tracks none is the pinned shape (phos-core).
    assert not lint.tracks_editorconfig(["a.sh", "b.json", "README.md"])
    # A file merely NAMED like it, but not exactly .editorconfig, does not count.
    assert not lint.tracks_editorconfig(["my.editorconfig.bak", "x.editorconfig.md"])


def test_tracks_root_editorconfig_reads_repo_root_not_target(monkeypatch):
    # The pin decision is a repo-wide git fact: it resolves the repo TOP-LEVEL and
    # reads its tracked list, so a subdirectory-scoped run (`shipit lint src/`)
    # still sees a root-tracked .editorconfig even though ls-files under the target
    # would not (#493, agy round 1).
    seen: dict[str, str] = {}
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: "/repo")

    def fake_ls(*, cwd):
        seen["cwd"] = cwd
        return [".editorconfig", "src/app.py"]

    monkeypatch.setattr(lint.git, "ls_files", fake_ls)
    assert lint._tracks_root_editorconfig(Path("/repo/src")) is True
    # Queried at the TOP-LEVEL, not the `src` target.
    assert seen["cwd"] == "/repo"


def test_tracks_root_editorconfig_nested_only_is_pinned(monkeypatch):
    # A repo tracking ONLY a nested .editorconfig is NOT tracked at the root → the
    # pin stays ON, closing codex's hermeticity hole (#493, round 1).
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: "/repo")
    monkeypatch.setattr(
        lint.git, "ls_files", lambda *, cwd: ["sub/.editorconfig", "sub/a.sh"]
    )
    assert lint._tracks_root_editorconfig(Path("/repo/sub")) is False


def test_tracks_root_editorconfig_outside_checkout_is_pinned(monkeypatch):
    # Outside any checkout → no tracked config → pinned (honor-tracked default);
    # the tracked-list query is never even reached.
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: None)

    def must_not_query(*, cwd):
        raise AssertionError("ls_files must not run without a repo root")

    monkeypatch.setattr(lint.git, "ls_files", must_not_query)
    assert lint._tracks_root_editorconfig(Path("/tmp/not-a-repo")) is False


def test_shfmt_pin_gated_on_tracked_editorconfig():
    shfmt = lint.SHELL.tools[1]
    assert shfmt.binary == "shfmt"
    # Pinned (no tracked .editorconfig): `-i 0` is prepended so shfmt ignores any
    # ambient .editorconfig and defaults to tabs.
    assert shfmt.argv(fix=False, pin_editorconfig=True) == ("-i", "0", "-d")
    assert shfmt.argv(fix=True, pin_editorconfig=True) == ("-i", "0", "-w")
    # Unpinned (repo tracks its own): shfmt reads the tracked config, no pin.
    assert shfmt.argv(fix=False, pin_editorconfig=False) == ("-d",)
    assert shfmt.argv(fix=True, pin_editorconfig=False) == ("-w",)


def test_prettier_pin_gated_on_tracked_editorconfig():
    prettier = lint.JSON.tools[0]
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
    # A tool with no editorconfig_pin is unaffected by pin_editorconfig — the
    # gate only ever prepends flags to shfmt/prettier.
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.editorconfig_pin == ()
    assert ruff_check.argv(fix=False, pin_editorconfig=True) == ("check",)


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


def test_fix_mode_never_rewrites_a_protected_fixture(tmp_path, capsys):
    # #500: `--fix` must not hand a deliberately-malformed / byte-exact fixture
    # to an in-place fixer. The fixture under fixtures/ is dropped from the
    # markdownlint --fix batch; the ordinary README.md is still fixed.
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["README.md", "tests/fixtures/broken.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert ("markdownlint", ("--fix", "README.md")) in rec.calls
    # The fixture is NOT in any invocation's argv — never handed to the fixer.
    assert not any("tests/fixtures/broken.md" in args for _, args in rec.calls)


def test_check_mode_still_passes_protected_fixtures_to_the_checkers(tmp_path, capsys):
    # The guard is MUTATION-only: in check mode the verb still hands the fixture
    # to the tool's argv, so the CI gate reports a genuinely-broken fixture.
    # (For MARKDOWN specifically, markdownlint then skips it via the managed
    # `.markdownlintignore` — a separate mechanism in the consumer's tree that
    # this verb does not model; here we assert only the verb-level behavior:
    # check mode does not drop the path.)
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["tests/fixtures/broken.md"]),
        run_tool=rec,
    )
    assert rc == 0
    assert ("markdownlint", ("tests/fixtures/broken.md",)) in rec.calls


def test_fix_mode_check_form_tool_still_sees_a_protected_fixture(tmp_path, capsys):
    # The guard drops fixtures only from a MUTATING batch. shellcheck has no fix
    # form, so even during --fix it runs its check form over the fixture; shfmt
    # (the fixer) must NOT receive it.
    rec = _Recorder()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["src/ok.sh", "tests/fixtures/bad.sh"]),
        run_tool=rec,
    )
    assert rc == 0
    # shellcheck (check-only) covers both files, fixture included.
    assert (
        "shellcheck",
        ("--severity=info", "src/ok.sh", "tests/fixtures/bad.sh"),
    ) in rec.calls
    # shfmt -w (the mutating fixer) ran over the real source but NEVER the
    # fixture (its argv carries an editorconfig pin, so match on membership).
    shfmt_calls = [args for binary, args in rec.calls if binary == "shfmt"]
    assert shfmt_calls, "shfmt should have run its fix form"
    assert all("src/ok.sh" in args for args in shfmt_calls)
    assert all("tests/fixtures/bad.sh" not in args for args in shfmt_calls)


def test_fix_mode_skips_fixer_when_every_file_is_a_protected_fixture(tmp_path, capsys):
    # When a fixer's whole batch is protected test-data, the fix run is skipped
    # rather than invoking the fixer with an empty batch (which some fixers treat
    # as an error). markdownlint is not invoked at all.
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


class _FakeCargoFmt:
    """A run_tool that simulates `cargo fmt --all` rewriting EVERY tracked .rs
    under its cwd (as real rustfmt would, following mod decls) — the fixture the
    #502 snapshot/restore guard must protect. clippy / fmt --check don't mutate."""

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
    # #502: `cargo fmt --all` takes no file batch and formats a whole crate, so a
    # protected `.rs` reachable via a `mod` decl CANNOT be kept off its argv (and
    # rustfmt's own `ignore` is nightly-only). The verb snapshots protected `.rs`
    # and restores any the formatter rewrote: the fixture is byte-identical after
    # --fix, while real crate source stays reformatted.
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    real = tmp_path / "src" / "lib.rs"
    fixture = tmp_path / "tests" / "fixtures" / "bad.rs"
    real.write_text("pub fn a()->i32{1}\n")
    original = "pub fn v()->i32{      1    }\n"  # deliberately malformed fixture
    fixture.write_text(original)

    fake = _FakeCargoFmt()
    rc = lint.run(
        str(tmp_path),
        fix=True,
        discover=_fake_discover(["Cargo.toml", "src/lib.rs", "tests/fixtures/bad.rs"]),
        run_tool=fake,
    )
    assert rc == 0
    # The protected fixture is restored byte-for-byte …
    assert fixture.read_bytes() == original.encode()
    # … while the real crate source is left reformatted.
    assert real.read_text() == "// reformatted by the fixer\n"


def test_fix_mode_restores_a_fixture_that_is_itself_a_crate(tmp_path):
    # #502 (agy): a fixture that IS a tracked crate (its own Cargo.toml under a
    # protected dir) becomes its own manifest root, so `cargo fmt` runs INSIDE
    # it. Its `.rs` is still protected — snapshotted and restored.
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
    # The guard is mutation-only: check mode runs `cargo fmt --all -- --check`
    # (non-mutating) and clippy, so a fixture .rs is neither rewritten nor
    # restored — it is simply left as-is and reported on by the check.
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    fixture = tmp_path / "tests" / "fixtures" / "bad.rs"
    original = "pub fn v()->i32{      1    }\n"
    fixture.write_text(original)

    fake = _FakeCargoFmt()  # only rewrites the MUTATING fmt form, not --check
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "tests/fixtures/bad.rs"]),
        run_tool=fake,
    )
    assert rc == 0
    assert fixture.read_bytes() == original.encode()


def test_shell_routed_by_shebang_runs_shellcheck(tmp_path, capsys):
    # An extensionless tracked file with a bash shebang routes to shell.
    script = tmp_path / "tool"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    rec = _Recorder()
    rc = lint.run(str(tmp_path), discover=_fake_discover(["tool"]), run_tool=rec)
    assert rc == 0
    assert any(binary == "shellcheck" for binary, _ in rec.calls)
    assert any(binary == "shfmt" for binary, _ in rec.calls)


# --------------------------------------------------------------------------
# The consumer-owned lint-ignore seam (#484) — end-to-end through run()
# --------------------------------------------------------------------------


def _fail_when_file_present(dirty_binary, dirty_file):
    """A run_tool that fails ``dirty_binary`` iff ``dirty_file`` reaches its argv,
    and records every call — the deterministic stand-in for a lint-dirty file."""
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
    # A deliberately-lint-dirty fixture the consumer OWNS is listed in
    # `.shipit.toml [lint].ignore`, so it never reaches markdownlint and the gate
    # is GREEN — the sanctioned reconcile-safe seam (#484), no managed-file edit.
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = ["tests/fixtures/**"]\n')
    run_tool = _fail_when_file_present("markdownlint", "tests/fixtures/ref.md")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["README.md", "tests/fixtures/ref.md"]),
        run_tool=run_tool,
    )
    assert rc == 0
    assert "LINT: OK" in capsys.readouterr().out
    # markdownlint ran on the un-ignored file only; the fixture never reached it.
    md_batches = [args for binary, args in run_tool.calls if binary == "markdownlint"]
    assert md_batches == [("README.md",)]


def test_same_fixture_un_ignored_gate_red(tmp_path, capsys):
    # The control: WITHOUT the ignore entry, the same dirty fixture reaches
    # markdownlint and reddens the gate — proving the seam is what turned it green,
    # and that the gate is NOT weakened for non-ignored paths.
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
    # One glob drops the path from EVERY leg, not just markdownlint (the
    # release-core-managed-script / generated-file cases in the #484 thread): an
    # ignored .py never reaches ruff either.
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
    # A repo without .shipit.toml lints everything — the seam defaults to empty,
    # never accidentally suppressing a leg.
    run_tool = _fail_when_file_present("markdownlint", "tests/fixtures/ref.md")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["tests/fixtures/ref.md"]),
        run_tool=run_tool,
    )
    assert rc == 1


def test_lint_ignore_directory_prefix_drops_generated_subtree(tmp_path, capsys):
    # The motivating lex case the old full_match could NOT express (#484): a
    # trailing-slash DIRECTORY pattern excludes a whole built `CHANGELOG/` tree of
    # generated .md, so a dirty generated file never reddens the gate — and it
    # takes real gitignore semantics to match `CHANGELOG/` against `CHANGELOG/x.md`.
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
    assert md_batches == [("README.md",)]


# --------------------------------------------------------------------------
# Editorconfig hermeticity pin (#493) — end-to-end through run()
# --------------------------------------------------------------------------


def test_run_pins_shfmt_and_prettier_when_no_editorconfig_tracked(tmp_path):
    # A repo tracking NO root .editorconfig: shfmt/prettier are pinned to ignore any
    # ambient .editorconfig, so the argv carries the pin flags ahead of the files.
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
        ("--no-editorconfig", "--check", "--log-level", "warn", "data.json"),
    ) in rec.calls


def test_run_does_not_pin_when_repo_tracks_editorconfig(tmp_path):
    # A repo that commits its own root .editorconfig owns its formatting config: the
    # pin is OFF so shfmt/prettier honor the tracked file (shipit's own shape).
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
        ("--check", "--log-level", "warn", "data.json"),
    ) in rec.calls


def test_run_pin_decision_independent_of_lint_ignore(tmp_path, monkeypatch):
    # The pin is a git-tracking fact, NOT a routing decision: a `[lint].ignore`
    # entry that would drop `.editorconfig` from the routed files must NOT flip
    # hermeticity (#493, copilot / agy round 1). Exercises the REAL
    # `_tracks_root_editorconfig` over a monkeypatched git seam: the repo tracks a
    # root .editorconfig AND `.shipit.toml` ignores it — the pin still reads OFF.
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = [".editorconfig"]\n')
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
    # Pin OFF despite the ignore: shfmt/prettier honor the tracked root config.
    assert ("shfmt", ("-d", "run.sh")) in rec.calls
    assert (
        "prettier",
        ("--check", "--log-level", "warn", "data.json"),
    ) in rec.calls


@pytest.mark.skipif(
    shutil.which("shfmt") is None or shutil.which("shellcheck") is None,
    reason="shell linters (shfmt/shellcheck) not on PATH in this env",
)
def test_shfmt_verdict_is_hermetic_across_ambient_editorconfig(tmp_path):
    """The guarantee (#493): a tab-indented shell script yields the SAME lint
    verdict whether or not an ambient space/2 `.editorconfig` sits in the tree.

    Runs the REAL shfmt (via lint.run's default _run_tool) over a repo that
    tracks no `.editorconfig`, once clean and once with an injected untracked
    `.editorconfig` — the exact phos-core shape (#472). Without the pin the second
    run reddens (shfmt wants to reflow tabs → 2-space); with it, both pass.
    """
    (tmp_path / "script.sh").write_text("#!/bin/bash\nif true; then\n\techo hi\nfi\n")
    discover = _fake_discover(["script.sh"])
    # The repo tracks no root .editorconfig → pinned (deterministic, not derived
    # from tmp_path's git state).
    pinned = {"tracks_root_editorconfig": lambda root: False}

    # Clean tree: tab-indented script is shfmt-clean (tabs are shfmt's default).
    assert lint.run(str(tmp_path), discover=discover, **pinned) == 0

    # Inject the untracked ambient `.editorconfig` (NOT in the tracked file list)
    # that co-resident tooling would symlink in — space/2, root=true.
    (tmp_path / ".editorconfig").write_text(
        "root = true\n[*]\nindent_style = space\nindent_size = 2\n"
    )
    # Identical verdict: the pin makes shfmt ignore the injected config.
    assert lint.run(str(tmp_path), discover=discover, **pinned) == 0


def test_malformed_shipit_toml_fails_clean_not_traceback(tmp_path, capsys):
    # A malformed `[lint].ignore` surfaces as the CLI's uniform `error: …` line +
    # exit 1 (the cli_errors shell, ADR-0030), NOT a raw ConfigError traceback
    # escaping mid-gate — the same clean failure every config-reading verb gives.
    (tmp_path / ".shipit.toml").write_text("[lint]\nignore = 42\n")
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["README.md"]),
        run_tool=_Recorder(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "list of glob strings" in err
