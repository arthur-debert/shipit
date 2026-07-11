"""Unit tests for lint — routing, the registry, and the hard-fail check verb.

Two layers:

* ORCHESTRATION — routing, the registry, and the check verb. The Exec + git
  boundary is injected (``run`` takes ``discover`` / ``run_tool``, speaking the
  runner's ExecResult/ExecError contract), so these run with NO real linters
  present — the tool commands are stubbed.

* HERMETICITY GATE (ADR-0037, LNT01-WS02 #515) — the invariance property tests
  below run the REAL registered binaries (default ``run_tool``, real subprocess)
  to prove each tool's verdict is ambient-config-blind. These need the tools ON
  PATH: a case ``skipif``s an absent optional toolchain (rust, lex), and
  ``test_core_lint_tools_present_...`` FLOORS the core set, failing loudly if a
  core linter is missing (e.g. when run outside shipit's pixi env) rather than
  skipping into a false green.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from shipit import execrun, lint
from shipit.verbs import lint as lint_verb

# The packaged canonical-config paths the gate injects by DEFAULT (WS03 #516),
# so argv assertions can name what `_canonical_config` resolves without hardcoding
# a machine-specific path. A tool with no shipped file-config (shellcheck, shfmt,
# cargo, lexd) injects nothing.
_RUFF_CFG = lint.data_path("ruff.toml")
_PRETTIER_CFG = lint.data_path("prettierrc.yaml")
_MD_CFG = lint.data_path("markdownlint.yaml")
_YAML_CFG = lint.data_path("yamllint.yaml")
_ACTIONLINT_CFG = lint.data_path("actionlint.yaml")

# --------------------------------------------------------------------------
# Pure routing
# --------------------------------------------------------------------------


def test_lang_for_routes_by_extension():
    assert lint.lang_for("src/x.py").name == "python"
    assert lint.lang_for("a/b.yml").name == "yaml"
    assert lint.lang_for("a/b.yaml").name == "yaml"
    # prettier's web-format family (LNT01-WS07 #520): JSON plus the TS/Svelte
    # legs all route to the single `web` lang / prettier tool.
    assert lint.lang_for("data.json").name == "web"
    assert lint.lang_for("src/app.ts").name == "web"
    assert lint.lang_for("src/App.tsx").name == "web"
    assert lint.lang_for("src/Widget.svelte").name == "web"
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
# The path-claiming route (TOL01-WS04 #553): .github/workflows/ → actions,
# ADDITIVE to the yaml extension route.
# --------------------------------------------------------------------------


def test_lang_for_never_returns_a_path_claiming_lang():
    # The EXTENSION route is untouched by the actions Lang: `.yml` anywhere —
    # including inside the claimed directory — resolves to the yaml Lang; the
    # actions claim is additive (route), never an extension hand-off.
    assert lint.lang_for(".github/workflows/ci.yml").name == "yaml"
    assert lint.lang_for("a/b.yml").name == "yaml"


def test_path_claimed_langs_claims_workflow_yaml():
    claims = lint.path_claimed_langs(".github/workflows/ci.yml")
    assert [lang.name for lang in claims] == ["actions"]
    claims = lint.path_claimed_langs(".github/workflows/release.yaml")
    assert [lang.name for lang in claims] == ["actions"]


def test_path_claimed_langs_scopes_by_prefix_and_extension():
    # The claim covers only workflow YAML directly IN the prefix directory: a
    # non-YAML stray in the directory, a sibling `.github` file, a repo-wide
    # `.yml`, and a lookalike directory all claim nothing.
    assert lint.path_claimed_langs(".github/workflows/README.md") == []
    assert lint.path_claimed_langs(".github/dependabot.yml") == []
    assert lint.path_claimed_langs("config.yml") == []
    assert lint.path_claimed_langs(".github/workflows-old/ci.yml") == []


def test_path_claimed_langs_is_non_recursive():
    # GitHub reads workflows only from the IMMEDIATE `.github/workflows/`
    # directory; an archived/generated file in a nested subdirectory is one
    # GitHub never runs, so actionlint must not claim it (#553).
    assert lint.path_claimed_langs(".github/workflows/archive/ci.yml") == []
    assert lint.path_claimed_langs(".github/workflows/old/nested/ci.yaml") == []


def test_route_workflow_files_bucket_into_yaml_and_actions():
    """The one legitimate dual route: a workflow file keeps its yamllint
    coverage AND gains actionlint's — additive, never a hand-off (#553)."""
    files = [".github/workflows/ci.yml", "config.yml", "a.py"]
    routed = dict((lang.name, paths) for lang, paths in lint.route(files))
    assert routed["yaml"] == [".github/workflows/ci.yml", "config.yml"]
    assert routed["actions"] == [".github/workflows/ci.yml"]


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
    # rustfmt is the one rust --fix leg: cargo fmt in place. Both legs carry the
    # canonical rustfmt.toml inline via `--config-path` (WS03 #516) — it rides the
    # tuple, not `config_inject`, because it must follow cargo's `--` separator.
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


def test_every_lang_has_at_least_one_tool():
    for lang in lint.LANGS:
        assert lang.tools, f"{lang.name} has no tools"


# --------------------------------------------------------------------------
# Editorconfig hermeticity pin (#493) — pure signal + argv gating
# --------------------------------------------------------------------------


def test_tracks_editorconfig_root_only():
    # `tracks_editorconfig` now takes a lazy reader for the root .editorconfig body,
    # consulted ONLY when the exact path is tracked (issue #528).
    def root_true():
        return "root = true\n"

    def not_root():
        return "[*]\nindent_style = space\n"

    def root_false():
        return "root = false\n"

    def must_not_read():
        raise AssertionError("reader must not run when .editorconfig is untracked")

    # Only a ROOT .editorconfig owns the tree's config (the pin runs once at the
    # root), and ONLY when it declares `root = true` — a repo that commits such a
    # root .editorconfig is honored (pin OFF).
    assert lint.tracks_editorconfig([".editorconfig", "a.sh"], root_true)
    # #528: a tracked root .editorconfig WITHOUT `root = true` keeps the pin ON —
    # presence is necessary but not sufficient (the tool would still walk up).
    assert not lint.tracks_editorconfig([".editorconfig", "a.sh"], not_root)
    # `root = false` is explicit non-root → pin STAYS ON.
    assert not lint.tracks_editorconfig([".editorconfig", "a.sh"], root_false)
    # A NESTED tracked .editorconfig does NOT disable the pin: honoring it would
    # need per-scope batch-splitting (deliberately not done), and keying on it
    # would open a hermeticity hole for files outside its scope (#493, codex). The
    # reader is never consulted (proven by the raising stub).
    assert not lint.tracks_editorconfig(
        ["sub/dir/.editorconfig", "sub/dir/a.sh"], must_not_read
    )
    # A repo that tracks none is the pinned shape (phos-core) — reader untouched.
    assert not lint.tracks_editorconfig(["a.sh", "b.json", "README.md"], must_not_read)
    # A file merely NAMED like it, but not exactly .editorconfig, does not count.
    assert not lint.tracks_editorconfig(
        ["my.editorconfig.bak", "x.editorconfig.md"], must_not_read
    )


def test_editorconfig_declares_root():
    # Pure preamble parse (#528): only `root = true` (case-insensitive) in the
    # PREAMBLE — before the first [section] — counts.
    assert lint.editorconfig_declares_root("root = true\n")
    assert lint.editorconfig_declares_root("Root = True\n")
    assert lint.editorconfig_declares_root("# comment\n; also\n\nroot=true\n[*]\n")
    # `root` set to anything but true does not count.
    assert not lint.editorconfig_declares_root("root = false\n")
    assert not lint.editorconfig_declares_root("")
    assert not lint.editorconfig_declares_root("[*]\nindent_style = space\n")
    # `root` INSIDE a section (after the first header) is not the preamble root.
    assert not lint.editorconfig_declares_root("[*]\nroot = true\n")
    # LAST-WINS on a duplicated preamble `root` (editorconfig semantics; the safe
    # over-pin direction): the FINAL assignment governs, not the first.
    assert not lint.editorconfig_declares_root("root = true\nroot = false\n")
    assert lint.editorconfig_declares_root("root = false\nroot = true\n")


def test_read_editorconfig_strips_utf8_bom(tmp_path):
    # #528: a UTF-8 BOM-prefixed `.editorconfig` must still parse as `root = true`.
    # The strip happens in `_read_editorconfig` (utf-8-sig), so exercise the read
    # seam — a literal BOM fed to the pure parser would (correctly) stay non-root.
    cfg = tmp_path / ".editorconfig"
    cfg.write_bytes(b"\xef\xbb\xbfroot = true\n")
    body = lint._read_editorconfig(cfg)
    assert body is not None
    assert lint.editorconfig_declares_root(body)


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
    # The real code now also reads /repo/.editorconfig's body to confirm
    # `root = true` (#528); /repo doesn't exist on disk, so stub the reader.
    monkeypatch.setattr(lint, "_read_editorconfig", lambda path: "root = true\n")
    assert lint._tracks_root_editorconfig(Path("/repo/src")) is True
    # Queried at the TOP-LEVEL, not the `src` target.
    assert seen["cwd"] == "/repo"


def test_tracks_root_editorconfig_requires_root_true_content(monkeypatch):
    # #528: presence of a tracked root .editorconfig is necessary but NOT
    # sufficient — the pin disables ONLY when the file declares `root = true`. A
    # linter-free unit test over the git seam + reader, so the core guarantee is
    # validated even where shfmt is absent.
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: "/repo")
    monkeypatch.setattr(lint.git, "ls_files", lambda *, cwd: [".editorconfig", "a.sh"])
    # A tracked non-`root = true` root config → NOT tracked-for-pin → pin STAYS ON.
    monkeypatch.setattr(
        lint, "_read_editorconfig", lambda path: "[*]\nindent_style = space\n"
    )
    assert lint._tracks_root_editorconfig(Path("/repo")) is False
    # The same tracked file declaring `root = true` → tracked-for-pin → pin OFF.
    monkeypatch.setattr(lint, "_read_editorconfig", lambda path: "root = true\n")
    assert lint._tracks_root_editorconfig(Path("/repo")) is True


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
    # A tool with no editorconfig_pin is unaffected by pin_editorconfig — the
    # gate only ever prepends flags to shfmt/prettier.
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.editorconfig_pin == ()
    assert ruff_check.argv(fix=False, pin_editorconfig=True) == ("check",)


# --------------------------------------------------------------------------
# The service — boundary injected
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
    # ruff (check + format), markdownlint all ran, files appended — each with its
    # canonical config injected by default (WS03 #516).
    assert ("ruff", ("--config", _RUFF_CFG, "check", "a.py")) in rec.calls
    assert ("ruff", ("--config", _RUFF_CFG, "format", "--check", "a.py")) in rec.calls
    assert ("markdownlint", ("--config", _MD_CFG, "b.md")) in rec.calls


def test_workflow_file_runs_yamllint_and_actionlint(tmp_path):
    """A workflow file's dual route reaches BOTH tools (TOL01-WS04 #553), each
    under its injected canonical config: yamllint keeps its coverage, actionlint
    rides `-config-file <shipped actionlint.yaml>` with the files appended."""
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
    # ruff runs its fix forms (canonical config injected, WS03 #516).
    assert ("ruff", ("--config", _RUFF_CFG, "check", "--fix", "a.py")) in rec.calls
    assert ("ruff", ("--config", _RUFF_CFG, "format", "a.py")) in rec.calls
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
    assert ("markdownlint", ("--config", _MD_CFG, "--fix", "README.md")) in rec.calls
    # The fixture is NOT in any invocation's argv — never handed to the fixer.
    assert not any("tests/fixtures/broken.md" in args for _, args in rec.calls)


def test_fix_mode_reports_the_post_drop_count_in_note_and_log(tmp_path, capsys, caplog):
    # Round-2 review (copilot): both the printed `(N files)` note and the
    # `files:` debug-log field must reflect the POST-drop batch actually handed
    # to the fixer, not the pre-drop routed count — else troubleshooting sees a
    # count inconsistent with the argv. Two markdown files route, one is a
    # fixture that gets dropped, so the fixer runs over exactly ONE file.
    rec = _Recorder()
    with caplog.at_level("DEBUG", logger="shipit.lint"):
        rc = lint.run(
            str(tmp_path),
            fix=True,
            discover=_fake_discover(["README.md", "tests/fixtures/broken.md"]),
            run_tool=rec,
        )
    assert rc == 0
    # Printed note: post-drop count (singular). The label carries the injected
    # `--config <canonical>` too (WS03 #516), so assert on the fix-form + count tail.
    assert "markdownlint --config" in (out := capsys.readouterr().out)
    assert "--fix (1 file)" in out
    # Debug log: the `files` field matches the argv (1), not the routed 2.
    finished = [
        r
        for r in caplog.records
        if r.getMessage() == "lint tool finished"
        and getattr(r, "tool", None) == "markdownlint"
    ]
    assert finished, "expected a 'lint tool finished' record for markdownlint"
    assert all(r.files == 1 for r in finished)


def test_check_mode_still_passes_protected_fixtures_to_the_checkers(tmp_path, capsys):
    # The guard is MUTATION-only: in check mode the service still hands the fixture
    # to the tool's argv, so the CI gate reports a genuinely-broken fixture.
    # (For MARKDOWN specifically, markdownlint then skips it via the managed
    # `.markdownlintignore` — a separate mechanism in the consumer's tree that
    # this service does not model; here we assert only the service-level behavior:
    # check mode does not drop the path.)
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
    # shellcheck (check-only) covers both files, fixture included. Its argv carries
    # the unconditional `--norc` hermeticity flag ahead of the canonical severity
    # floor (ADR-0037 / #515 — closes the ancestor `.shellcheckrc` walk).
    assert (
        "shellcheck",
        ("--norc", "--severity=info", "src/ok.sh", "tests/fixtures/bad.sh"),
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
    # clippy carries its inline lint floor; fmt carries the injected rustfmt.toml
    # via `--config-path` (WS03 #516). Read both from the registry so the exact
    # tuples (incl. the machine-specific config path) stay in one place.
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


# --------------------------------------------------------------------------
# Rust toolchain-skew guard (#602)
# --------------------------------------------------------------------------


def test_parse_cargo_version():
    assert lint.parse_cargo_version("cargo 1.96.0 (d1b87f7c9 2026-01-05)") == "1.96.0"
    # A nightly banner still yields the numeric core the pin compares against.
    assert (
        lint.parse_cargo_version("cargo 1.98.0-nightly (abc123 2026-06-01)") == "1.98.0"
    )
    # A preamble line (e.g. a rustup warning) before the banner is skipped.
    assert lint.parse_cargo_version("warning: x\ncargo 1.90.2\n") == "1.90.2"
    assert lint.parse_cargo_version("rustc 1.96.0") is None
    assert lint.parse_cargo_version("") is None


def test_rust_pin_satisfied_common_shapes():
    assert lint.rust_pin_satisfied("1.96.0", "1.96.*")
    assert lint.rust_pin_satisfied("1.96.3", "1.96.*")
    assert not lint.rust_pin_satisfied("1.97.0", "1.96.*")
    # The prefix match is dot-bounded: 1.9.* must not swallow 1.96.0.
    assert not lint.rust_pin_satisfied("1.96.0", "1.9.*")
    assert lint.rust_pin_satisfied("1.96.0", "==1.96.0")
    assert not lint.rust_pin_satisfied("1.96.1", "==1.96.0")
    # A bare/fuzzy conda spec is a prefix match.
    assert lint.rust_pin_satisfied("1.96.0", "1.96")
    assert lint.rust_pin_satisfied("1.96.0", "=1.96")
    assert lint.rust_pin_satisfied("2.0.0", "*")


def test_rust_pin_satisfied_unmodelled_shapes_never_claim_skew():
    # Range/compound specs are not modelled; ambiguity resolves to SATISFIED so
    # the gate stays hard — a wrong skew claim would downgrade a real failure.
    assert lint.rust_pin_satisfied("1.80.0", ">=1.90")
    assert lint.rust_pin_satisfied("1.80.0", ">=1.90,<2")
    assert lint.rust_pin_satisfied("1.80.0", "~=1.96")
    # Non-conda operators a stray manifest could carry — a cargo-style caret and
    # a path/URL spec — are unmodelled too, so they never trip a false skew.
    assert lint.rust_pin_satisfied("1.80.0", "^1.96")
    assert lint.rust_pin_satisfied("1.80.0", "@ file:///opt/rust")
    # A non-numeric / unparseable pin (a bare word, an empty base, a partial
    # version tail) carries no sentinel operator but is still unmodelled: it must
    # resolve to SATISFIED, never fall through to prefix-match and claim skew.
    assert lint.rust_pin_satisfied("1.96.0", "nightly")
    assert lint.rust_pin_satisfied("1.96.0", "stable")
    assert lint.rust_pin_satisfied("1.96.0", "1.96.0-nightly")
    assert lint.rust_pin_satisfied("1.96.0", "=")


def test_rust_pin_from_manifest_lint_feature_wins():
    # The managed rust lint block ([feature.lint.dependencies], #547) is the
    # canonical pin — it names the toolchain of the very env the hooks run.
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
    assert lint.detect_rust_skew("1.97.*", probe) is None  # pin satisfied
    assert lint.detect_rust_skew(None, probe) is None  # no pin, no claim
    assert lint.detect_rust_skew("1.96.*", None) is None  # no probe, no claim
    assert lint.detect_rust_skew("1.96.*", "garbled") is None  # unparseable probe


class _SkewRecorder(_Recorder):
    """A _Recorder whose cargo also answers ``--version`` with a scripted banner
    (rc 0), so the #602 skew probe sees a resolvable toolchain while the real
    cargo legs keep their scripted verdicts."""

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
    # The #602 hazard: the resolved cargo (1.97.0) escapes the repo's pin
    # (1.96.*), so a clippy/fmt failure is the toolchain's verdict, not the
    # canonical one — warn-not-block, loudly, instead of training --no-verify.
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
    # clippy and fmt both fail under one skew, but the per-dir probe is cached:
    # exactly one `cargo --version` for the single manifest dir.
    probes = [a for b, a in rec.calls if b == "cargo" and a == ("--version",)]
    assert len(probes) == 1


def test_rust_skew_probes_cargo_in_the_manifest_dir_not_root(tmp_path, capsys):
    # A nested manifest (src-tauri/) can carry a directory-scoped rustup
    # override, so the #602 probe must run `cargo --version` in the SAME dir as
    # the failing leg — probing from root could read a different toolchain and
    # claim skew off the wrong cargo. The probe cwd must track the manifest dir.
    _write_rust_pin(tmp_path)
    rec = _SkewRecorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["src-tauri/Cargo.toml", "src-tauri/src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 0  # skew claimed → warn-not-block
    assert "TOOLCHAIN SKEW" in capsys.readouterr().out
    # The --version probe ran in src-tauri/, the failing leg's own dir.
    probe_cwds = [
        cwd for b, args, cwd in rec.cwds if b == "cargo" and args == ("--version",)
    ]
    assert probe_cwds == [tmp_path / "src-tauri"]


class _NoisySkewRecorder(_SkewRecorder):
    """A _SkewRecorder whose failing cargo legs (clippy/fmt) also emit real
    output on stdout, so a test can assert the tool's own diagnostics ride the
    #602 skew note instead of being swallowed by the warn-not-block downgrade."""

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
    # The downgrade must stay LOUD: the failing cargo leg's own diagnostics are
    # appended under the skew note and printed beneath the ok mark, so an
    # off-pin failure is never silently swallowed (#602).
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
    # The downgrade is scoped to the cargo legs alone: a genuinely failing
    # non-rust leg still fails the run under skew.
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
    # The resolved cargo satisfies the pin: the verdict IS canonical, so a
    # failing cargo leg blocks exactly as before — no new hole in the gate.
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
    # No pixi-pinned rust: whatever toolchain the repo declares is canonical
    # (ADR-0007), so there is no skew to detect — and no probe exec is spent.
    rec = _Recorder(codes={"cargo": 101})
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["Cargo.toml", "src/main.rs"]),
        run_tool=rec,
    )
    assert rc == 1
    assert ("cargo", ("--version",)) not in rec.calls


def test_rust_skew_passing_cargo_run_prints_no_note(tmp_path, capsys):
    # The guard is failure-only: a PASSING run on a skewed toolchain stands
    # (CI re-checks canonically), with no skew noise on a green report.
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
    # cargo missing entirely: the probe yields nothing (no skew claim) and the
    # leg's own launch failure stays the standard hard 127.
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
    # fmt runs its in-place fix form (with the injected rustfmt config-path, WS03
    # #516); clippy still runs its check form.
    assert ("cargo", lint.RUST.tools[1].fix) in rec.calls
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
    # rustfmt's own `ignore` is nightly-only). The service snapshots protected `.rs`
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
    # markdownlint ran on the un-ignored file only; the fixture never reached it
    # (canonical config injected ahead of the file, WS03 #516).
    md_batches = [args for binary, args in run_tool.calls if binary == "markdownlint"]
    assert md_batches == [("--config", _MD_CFG, "README.md")]


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
    assert md_batches == [("--config", _MD_CFG, "README.md")]


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
    # prettier: injected `--config` (WS03 #516) precedes the #493 `--no-editorconfig`
    # pin (config_inject, then editorconfig_pin, then the base args — see Tool.argv).
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
    # Pin OFF, but the canonical `--config` is still injected (unconditional, WS03).
    assert (
        "prettier",
        ("--config", _PRETTIER_CFG, "--check", "--log-level", "warn", "data.json"),
    ) in rec.calls


def test_run_pin_decision_independent_of_lint_ignore(tmp_path, monkeypatch):
    # The pin is a git-tracking fact, NOT a routing decision: a `[lint].ignore`
    # entry that would drop `.editorconfig` from the routed files must NOT flip
    # hermeticity (#493, copilot / agy round 1). Exercises the REAL
    # `_tracks_root_editorconfig` over a monkeypatched git seam: the repo tracks a
    # root .editorconfig AND `.shipit.toml` ignores it — the pin still reads OFF.
    (tmp_path / ".shipit.toml").write_text('[lint]\nignore = [".editorconfig"]\n')
    # The tracked root config must declare `root = true` for the pin to disable
    # (#528); write a real one so the real `_read_editorconfig` sees it.
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
    # Pin OFF despite the ignore: shfmt/prettier honor the tracked root config.
    # (The canonical `--config` is injected regardless — unconditional, WS03 #516.)
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


@pytest.mark.skipif(
    shutil.which("shfmt") is None or shutil.which("shellcheck") is None,
    reason="shell linters (shfmt/shellcheck) not on PATH in this env",
)
def test_tracked_non_root_editorconfig_keeps_pin_on_hostile_ancestor(
    tmp_path, monkeypatch
):
    """#528: a repo tracking a NON-`root = true` `.editorconfig` at its root, with a
    HOSTILE ancestor `.editorconfig` planted ABOVE it, yields the SAME shfmt verdict.

    A presence-only `tracks_editorconfig` would DISABLE the pin here (the root file
    IS tracked) and let shfmt walk UP into the hostile ancestor, reflowing the
    tab-indented fixture. Requiring `root = true` keeps the pin ON via the REAL
    decision path, so the verdict does not move.
    """
    base = tmp_path / "base"
    repo = base / "repo"
    repo.mkdir(parents=True)
    # TAB-indented shell fixture: clean under canonical `-i 0`, but a 2-space
    # ancestor .editorconfig would reflow it.
    (repo / "t.sh").write_text("#!/bin/bash\nif true; then\n\techo hi\nfi\n")
    # Tracked-but-NON-root .editorconfig at the repo root (NO `root = true`).
    # INNOCUOUS on purpose — it touches `[*.md]`, NOT the `.sh` fixture — so the
    # repo-local config alone cannot reflow `t.sh`. That isolates the planted
    # ANCESTOR as the SOLE hostile influence: under a presence-only revert the pin
    # would disable and shfmt would walk up into `base/.editorconfig` (root=true,
    # `[*.sh]` space/2) and reflow the tab fixture; requiring `root = true` keeps
    # the pin ON so it does not.
    (repo / ".editorconfig").write_text(
        "[*.md]\nindent_style = space\nindent_size = 2\n"
    )
    # Hostile ancestor .editorconfig ABOVE the repo (root = true, space/2) — what a
    # presence-only check would let shfmt walk up into.
    (base / ".editorconfig").write_text(_HOSTILE_EDITORCONFIG)

    # Drive the REAL pin decision: real `_tracks_root_editorconfig` over the git
    # seam (tracked non-root config) reading `repo/.editorconfig` → False → pin ON.
    monkeypatch.setattr(lint.git, "repo_root", lambda *, cwd: str(repo))
    monkeypatch.setattr(lint.git, "ls_files", lambda *, cwd: [".editorconfig", "t.sh"])

    # (1) The crisp core guarantee — pin ON for the tracked non-root config,
    # independent of any linter.
    assert lint._tracks_root_editorconfig(repo) is False

    # (2) End-to-end with the REAL shfmt subprocess (default `_run_tool`): the
    # hostile ancestor does NOT reflow the verdict because the pin stays ON.
    runs: list[lint.ToolRun] = []
    rc = lint.run(str(repo), discover=_fake_discover(["t.sh"]), runs_out=runs)
    shfmt_run = next(r for r in runs if r.binary == "shfmt")
    assert shfmt_run.ok is True
    assert rc == 0


def test_malformed_shipit_toml_fails_clean_not_traceback(tmp_path, capsys):
    # A malformed `[lint].ignore` surfaces as the CLI's uniform `error: …` line +
    # exit 1 (the cli_errors shell, ADR-0030), NOT a raw ConfigError traceback
    # escaping mid-gate — the same clean failure every config-reading verb gives.
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


# --------------------------------------------------------------------------
# prettier plugin-load fail-open (#498)
# --------------------------------------------------------------------------


def _prettier_output(rc, output):
    """A run_tool that returns ``rc`` + ``output`` (on stderr) for prettier and a
    clean 0 for everything else — to drive the orchestrator's fail-open branch
    without a real prettier."""

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
    # The Node ESM/CJS resolver abort a .prettierrc plugin absent from
    # node_modules produces — package OR module phrasing, paired with the
    # `imported from` discriminator (real prettier 3.x output, #498).
    pkg = (
        "[error] Cannot find package 'prettier-plugin-svelte' imported from /x/noop.js"
    )
    mod = "[error] Cannot find module 'prettier-plugin-tailwindcss' imported from /x/noop.js"
    assert lint.is_prettier_plugin_load_failure("prettier", 1, pkg)
    assert lint.is_prettier_plugin_load_failure("prettier", 1, mod)


def test_is_prettier_plugin_load_failure_never_swallows_a_real_failure():
    # The critical #498 guardrail: prettier's OWN formatting verdict carries no
    # `imported from`, so a genuinely dirty JSON must NOT match (else it would
    # fail open and a broken file would pass).
    dirty = "[warn] data.json\n[warn] Code style issues found in the above file."
    assert not lint.is_prettier_plugin_load_failure("prettier", 1, dirty)
    # A clean run (rc 0) is never a plugin-load failure.
    assert not lint.is_prettier_plugin_load_failure("prettier", 0, "")
    # The carve-out is prettier-only — the same phrase from another tool hard-fails.
    other = "Cannot find package 'x' imported from y"
    assert not lint.is_prettier_plugin_load_failure("markdownlint", 1, other)
    # A resolver phrase WITHOUT `imported from` (a bare require stack) does not
    # match either — the pairing keeps the match tight.
    assert not lint.is_prettier_plugin_load_failure(
        "prettier", 1, "Cannot find module 'x'\nRequire stack: ..."
    )


def test_partition_plugin_scoped_splits_by_extension():
    # The `.svelte` (plugin-scoped) files peel off into their own batch, order
    # preserved; the plugin-free `.json`/`.ts`/`.tsx` stay together (#520).
    paths = ["a.json", "src/W.svelte", "b.ts", "c.tsx", "d/E.svelte"]
    free, scoped = lint.partition_plugin_scoped(paths, (".svelte",))
    assert free == ["a.json", "b.ts", "c.tsx"]
    assert scoped == ["src/W.svelte", "d/E.svelte"]


def test_partition_plugin_scoped_no_declared_extensions_is_all_free():
    # Every non-web leg declares no plugin-scoped extensions → one plugin-free
    # batch, exactly the single-batch behaviour it has always had.
    paths = ["a.py", "b.rs"]
    free, scoped = lint.partition_plugin_scoped(paths, ())
    assert free == paths
    assert scoped == []


def test_prettier_svelte_leg_fails_open(tmp_path, capsys):
    # Orchestrator wiring: the plugin-scoped `.svelte` leg is the ONE that may
    # fail open — prettier aborts on the missing prettier-plugin-svelte, so that
    # leg passes and the reason is printed under the `ok` mark (#498/#520).
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
    assert "web:prettier" not in out  # never listed among the failures
    assert "not installed" in out  # the skip reason IS surfaced, not silent
    assert "ok   web" in out


def test_prettier_json_leg_never_fails_open(tmp_path, capsys):
    # The flip side of the split (#520): the plugin-FREE `.json`/`.ts` leg is
    # `fail_open_ok=False`, so even a (structurally impossible under the scoped
    # canonical config) plugin-load abort on it hard-fails rather than masking a
    # potentially dirty file. The JSON verdict can NEVER be zeroed by #498.
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
    # THE regression the split exists for (#520, codex/copilot review of #542):
    # a repo with a `.svelte` file (its plugin absent → prettier aborts on load)
    # AND a dirty `.json` must still RED on the JSON. Before the split both rode
    # one prettier invocation, so the svelte plugin abort zeroed the whole batch
    # and the dirty JSON passed. Split apart, the `.svelte` leg fails open while
    # the `.json` leg keeps its real (failing) verdict.
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
    assert rc == 1  # the dirty JSON is NOT masked by the svelte plugin abort
    out = capsys.readouterr().out
    assert "LINT: FAILED" in out
    assert "web:prettier" in out
    # The svelte leg still fails open — its skip reason is surfaced alongside.
    assert "not installed" in out


def test_prettier_dirty_json_still_fails_in_orchestrator(tmp_path, capsys):
    # The fail-open carve-out must NOT broaden: prettier's own dirty-file warning
    # (no `imported from`) still reddens the leg.
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
    """Acceptance (#498), leg (a): a fixture whose `.prettierrc` names an ABSENT
    plugin — the svelte/tailwind shape — linted WITHOUT node_modules aborts real
    prettier on LOAD. The JSON leg FAILS OPEN (verdict 0) instead of the false
    failure the raw nonzero exit would produce.

    Runs the REAL prettier (pixi-provisioned), mirroring the real-shfmt pin test.
    The plugin is named to be definitely absent, so the abort is deterministic and
    does not depend on any real plugin being un-installed.
    """
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
    """Acceptance (#498), leg (b): with prettier able to run (no missing plugin),
    a genuinely dirty JSON still FAILS — the carve-out is scoped to the
    plugin-load abort, never a real format verdict. Real prettier, no node_modules.

    Injects NO config (`canonical_config` → None) so real prettier runs on its
    own bare defaults, isolating the plugin-load carve-out (leg (a) above) from
    a genuine format failure with no canonical config in the picture at all.
    This does NOT exercise the shipped canonical body — see
    `test_prettier_dirty_json_still_fails_under_real_canonical_config` below for
    the regression proof that the PRODUCTION default resolver (the packaged
    `prettierrc.yaml`, its svelte/tailwind plugins scoped to `.svelte` via
    `overrides` rather than declared globally) still fails a dirty JSON in a
    plugin-less env, which a global `plugins:` list previously masked (#525
    review).
    """
    (tmp_path / "data.json").write_text('{"a":      1}\n')  # bad spacing → dirty
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        tracks_root_editorconfig=lambda root: True,
        canonical_config=lambda tool, root: None,
    )
    assert rc == 1


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_dirty_json_still_fails_under_real_canonical_config(tmp_path):
    """Regression (#525 review): a dirty JSON must still hard-fail under the
    PACKAGED canonical config (WS03 #516), via the PRODUCTION default resolver
    — no ``canonical_config`` override, unlike ``test_prettier_dirty_json_still_fails_real``
    above, which deliberately routes around the shipped config to isolate the
    plugin-load leg.

    This is the proof the split-config fix (shipped ``prettierrc.yaml``'s
    svelte/tailwind plugins now live under an ``overrides: [files: "*.svelte"]``
    block, never the top-level ``plugins:`` list) actually closes the hole: with
    the plugins global, injecting the real canonical config in a tree with no
    ``node_modules`` made prettier abort on plugin load, which
    ``is_prettier_plugin_load_failure`` (#498) read as environment-not-provisioned
    and failed the leg OPEN — silently passing this same dirty file. Scoping the
    plugins to `.svelte` means the JSON leg never touches them, so the genuine
    dirty-file verdict surfaces instead of being masked.
    """
    (tmp_path / "data.json").write_text('{"a":      1}\n')  # bad spacing → dirty
    rc = lint.run(
        str(tmp_path),
        discover=_fake_discover(["data.json"]),
        tracks_root_editorconfig=lambda root: True,
    )
    assert rc == 1


# --------------------------------------------------------------------------
# Canonical-config injection (ADR-0037, WS01 #514) — the WS03 seam mechanism
# --------------------------------------------------------------------------


def test_config_inject_omitted_when_no_path_is_resolved():
    # A placeholder `config_inject` fragment is OMITTED when the resolver yields no
    # path (config_path=None): argv falls back to the unpinned form rather than
    # emitting a dangling `--config {config}` — the safety valve for an inline-config
    # or not-yet-shipped tool (WS03 #516).
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.config_inject == ("--config", lint.CONFIG_PLACEHOLDER)
    assert ruff_check.argv(fix=False) == ("check",)
    assert ruff_check.argv(fix=False, config_path=None) == ("check",)


def test_config_inject_substitutes_and_prepends_the_path_unconditionally():
    # Given a canonical config path (what WS03's resolver will yield), the fragment
    # is substituted and PREPENDED — regardless of repo state (unconditional,
    # unlike the editorconfig pin).
    ruff_check = lint.PYTHON.tools[0]
    assert ruff_check.argv(fix=False, config_path="/canon/ruff.toml") == (
        "--config",
        "/canon/ruff.toml",
        "check",
    )
    # Fix form takes the injection just the same.
    assert ruff_check.argv(fix=True, config_path="/canon/ruff.toml") == (
        "--config",
        "/canon/ruff.toml",
        "check",
        "--fix",
    )


def test_config_inject_inline_fragment_is_always_applied():
    # A fragment with NO placeholder is an inline config (command-line flags, no
    # file) and is prepended unconditionally, path or not.
    tool = lint.Tool("demo", ("--check",), config_inject=("--std",))
    assert tool.argv(fix=False) == ("--std", "--check")
    assert tool.argv(fix=False, config_path="/ignored") == ("--std", "--check")


def test_config_inject_substring_placeholder_form():
    # The placeholder may be a SUBSTRING of a token (`--config={config}`), not
    # only its own token — argv must match per-token, else the exact-element `in`
    # check misses it and injects the literal `{config}` (round 1, agy).
    tool = lint.Tool("demo", ("--check",), config_inject=("--config={config}",))
    # With a path: the substring is substituted in place and the token prepended.
    assert tool.argv(fix=False, config_path="/canon/demo.toml") == (
        "--config=/canon/demo.toml",
        "--check",
    )
    # Without a path (pre-WS03): OMITTED, never the literal `{config}` leaking through.
    assert tool.argv(fix=False) == ("--check",)
    assert tool.argv(fix=False, config_path=None) == ("--check",)


def test_config_inject_coexists_with_the_editorconfig_pin():
    # prettier carries BOTH: the canonical-config placeholder (resolved in WS03)
    # and the #493 editorconfig pin (still gated). With a path AND the pin on, both
    # prepend — injection first, then the pin, then the base.
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
    # The path-config tools (--config/-c <file>) each carry a placeholder
    # `config_inject` that WS03's `_canonical_config` fills. The inline/suffix tools
    # (shellcheck, shfmt, cargo, lexd) inject via their check/fix args instead and
    # are intentionally exempt from the placeholder form.
    file_config_binaries = {"ruff", "prettier", "markdownlint", "yamllint"}
    for lang in lint.LANGS:
        for tool in lang.tools:
            if tool.binary in file_config_binaries:
                # Mirror argv's per-token substring match (round 1, agy): the
                # placeholder may be its own token OR a substring of one, so the
                # assertion cannot be exact-element (`in` on the tuple).
                assert any(
                    lint.CONFIG_PLACEHOLDER in tok for tok in tool.config_inject
                ), tool.binary


def test_run_injects_the_resolved_canonical_config_path(tmp_path):
    # End-to-end: a stub resolver (standing in for WS03's real one) makes run()
    # prepend the canonical config path to every tool that declares a placeholder.
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
    # With no resolver supplied, the DEFAULT `_canonical_config` (WS03 #516) injects
    # each file-config tool's shipped `shipit/data` body — this is the production
    # path, not a stub. A tool with no shipped file-config (lexd) is untouched.
    rec = _Recorder()
    lint.run(str(tmp_path), discover=_fake_discover(["a.py", "d.lex"]), run_tool=rec)
    assert ("ruff", ("--config", _RUFF_CFG, "check", "a.py")) in rec.calls
    assert ("ruff", ("--config", _RUFF_CFG, "format", "--check", "a.py")) in rec.calls
    assert ("lexd", ("check", "d.lex")) in rec.calls  # no file-config → unpinned


def test_canonical_config_maps_file_config_tools_and_only_those(tmp_path):
    # The resolver returns an EXISTING shipped body for each file-config tool and
    # None for every inline-config tool (shellcheck/shfmt/cargo) and lexd. The path
    # is the packaged data file, independent of `root` (so injection fires in any
    # tree — the ancestor-config block the env scrub cannot give).
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
    # #608 (phos-editor/core's red lint cell): an org-registered self-hosted
    # runner label is a FLEET-level fact, declared once in the canonical
    # actionlint.yaml the gate pins via -config-file (ADR-0037) — never a
    # per-repo carve-out (the gate ignores repo-local actionlint.yaml). gpu_t4
    # is the org's NVIDIA T4 GPU larger runner (phos-core's gpu-painting lane).
    # Parse the config and assert gpu_t4 under self-hosted-runner.labels
    # specifically — a raw substring match would also see the explanatory
    # comment above the mapping, staying green if the real label were deleted.
    body = Path(lint.data_path("actionlint.yaml")).read_text(encoding="utf-8")
    config = yaml.safe_load(body)
    assert "gpu_t4" in config["self-hosted-runner"]["labels"]


def test_rust_fmt_injects_the_shipped_rustfmt_config():
    # rustfmt's canonical config rides the cargo fmt tuples inline via `--config-path`
    # (it must follow cargo's `--`, so it is NOT a config_inject placeholder). The
    # path is the shipped body, present on disk.
    fmt = lint.RUST.tools[1]
    assert "--config-path" in fmt.check and "--config-path" in fmt.fix
    assert fmt.check[fmt.check.index("--config-path") + 1] == lint._RUSTFMT_CONFIG_PATH
    assert Path(lint._RUSTFMT_CONFIG_PATH).is_file()


def test_data_path_resolves_a_real_file_and_fails_fast_when_missing():
    # `_data_path` must hand a linter subprocess a real on-disk `--config` path, so
    # `shipit lint` never breaks at canonical-config injection (ADR-0037). shipit.data
    # is a NAMESPACE package, so `resources.files` yields a MultiplexedPath (not
    # os.PathLike) — `str(...joinpath(name))` resolves the shipped body regardless,
    # and the existence check (NOT os.fspath) is the fail-fast: a missing body raises
    # a clear FileNotFoundError rather than a confusing TypeError or a bogus path.
    path = lint._data_path("ruff.toml")
    assert Path(path).is_file()
    with pytest.raises(FileNotFoundError):
        lint._data_path("no-such-canonical-config.toml")


def test_shipped_ruff_toml_matches_the_repo_root_carve_out():
    # The carve-out (#516) lives in TWO byte-identical places: shipit's repo-root
    # `ruff.toml` (what a direct `ruff` / editor reads; the acceptance "shipit's ruff
    # config lives in ruff.toml, not pyproject") and the packaged `shipit/data/ruff.toml`
    # (what the gate injects fleet-wide). A drift between them would make the gate and
    # a bare `ruff` disagree, so pin them equal. `pyproject.toml` must carry NO ruff
    # config anymore.
    data = Path(lint.data_path("ruff.toml")).read_bytes()
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / "ruff.toml").read_bytes() == data
    # No `[tool.ruff…]` TABLE header survives in pyproject (a prose mention in a
    # `#` comment is fine — match a real header line only).
    pyproject_lines = (repo_root / "pyproject.toml").read_text().splitlines()
    assert not any(line.lstrip().startswith("[tool.ruff") for line in pyproject_lines)


# --------------------------------------------------------------------------
# Ambient-config env scrub (ADR-0037, WS01 #514)
# --------------------------------------------------------------------------


def test_is_ambient_config_var_matches_the_leaky_keys():
    for leaky in (
        "HOME",
        "SHELLCHECK_OPTS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "YAMLLINT_CONFIG_FILE",
        "RUFF_CONFIG",
        # The Rust config-override vars (round 1, codex): without these
        # `cargo clippy` reads a machine-local config.toml / clippy.toml.
        "CARGO_HOME",
        "CLIPPY_CONF_DIR",
    ):
        assert lint._is_ambient_config_var(leaky), leaky
    # PATH and the tool runtime must survive — scrubbing them would break launch.
    # PKG_CONFIG_PATH / FONTCONFIG_PATH are standard build vars, NOT tool config:
    # the old `"_CONFIG" in key` substring wrongly stripped them and broke
    # cargo/C builds (round 1, agy) — they are absent from the explicit
    # denylist (`_TOOL_CONFIG_ENV_VARS`), so the scrub PRESERVES them.
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
    # them and broke cargo/C builds (round 1, agy).
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
    # A COMPLETE env (replace_env=True), so PATH must be preserved or nothing launches.
    assert scrubbed["PATH"] == "/usr/bin"
    # Build vars preserved — not tool config.
    assert scrubbed["PKG_CONFIG_PATH"] == "/usr/lib/pkgconfig"
    assert scrubbed["FONTCONFIG_PATH"] == "/etc/fonts"


def test_run_tool_passes_scrubbed_env_with_replace_env(tmp_path, monkeypatch):
    # The single exec choke point applies the scrub: execrun.run gets the scrubbed
    # env AND replace_env=True (so it is the child's WHOLE environment, the only
    # way to REMOVE an inherited var). No new execrun plumbing — reuses env/replace_env.
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


# --------------------------------------------------------------------------
# The invariance property gate (ADR-0037, LNT01-WS02 #515)
#
# THE acceptance gate for the whole LNT01 epic. Parametrized over every
# registered TOOL (each `Lang.tools` entry, not just each Lang — round 2, codex):
# for every tool, lint a fixture TWICE — once clean, once with a HOSTILE config
# planted in an ambient source (a directory ABOVE the repo root, `$HOME`, or the
# tool's config ENV VAR) — and assert THAT TOOL'S OWN verdict (its `ToolRun` out of
# `runs_out`, not the language's aggregate 0/1) does not move. The gate blocks
# ambient config two ways, one per source class:
#
#   * ENV / $HOME — the `_run_tool` scrub drops the tool's config env var and
#     `$HOME`/`XDG_*`, so the child never sees them (WS01 #514).
#   * ANCESTOR DIRECTORY — a config file in a PARENT of the checkout is a
#     filesystem walk the env scrub CANNOT reach; it is blocked only by the
#     injected argv (a file-config tool's `--config`, cargo fmt's `--config-path`,
#     shfmt's `-i 0`, shellcheck's `--norc`). A tool with no such injection
#     consults the ancestor file and leaks — the gate catches exactly that.
#
# PER TOOL, not per Lang (round 2, codex): a per-Lang aggregate verdict lets one
# tool MASK another — the SC2086-dirty shell fixture makes shellcheck dominate the
# 0/1, so removing shfmt's `-i 0` would not move it and an shfmt leak would be
# invisible. Reading each tool's OWN `ToolRun` (`_target_run`) closes that;
# `test_per_tool_assertion_catches_the_shfmt_leak_...` is the direct regression
# proof (aggregate masked, per-tool caught).
#
# Because the cases are BUILT from `LANGS.tools`, a newly-registered tool is
# subject to the invariant on day one: it needs a `_ToolSpec` or the coverage guard
# (`test_every_registered_tool_has_a_hermeticity_spec`) fails, and once specced, a
# tool whose ambient config leaks fails its own case. THREE ancestor sub-cases leak
# TODAY (clippy's clippy.toml, cargo's `.cargo/config.toml`, lexd's `.lex.toml` —
# walk-based discovery the scrub can't block and no injection closes yet); they are
# `xfail(strict)` against the tracked follow-up #526, so the gate is green now and
# reddens the instant a leak is closed (unexpected pass) OR a NEW leak appears
# (unexpected fail elsewhere).
#
# NO VACUOUS GREEN — three independent guards, because a hermeticity gate that
# cannot FAIL is worse than none:
#   * SILENT-SKIP FLOOR — every case is `skipif(binary missing)`, so a missing
#     tool skips = green. `test_core_lint_tools_present_...` turns that into a
#     LOUD failure for the CORE set, so a dropped binary can never be a false
#     green on CI. (Optional toolchains — rust, lex — legitimately skip; see the
#     CI-coverage caveat below.)
#   * BASELINE PIN — each spec pins `expected_ok`; the property asserts the clean
#     run's own `ToolRun.ok` equals it BEFORE `clean.ok == hostile.ok`, so a
#     drifted fixture cannot collapse the invariant to a hollow `True == True`.
#   * TEETH — the tests below prove that removing a tool's injection MOVES that
#     tool's own run (pinned to exact endpoints, not a bare `!=`).
#
# CI COVERAGE (#532 — closed): the `test` task lives in the pixi `test` FEATURE, so
# the canonical `pixi run test` and CI's `pixi run -e test test` are the SAME command
# in the SAME env — one that carries the Rust toolchain (cargo/clippy/rustfmt,
# conda-forge-pinned in pixi.lock) AND provisions lexd at its pin INLINE (the task cmd
# runs `shipit provision lexd` before pytest, IN this env, so lexd lands in the test
# env's own bin — not a depends-on, which pixi would run in its home/default env). So
# the rust/lex hermeticity cases — and the
# `xfail(strict)` "leak closed → reminder" signal for clippy/cargo/lexd (#526) — run
# BOTH locally and on CI: closing #526 reddens the gate everywhere, with no local/CI split
# in either direction. The `skipif(binary missing)` guards remain only the last-ditch
# fallback for a genuinely toolchain-less machine (it still runs the core cases and
# skips these); the canonical gate no longer relies on them for the optional
# toolchains, because that gate now provisions them.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Vector:
    """One ambient-config injection vector a spec is exercised on.

    ``name`` is the vector's UNIQUE id suffix within its spec — usually the
    injection mechanism itself (``"env"`` / ``"home"`` / ``"ancestor"`` / ``"cwd"``),
    but a spec that exercises the SAME mechanism twice through DIFFERENT config
    files disambiguates them by name (e.g. cargo's ``"ancestor"`` clippy.toml walk
    vs its ``"ancestor-cargo-config"`` ``.cargo/config.toml`` walk — two distinct
    ancestor leaks on the one clippy tool). ``kind`` is the mechanism that actually
    drives the plant (``_ambient_runs`` branches on it); it defaults to ``name``, so
    the common name==mechanism vectors need not set it.

    ``hostile_name`` / ``hostile_body`` OVERRIDE the spec's defaults for THIS vector,
    and ``home_rel`` overrides the spec's ``$HOME``-relative path for a ``home``-kind
    vector (else the spec's are used) — needed when one tool leaks through more than
    one ambient config file, so each vector plants its own. Overrides resolve with
    ``is not None``, so an intentionally falsy value (e.g. an empty body) still wins.

    ``xfail`` is a reason string when this vector LEAKS on the current mechanism (the
    ancestor walk for clippy's clippy.toml, cargo's ``.cargo/config.toml``, and
    lexd's ``.lex.toml`` — tracked in #526) — the case is then ``xfail(strict)`` so
    an unexpected pass (leak closed) reddens the gate as a reminder to drop it.
    """

    name: str
    xfail: str | None = None
    kind: str | None = None  # injection mechanism; defaults to ``name``
    hostile_name: str | None = None  # override spec.hostile_name for THIS vector
    hostile_body: str | None = None  # override spec.hostile_body for THIS vector
    home_rel: str | None = None  # override spec.home_rel for a "home"-kind vector

    @property
    def source(self) -> str:
        """The injection mechanism ``_ambient_runs`` branches on — ``kind`` when a
        vector's name differs from its mechanism, else the ``name`` itself."""
        return self.kind or self.name


@dataclass(frozen=True)
class _ToolSpec:
    """How to exercise ONE registered tool (a single ``Lang.tools`` entry) for
    hermeticity. The gate asserts PER TOOL, not per language (round 2, codex).

    WHY per tool: a per-``Lang`` aggregate 0/1 verdict lets one tool MASK another.
    The shell fixture is SC2086-dirty, so shellcheck dominates the aggregate — and
    removing shfmt's ``-i 0`` would not move it, hiding an shfmt leak. Same for
    rust (``cargo fmt`` behind clippy) and python (``ruff format`` behind
    ``ruff check``). So each tool gets its OWN fixture + hostile targeting THAT
    tool's ambient config source, and the assertion reads the tool's own
    :class:`~shipit.lint.ToolRun` out of ``runs_out`` (:func:`_target_run`),
    so one tool's baseline can never mask another tool's leak.

    ``target_check`` is the tool's :attr:`Tool.check` tuple — the identity that
    selects both the ``Tool`` in ``Lang.tools`` (:func:`_tool_for`) and its
    ``ToolRun`` (the run whose label ends with that tuple; :func:`_target_run`).
    ``tag`` names the case in test ids.

    ``binaries`` lists EVERY executable the run needs on PATH — not just the
    top-level command. For cargo that includes the ``cargo-clippy`` / ``cargo-fmt``
    subcommand shims AND the ``clippy-driver`` / ``rustfmt`` drivers (round 2,
    copilot): ``cargo`` can be present without them, and a case that ran anyway
    would have BOTH its clean and hostile runs fail identically on the missing
    component, so the tool's verdict would not move and an ``xfail(strict)`` leak
    case would spuriously XPASS (a false green the floor forbids).

    ``expected_ok`` PINS the tool's clean-run outcome (True = clean under
    canonical, False = deliberately dirty). The property asserts the clean run's
    own ``ToolRun.ok`` equals it, so the invariance can never be VACUOUS: if a
    fixture drifts, ``clean.ok == hostile.ok`` could hold for the wrong reason —
    the pin catches that.

    ``env_var`` / ``env_kind`` (a config FILE path, a DIR to search, or inline
    FLAGS), ``home_rel`` (the ``$HOME``-relative user-config path, or
    ``home_via_lexd`` when lexd's own ``config set --scope user`` must place it
    OS-correctly), and ``pluginless_prettier`` (sidestep the #498 fail-open) feed
    the individual vectors — see :func:`_ambient_runs`.
    """

    lang: str
    tag: str  # readable tool id for the case (e.g. "ruff-format", "shfmt")
    target_check: tuple[str, ...]  # the Tool.check tuple identifying this tool
    binaries: tuple[str, ...]
    fixture: tuple[tuple[str, str], ...]  # (repo-relative path, content)
    hostile_name: str  # the config filename the tool DISCOVERS (ancestor/cwd/home)
    hostile_body: str  # config that flips the tool's verdict when honored
    vectors: tuple[_Vector, ...]
    expected_ok: bool  # clean-run baseline: True clean-under-canonical, False dirty
    env_var: str | None = None
    env_kind: str = "file"  # "file" | "dir" | "flags"
    env_flags: str | None = None  # the value for env_kind == "flags"
    home_rel: str | None = None  # $HOME-relative user-config path
    home_via_lexd: bool = False  # lexd persists user scope via `lexd config set`
    pluginless_prettier: bool = False  # sidestep the #498 fail-open (see below)


# The registry-parametrized spec table — one _ToolSpec PER registered tool
# (round 2, codex). Fixtures are CLEAN under the canonical config and the hostile
# TIGHTENS (True→False), except shellcheck + lexd whose fixtures are deliberately
# DIRTY under canonical so a DISABLING/DOWNGRADING hostile has teeth (False→True) —
# a clean fixture cannot be flipped by a config that only relaxes rules.
_RUST_CARGO_TOML = '[package]\nname = "hermtest"\nversion = "0.0.0"\nedition = "2021"\n'
_RUST_LIB = "pub fn f(a: i32, b: i32, c: i32) -> i32 {\n    a + b + c\n}\n"
_RUST_FIXTURE = (("Cargo.toml", _RUST_CARGO_TOML), ("src/lib.rs", _RUST_LIB))
# The Rust toolchain — cargo AND the subcommand shims (`cargo-clippy`/`cargo-fmt`,
# which `cargo clippy`/`cargo fmt` actually exec) AND the drivers
# (`clippy-driver`/`rustfmt`). A partial install missing any of these must SKIP,
# not drift red (round 2, copilot).
_RUST_BINS = ("cargo", "cargo-clippy", "cargo-fmt", "clippy-driver", "rustfmt")
# A shfmt fixture that is TAB-indented (clean under canonical `-i 0`) but that a
# hostile 2-space `.editorconfig` would reflow — so shfmt's `-i 0` pin is what
# keeps it clean, independently of shellcheck (which co-runs on the same file).
_SHFMT_FIXTURE = (("t.sh", "#!/bin/bash\nif true; then\n\techo hi\nfi\n"),)
_HOSTILE_EDITORCONFIG = "root = true\n[*.sh]\nindent_style = space\nindent_size = 2\n"
# A bogus `lex.*` canonical directive trips two DENY-level rules
# (unknown_lex_canonical + schema.unknown_label), so `lexd check` fails — the
# dirty baseline the hostile downgrade below flips.
_LEX_DOC = ":: lex.totallybogusdirective ::\n\nBody.\n"
# lexd honors `"allow"` (not `"warn"`) to actually SUPPRESS these two findings;
# both must be downgraded or the survivor keeps `lexd check` red.
_LEX_HOSTILE = (
    '[diagnostics.rules]\nunknown_lex_canonical = "allow"\n\n'
    '[diagnostics.rules.schema]\nunknown_label = "allow"\n'
)

_HERM_SPECS: tuple[_ToolSpec, ...] = (
    # ruff CHECK — file-config (`--config`). Clean `x = 1`; hostile selects E501 at
    # line-length 1 so any content overflows. env var + user config + ancestor
    # walk all overridden by the injected `--config`, env/home also scrubbed.
    _ToolSpec(
        lang="python",
        tag="ruff-check",
        target_check=("check",),
        binaries=("ruff",),
        fixture=(("m.py", "x = 1\n"),),
        hostile_name="ruff.toml",
        hostile_body='line-length = 1\n[lint]\nselect = ["E501"]\n',
        vectors=(_Vector("env"), _Vector("home"), _Vector("ancestor")),
        expected_ok=True,  # `x = 1` is clean under canonical ruff
        env_var="RUFF_CONFIG",
        env_kind="file",
        home_rel=".config/ruff/ruff.toml",
    ),
    # ruff FORMAT — the SECOND python tool, asserted independently so `ruff check`
    # cannot mask it (round 2, codex). Clean `x = "hi"` (canonical double-quote);
    # hostile `[format] quote-style = single` would reflow it. The ancestor vector
    # exercises ruff-format's OWN `--config` injection; the env/home (scrub-blocked,
    # environment-level not tool-argv) sources are already proven by ruff-check.
    _ToolSpec(
        lang="python",
        tag="ruff-format",
        target_check=("format", "--check"),
        binaries=("ruff",),
        fixture=(("m.py", 'x = "hi"\n'),),
        hostile_name="ruff.toml",
        hostile_body='[format]\nquote-style = "single"\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=True,  # double-quote `x = "hi"` is clean under canonical ruff
    ),
    # cargo CLIPPY — inline `-D warnings`, NO config injection. Clean 3-arg fn.
    # TWO independent ancestor leaks ride this one tool, each `xfail(strict)` → #526:
    #   * clippy.toml (item 2): hostile drops too-many-arguments-threshold to 2 so it
    #     fires. CLIPPY_CONF_DIR is scrubbed (hermetic); the ancestor clippy.toml walk
    #     has no injection to block it and LEAKS.
    #   * .cargo/config.toml (item 3): hostile `[build] rustflags` denies the
    #     restriction lint `clippy::arithmetic_side_effects`, which the fixture's
    #     `a + b + c` then trips. cargo (the driver) merges `.cargo/config.toml` from
    #     every ANCESTOR dir of the checkout, and no injection overrides that walk —
    #     a SECOND ancestor source the env scrub cannot reach.
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
            # #526 item 3: cargo's OWN `.cargo/config.toml` ancestor walk — a second
            # ancestor mechanism on the clippy tool, plants a different file/body.
            _Vector(
                "ancestor-cargo-config",
                kind="ancestor",
                xfail="#526 (item 3): cargo walks ancestors for .cargo/config.toml; env-scrub insufficient",
                hostile_name=".cargo/config.toml",
                hostile_body='[build]\nrustflags = ["-Dclippy::arithmetic_side_effects"]\n',
            ),
        ),
        expected_ok=True,  # canonical-clean 3-arg fn under clippy
        env_var="CLIPPY_CONF_DIR",
        env_kind="dir",
    ),
    # cargo FMT — the SECOND rust tool, asserted independently so clippy cannot mask
    # it (round 2, codex). Inline `--config-path <shipped rustfmt.toml>` injection.
    # Clean 4-space fn; a hostile ancestor `rustfmt.toml` (tab_spaces = 2) would
    # reflow it, but `--config-path` overrides the ancestor walk → hermetic. rustfmt
    # reads NO config env var, so the ancestor walk is its only ambient source.
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
        expected_ok=True,  # canonical-clean 4-space fn under the shipped rustfmt.toml
    ),
    # shellcheck — inline flags incl. the `--norc` this WS adds. Fixture is
    # SC2086-dirty (`echo $1`); hostile `.shellcheckrc` (or SHELLCHECK_OPTS) would
    # disable SC2086. --norc blocks the .shellcheckrc walk + $HOME copy; the scrub
    # blocks SHELLCHECK_OPTS + $HOME.
    _ToolSpec(
        lang="shell",
        tag="shellcheck",
        target_check=("--norc", "--severity=info"),
        binaries=("shellcheck",),
        fixture=(("s.sh", "#!/bin/bash\necho $1\n"),),
        hostile_name=".shellcheckrc",
        hostile_body="disable=SC2086\n",
        vectors=(_Vector("env"), _Vector("home"), _Vector("ancestor")),
        expected_ok=False,  # `echo $1` is SC2086-dirty under canonical
        env_var="SHELLCHECK_OPTS",
        env_kind="flags",
        env_flags="--exclude=SC2086",
        home_rel=".shellcheckrc",
    ),
    # shfmt — the SECOND shell tool, asserted independently so shellcheck cannot
    # mask it (round 2, codex — this is the exact masking codex flagged). Clean
    # TAB-indented fixture; hostile ancestor `.editorconfig` (2-space) would reflow
    # it, but shfmt's `-i 0` pin makes it skip `.editorconfig` → hermetic.
    _ToolSpec(
        lang="shell",
        tag="shfmt",
        target_check=("-d",),
        binaries=("shfmt",),
        fixture=_SHFMT_FIXTURE,
        hostile_name=".editorconfig",
        hostile_body=_HOSTILE_EDITORCONFIG,
        vectors=(_Vector("ancestor"),),
        expected_ok=True,  # tab-indented fixture is clean under canonical `-i 0`
    ),
    # yamllint — file-config (`-c`). Clean out-of-order mapping; hostile enables
    # key-ordering so it fires. `-c` overrides env + user config + ancestor walk.
    _ToolSpec(
        lang="yaml",
        tag="yamllint",
        target_check=("--strict",),
        binaries=("yamllint",),
        fixture=(("d.yml", "b: 2\na: 1\n"),),
        hostile_name=".yamllint",
        hostile_body="extends: default\nrules:\n  key-ordering: enable\n",
        vectors=(_Vector("env"), _Vector("home"), _Vector("ancestor")),
        expected_ok=True,  # clean under canonical yamllint (key-ordering off)
        env_var="YAMLLINT_CONFIG_FILE",
        env_kind="file",
        home_rel=".config/yamllint/config",
    ),
    # actionlint — file-config (`-config-file`), the first PATH-CLAIMED Lang
    # (TOL01-WS04 #553). Its one ambient source is the project config it
    # auto-discovers at `<project root>/.github/actionlint.yaml`, the project
    # root found by walking UP from the workflow file to a `.git` entry — the
    # fixture plants `.git/HEAD` so the tmp repo IS a project (no `.git`, no
    # discovery, and the case would be vacuously green). The fixture is DIRTY
    # under canonical (`my-gpu-box` is not among the shipped labels — those
    # list only the ORG'S registered runners, e.g. `gpu_t4`, #608); a hostile
    # repo config DECLARING the label flips it green
    # when honored — the injected `-config-file` is what blocks it (for
    # actionlint even the repo's OWN tracked config is ambient; the gate owns
    # the config outright). No config env var, no $HOME config, and the walk
    # stops at the project root — so `cwd` (the markdownlint pattern) is the
    # one vector. The embedded `run:` script keeps clear of shellcheck
    # findings so the actionlint verdict is the runner-label rule alone.
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
        expected_ok=False,  # unknown runner label is dirty under canonical labels
    ),
    # prettier — file-config (`--config`). Only ambient source is the ancestor
    # `.prettierrc` walk (prettier reads no config env var and no plain $HOME
    # file). Clean 2-space JSON; hostile sets tabWidth 8 so it reflows. JSON is the
    # fixture here because it is neutral to prettier's semi / quote / trailingComma
    # defaults — so it stays clean under BOTH the injected canonical (this spec's
    # hermeticity run) AND prettier's pure defaults (the teeth run's `_none_resolver`
    # baseline). A `.ts` fixture cannot be clean under both (canonical sets
    # semi=false/trailingComma=none, defaults set semi=true/trailingComma=all), so
    # the TS leg gets its OWN dedicated test that only exercises the injected path
    # (test_prettier_ts_leg_is_ambient_config_blind_and_config_governed). `.svelte`
    # is not hermeticity-tested at all: its plugins are absent from shipit's
    # conda-forge lint env by design (they ride the consumer repo's own npm
    # devDependencies, see prettierrc.yaml), so a `.svelte` fixture would FAIL OPEN
    # (#498) and prove nothing — `.svelte` routing is covered by test_lang_for.
    #
    # The REAL shipped canonical body is injected here (no plugin-less swap): its
    # svelte/tailwind plugins are scoped to a `*.svelte` override, so a `.json`
    # fixture never resolves them — prettier genuinely enforces the JSON rules
    # rather than aborting on plugin load and failing open (#498). That the real
    # config REDDENS a dirty JSON (not fails it open) is proven by
    # test_prettier_dirty_json_still_fails_under_real_canonical_config; keeping the
    # real config here exercises the SAME `--config` injection seam on a real rule
    # (the teeth test proves removing the injection reddens).
    _ToolSpec(
        lang="web",
        tag="prettier",
        target_check=("--check", "--log-level", "warn"),
        binaries=("prettier",),
        fixture=(("data.json", '{\n  "a": {\n    "b": 1\n  }\n}\n'),),
        hostile_name=".prettierrc",
        hostile_body='{"tabWidth": 8}\n',
        vectors=(_Vector("ancestor"),),
        expected_ok=True,  # 2-space JSON is clean under the canonical config
    ),
    # markdownlint — file-config (`--config`). It reads config ONLY from the
    # working directory (no ancestor walk, no $HOME, no env var), so the sole
    # ambient-ish source is a repo-local `.markdownlint.json`; the injected
    # `--config` makes even THAT ignored — the gate owns the config outright.
    # Clean short prose; hostile sets MD013 line_length 3 so it fires.
    _ToolSpec(
        lang="markdown",
        tag="markdownlint",
        target_check=(),
        binaries=("markdownlint",),
        fixture=(("r.md", "# Title\n\nHello world, this is a line of prose.\n"),),
        hostile_name=".markdownlint.json",
        hostile_body='{"default": false, "MD013": {"line_length": 3}}\n',
        vectors=(_Vector("cwd"),),
        expected_ok=True,  # short prose is clean under canonical markdownlint
    ),
    # lexd — reads `.lex.toml` (`[diagnostics.rules]` severity), NO config
    # injection. Fixture trips two deny-level rules (dirty); hostile downgrades
    # them to allow so `lexd check` passes. The user-scope config is $HOME-rooted
    # (scrubbed, hermetic); the ancestor `.lex.toml` walk has no injection and
    # LEAKS — xfail(strict) → #526.
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
        expected_ok=False,  # bogus lex.* directive trips two deny rules (dirty)
        home_via_lexd=True,
    ),
)


def _spec(tag: str) -> _ToolSpec:
    return next(s for s in _HERM_SPECS if s.tag == tag)


def _tool_for(spec: _ToolSpec) -> lint.Tool:
    """The registered ``Tool`` a spec targets — matched by its ``check`` tuple in
    the (possibly monkeypatched) ``lint.LANGS``, so a teeth test that swaps a tool
    resolves the swapped one."""
    lang = next(lang for lang in lint.LANGS if lang.name == spec.lang)
    return next(t for t in lang.tools if t.check == spec.target_check)


def _target_run(runs: list[lint.ToolRun], tool: lint.Tool) -> lint.ToolRun:
    """The single ``ToolRun`` produced by ``tool`` — matched by binary AND the
    tool's ``check`` tuple appearing as the label's token SUFFIX (``run`` builds
    the label as ``binary`` + argv, argv ends with the check/base tuple). This
    disambiguates two tools that share a binary (ruff check/format, cargo
    clippy/fmt) without relying on run ORDER, so one tool's outcome is read in
    isolation — the crux of the per-tool (non-masking) assertion."""
    # Match the check tuple as a STRING suffix of the label, not a token-split
    # suffix: `argv` puts `base` (the check tuple) last, so `label` ends with
    # `" ".join(tool.check)`. A `.split()` suffix fractures when a check token is
    # an absolute path containing spaces (e.g. rustfmt's embedded
    # `_RUSTFMT_CONFIG_PATH` under a checkout dir with spaces), so match the raw
    # string instead — the path stays contiguous in both label and expected (agy).
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
    """A ``canonical_config`` that injects nothing — the teeth tests use it to
    STRIP a tool's config injection and prove the injection is load-bearing."""
    return None


def _pluginless_resolver(pluginless_path: Path):
    """A `canonical_config` that swaps prettier's body for a plugin-less one (so it
    does not fail open, #498) while every other tool keeps its real shipped config."""

    def resolve(tool: lint.Tool, root: Path) -> str | None:
        if tool.binary == "prettier":
            return str(pluginless_path)
        return lint._canonical_config(tool, root)

    return resolve


def _ambient_runs(base, spec, vector, *, planted, monkeypatch, canonical):
    """Lint ``spec``'s fixture in a FRESH tree under ``base``, optionally with the
    hostile config PLANTED in ``vector``, and return every :class:`ToolRun` — the
    PER-TOOL outcomes, so the caller asserts on the TARGET tool's run alone
    (:func:`_target_run`) rather than the masked aggregate verdict (round 2, codex).

    ``vector`` is a :class:`_Vector`: its ``source`` picks the injection mechanism
    (``ancestor`` / ``cwd`` / ``env`` / ``home``) and its ``hostile_name`` /
    ``hostile_body`` override the spec's defaults, so one tool can be exercised on
    two DIFFERENT ancestor files (clippy's clippy.toml AND cargo's
    ``.cargo/config.toml``, #526 items 2 and 3).

    Each call builds its own ``base/repo`` (whose parent ``base`` is the ancestor
    directory) so the clean and planted runs never share on-disk state or a tool
    cache — the only difference between them is the planted hostile. The gate's
    real exec path runs: default ``_run_tool`` (real subprocess + env scrub), the
    editorconfig pin forced ON (``tracks_root_editorconfig`` → False), and the
    given ``canonical`` resolver for the injected ``--config``.
    """
    base = Path(base)
    repo = base / "repo"
    repo.mkdir(parents=True)
    for rel, content in spec.fixture:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    # Per-vector hostile OVERRIDE (else the spec's default) — a tool that leaks
    # through more than one ambient file plants a different one per vector.
    # ``is not None`` (not ``or``): an override must win even when falsy, e.g. an
    # intentionally EMPTY body that neutralizes rather than tightens.
    hostile_name = (
        vector.hostile_name if vector.hostile_name is not None else spec.hostile_name
    )
    hostile_body = (
        vector.hostile_body if vector.hostile_body is not None else spec.hostile_body
    )

    def _plant(dest: Path) -> None:
        # hostile_name may be NESTED (e.g. `.cargo/config.toml`), so ensure its
        # parent dirs exist before writing.
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(hostile_body)

    # Neutralize any inherited value of the tool's config env var, so the clean
    # run is genuinely clean and the ONLY difference is what this call plants.
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
            else:  # a config FILE the env var points at
                cfg = base / hostile_name
                _plant(cfg)
                monkeypatch.setenv(spec.env_var, str(cfg))
    elif vector.source == "home":
        # Point $HOME at a controlled dir (empty when clean, hostile when planted)
        # and drop XDG_CONFIG_HOME so the tool resolves ~/.config beneath it. The
        # scrub removes both, so the child sees neither — hermetic either way.
        home = base / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        if planted:
            if spec.home_via_lexd:
                # lexd persists user scope OS-correctly under $HOME; suppress the
                # two deny rules the fixture trips so honoring the user config would
                # flip the verdict (the scrub blocks $HOME, so it must not).
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
                # Honor a per-vector home_rel override consistently with the other
                # kinds (else the spec's), then plant via the shared _plant helper.
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
    """A UNIQUE, readable case-id stem for one registered tool. Includes the
    ``check`` tuple, not just the binary: ruff (check+format) and cargo
    (clippy+fmt) register two tools sharing ONE binary, so a binary-only id would
    COLLIDE if both were ever missing a spec — and pytest would raise a
    duplicate-id error instead of reaching the intended clean NO-SPEC failure
    (round 4, copilot). ``check`` is the tool's registry identity (the coverage
    guard keys on it), so it is guaranteed unique per lang."""
    disc = "-".join(tool.check) if tool.check else "noargs"
    return f"{lang.name}-{tool.binary}-{disc}"


def _hermeticity_cases():
    """Build the parametrized cases by ITERATING every registered TOOL — each
    `Lang.tools` entry, not just each Lang (round 2, codex): per-tool so one tool's
    baseline can't mask another's leak. A registered tool with no `_ToolSpec` yields
    a NO-SPEC case that fails; each spec's vectors become `lang-tag-vector` cases,
    skipped when a binary is absent and `xfail(strict)` on the vectors that leak
    today (#526). Case ids are unique per TOOL (`_tool_id` / `spec.tag`), never just
    per binary, so two same-binary tools can't collide (round 4, copilot)."""
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


# The lint tools shipped in shipit's DEFAULT pixi env (pixi.toml) — the set CI
# provisions and gates on. Every hermeticity case is `skipif(binary missing)`, so
# a tool absent from PATH makes its cases SILENTLY SKIP = green. That is the
# intended behavior for OPTIONAL toolchains (rust, lex — see CI-coverage note in
# the section header), but a false green for the core set: were a provisioning
# regression to drop one on CI, its hermeticity cases would all skip and the gate
# would pass while proving nothing. This floor is the guard against exactly that.
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
    """Every CORE lint tool MUST be on PATH — a missing one FAILS here rather than
    silently skipping its hermeticity cases into a false green (#515). The suite is
    meant to run in shipit's pixi env (`pixi run test`), where pixi.toml provisions
    all of these; a failure means the env is broken, not that the test is wrong."""
    missing = [t for t in CORE_LINT_TOOLS if shutil.which(t) is None]
    assert not missing, (
        f"core lint tool(s) missing from PATH: {missing}. The hermeticity gate "
        f"`skipif`s an absent tool, so a missing CORE binary makes its cases skip "
        f"and the gate pass for the WRONG reason. Run in shipit's pixi env "
        f"(`pixi run test`), where pixi.toml provisions these."
    )


def test_every_registered_tool_has_a_hermeticity_spec():
    """The registry gate (#515), now PER TOOL (round 2, codex): every
    ``(lang, tool)`` in `LANGS` MUST have a `_ToolSpec`, so a newly-registered tool
    is subject to the invariant automatically — it cannot be added without either a
    spec proving it is ambient-blind or an xfail pinning its leak. A per-LANG guard
    would let a second tool added to an existing lang (e.g. another `cargo` leg)
    slip in unproven; keying on `tool.check` closes that."""
    registered = {(lang.name, tool.check) for lang in lint.LANGS for tool in lang.tools}
    specced = {(s.lang, s.target_check) for s in _HERM_SPECS}
    assert registered <= specced, (
        f"registered tool(s) with no _ToolSpec (add one — each tool must prove "
        f"hermeticity or xfail its leak): "
        f"{sorted((lang, ' '.join(chk)) for lang, chk in registered - specced)}"
    )


@pytest.mark.parametrize("spec,vector", _hermeticity_cases())
def test_lint_tool_is_ambient_config_blind(spec, vector, tmp_path, monkeypatch):
    """For each registered TOOL × ambient vector: the TOOL'S OWN verdict (its
    :class:`ToolRun`, read from `runs_out` — NOT the language's aggregate 0/1) is
    identical with and without a hostile config planted in that source (ADR-0037,
    #515). Per-tool so one tool's baseline cannot mask another's leak (round 2,
    codex): the shell fixture failing on shellcheck no longer hides an shfmt leak.

    A ``NO-SPEC`` case (a registered tool missing from `_HERM_SPECS`) fails loudly.
    The THREE walk-based ancestor sub-cases — clippy's clippy.toml, cargo's
    `.cargo/config.toml`, and lexd's `.lex.toml` — are `xfail(strict)` (#526): they
    leak today, so that tool's own run MOVES and the assertion fails as expected.

    The clean run's ``ToolRun.ok`` is PINNED to ``spec.expected_ok`` before the
    invariance check, so the property can never pass vacuously: if a fixture drifts
    and both runs collapse to the same non-baseline outcome, ``clean.ok ==
    hostile.ok`` would be hollow — the pin fails first and names the drift. (The pin
    holds even for the leak cases: "clean" plants no hostile, so it is the canonical
    baseline regardless of whether the hostile run leaks.)
    """
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


# --- Teeth: prove the invariant is NOT vacuously green -----------------------


def _teeth_target_oks(tmp_path, spec, tool, monkeypatch, canonical):
    """Run ``spec``'s ancestor case clean + hostile under ``canonical`` and return
    ``(clean.ok, hostile.ok)`` for the TARGET ``tool``'s own run — the shape every
    teeth test asserts on."""
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
    """Removing ruff-CHECK's `--config` injection turns its ancestor case RED —
    proof the injection is load-bearing, not a vacuous pass (#515). With the
    resolver returning None, ruff falls back to discovery and honors the hostile
    ancestor `ruff.toml`, so ruff-check's OWN run MOVES (ok True → False)."""
    spec = _spec("ruff-check")
    oks = _teeth_target_oks(
        tmp_path, spec, _tool_for(spec), monkeypatch, _none_resolver
    )
    assert oks == (True, False)


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_gate_has_teeth_removing_ruff_format_config_reddens(tmp_path, monkeypatch):
    """The SECOND python tool, proven independently (round 2, codex): removing
    ruff-FORMAT's `--config` lets the hostile ancestor `ruff.toml`
    (`quote-style = single`) reflow `x = "hi"`, so ruff-format's OWN run MOVES
    (ok True → False). The old per-Lang aggregate — dominated by ruff-check — could
    have masked this; the per-tool run cannot."""
    spec = _spec("ruff-format")
    oks = _teeth_target_oks(
        tmp_path, spec, _tool_for(spec), monkeypatch, _none_resolver
    )
    assert oks == (True, False)


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not on PATH")
def test_gate_has_teeth_removing_actionlint_config_file_greens(tmp_path, monkeypatch):
    """Removing actionlint's `-config-file` injection lets it auto-discover the
    hostile repo `.github/actionlint.yaml` (which declares the fixture's unknown
    runner label), so its OWN run MOVES (ok False → True) — proof the injection
    is load-bearing and the `cwd` invariance case is not vacuous (TOL01-WS04
    #553). Inlined rather than `_teeth_target_oks` because actionlint's vector
    is `cwd` (project-root discovery), not the helper's hardcoded ancestor."""
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
    """The prettier leg has REAL teeth despite the #498 plugin-load fail-open: with
    prettier's own defaults (no plugins to load, so no #498 fail-open) genuinely
    format, so dropping `--config` lets the hostile ancestor `.prettierrc`
    (tabWidth 8) reflow the JSON and prettier's OWN run MOVES (ok True → False). If
    prettier were silently failing open, both runs would be ok and this would not
    move — the assertion catches that."""
    spec = _spec("prettier")
    oks = _teeth_target_oks(
        tmp_path, spec, _tool_for(spec), monkeypatch, _none_resolver
    )
    assert oks == (True, False)


@pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier not on PATH")
def test_prettier_ts_leg_is_ambient_config_blind_and_config_governed(
    tmp_path, monkeypatch
):
    """The TypeScript leg (LNT01-WS07 #520): a `.ts` file routes to the `web`
    prettier tool and the injected `--config` GOVERNS its formatting — blind to an
    ambient ancestor `.prettierrc`, and load-bearing (a DIFFERENT injected config
    moves the verdict). prettier parses TS natively (no plugin), so there is no
    #498 fail-open to mask the check; the clean-baseline pin keeps it non-vacuous.

    The shared `_HERM_SPECS` prettier case stays JSON (neutral to prettier's
    semi/quote/trailingComma defaults, so it is clean under both the injected
    canonical AND the teeth run's pure defaults); `.ts` cannot satisfy both, so it
    gets this dedicated test on the injected path alone.
    """
    # A `.ts` fixture that is clean under a plugin-less canonical body
    # (singleQuote / tabWidth 2 / semi false / trailingComma none) and reflows only
    # on indentation width — so a tabWidth change is what moves its verdict.
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
    # Ambient-blind: the 2-space fixture is clean under the injected canonical, and
    # a hostile ancestor `.prettierrc` (tabWidth 8) does NOT move it — `--config` wins.
    clean = _run_ts("clean", canon, planted=False)
    assert clean.ok is True, "TS clean baseline must genuinely pass (no fail-open)"
    hostile = _run_ts("hostile", canon, planted=True)
    assert clean.ok == hostile.ok, "ambient ancestor .prettierrc leaked into the TS leg"
    # Config-governed (load-bearing): swap the INJECTED canonical to tabWidth 8 and
    # the 2-space `.ts` fixture reflows — proof the injected `--config` really drives
    # the TS verdict, so the blindness above is not vacuous.
    wide = "singleQuote: true\ntabWidth: 8\nsemi: false\ntrailingComma: none\n"
    governed = _run_ts("governed", wide, planted=False)
    assert governed.ok is False, "injected --config does not govern the TS leg"


@pytest.mark.skipif(
    any(shutil.which(b) is None for b in _RUST_BINS),
    reason="rust toolchain not on PATH",
)
def test_gate_has_teeth_removing_cargo_fmt_config_path_reddens(tmp_path, monkeypatch):
    """The SECOND rust tool, proven independently (round 2, codex): cargo-fmt's
    canonical config is the INLINE `--config-path <shipped rustfmt.toml>` (not the
    resolver), so strip it from the registry. Then the hostile ancestor
    `rustfmt.toml` (tab_spaces = 2) is honored via rustfmt's own ancestor walk and
    cargo-fmt's OWN run MOVES (ok True → False) — a leak clippy could have masked."""
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
    """WS07 acceptance 4 (#520): hermeticity holds for a per-manifest cargo run
    TARGETED at a SUBDIR crate (the tauri `src-tauri/` shape), not just a root
    crate. The existing rust `_HERM_SPECS` case fixtures the crate AT the repo root;
    this proves the invariance survives per-manifest TARGETING into a subdirectory.

    The crate lives at `src-tauri/`; `manifest_roots` targets `src-tauri` alone (as
    `test_manifest_roots_subdir_crate_only` asserts on the pure function). A hostile
    `rustfmt.toml` (tab_spaces = 2) planted at the REPO ROOT is an ancestor rustfmt
    would walk into from the subdir crate — but cargo-fmt's inline `--config-path
    <shipped rustfmt.toml>` overrides that walk, so the subdir crate's fmt verdict is
    identical with and without the ancestor config. (Non-vacuous: the sibling teeth
    test proves that WITHOUT `--config-path` the same ancestor MOVES the verdict.)"""
    fixture = (
        ("src-tauri/Cargo.toml", _RUST_CARGO_TOML),
        # a clean 4-space fn — clean under the shipped rustfmt.toml, reflowed by a
        # honored tab_spaces = 2, so the ancestor config has teeth if it leaks.
        ("src-tauri/src/lib.rs", _RUST_LIB),
    )
    # The per-manifest targeting is the subdir crate, never the repo root.
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
            # the hostile ancestor sits at the REPO ROOT, ABOVE the src-tauri crate
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
    """Removing shellcheck's `--norc` turns its ancestor case RED — proof `--norc`
    is load-bearing (#515). Swap the registry entry for the pre-#515 form (by BINARY
    NAME, not index — round 2, agy) and the hostile ancestor `.shellcheckrc`
    (disable=SC2086) is honored, so shellcheck's OWN run MOVES (ok False → True).
    This is the exact ancestor leak the env scrub could not close."""
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
    # `pre_norc` has a different `check`, so target it directly rather than via the
    # spec's `target_check`.
    oks = _teeth_target_oks(
        tmp_path, _spec("shellcheck"), pre_norc, monkeypatch, lint._canonical_config
    )
    # SC2086-dirty fixture: without `--norc`, the hostile `.shellcheckrc` disables
    # SC2086 and the run goes clean — ok False → True.
    assert oks == (False, True)


@pytest.mark.skipif(
    shutil.which("shellcheck") is None or shutil.which("shfmt") is None,
    reason="shell linters not on PATH",
)
def test_per_tool_assertion_catches_the_shfmt_leak_a_lang_aggregate_masks(
    tmp_path, monkeypatch
):
    """THE round-2 regression proof (codex): a per-Lang aggregate verdict MASKS a
    second tool's leak; asserting PER TOOL does not.

    The fixture is BOTH SC2086-dirty (shellcheck fails, so the aggregate 0/1 is 1 in
    EVERY run) AND tab-indented (shfmt territory). De-pin shfmt (drop `-i 0`) and
    plant a hostile ancestor `.editorconfig` (2-space): the AGGREGATE verdict stays
    1 clean and hostile — shellcheck masks the movement, so a per-Lang gate sees
    NOTHING — but shfmt's OWN `ToolRun` moves (ok True → False). The per-tool
    assertion catches exactly the leak the aggregate hides."""
    # De-pin shfmt by BINARY NAME, not index (round 2, agy): robust to tool order.
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
    # SC2086-dirty AND tab-indented: shellcheck fails (dominating the aggregate),
    # shfmt is the tool under test.
    spec = replace(
        _spec("shfmt"),
        fixture=(("m.sh", "#!/bin/bash\nif true; then\n\techo $1\nfi\n"),),
    )
    tool = _tool_for(spec)  # the de-pinned shfmt (check unchanged)
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
    # AGGREGATE is MASKED — shellcheck's SC2086 pins the whole-lang verdict at 1 in
    # both runs, so a per-Lang gate would see no movement.
    assert lint.verdict(clean_runs) == 1
    assert lint.verdict(hostile_runs) == 1
    # PER-TOOL is NOT masked — shfmt's own run moves clean → hostile.
    assert _target_run(clean_runs, tool).ok is True
    assert _target_run(hostile_runs, tool).ok is False
