"""The stage-from-prefix copy step (:mod:`shipit.staging`, conda-direct #1079) —
the durable, generic mirror of `fetch-deps`.

Unit-tested against a CONSTRUCTED env prefix (the copy logic is pure filesystem;
the REAL `.conda` → pixi resolve round trip that produces such a prefix is proven
in test_release_publish.py's staging round-trip test). Covers file + directory
copies, the executable-bit round trip a shipped binary needs, idempotent re-runs,
the loud missing-source refusal, the bounded-destination guard, the refuse-links
model (a symlink/junction on the source, env anchor, or staging root is refused,
never followed), and feature (named-env) prefix resolution.
"""

import pytest

from shipit import config, staging


def _prefix(root, env="default"):
    """The env prefix dir under ``root`` — where the tests plant "resolved" files
    the way pixi would extract a conda package."""
    p = root / ".pixi" / "envs" / env
    p.mkdir(parents=True, exist_ok=True)
    return p


def _plant(path, content, *, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


# --------------------------------------------------------------------------
# File + directory copies
# --------------------------------------------------------------------------


def test_stages_a_tool_binary_keeping_the_exec_bit(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "#!/bin/sh\necho hi\n", mode=0o755)

    (result,) = staging.stage(
        tmp_path,
        [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
    )

    dest = tmp_path / "resources" / "lexd-lsp"
    assert dest.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert dest.stat().st_mode & 0o111, "the exec bit must survive the copy"
    assert result == staging.StagedFile(
        "lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp", is_dir=False, executable=True
    )


def test_stages_a_non_executable_data_file_as_plain(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "share" / "tsx" / "grammar.wasm", "WASM", mode=0o644)

    (result,) = staging.stage(
        tmp_path,
        [config.StageEntry("tsx", "share/tsx/grammar.wasm", "resources/grammar.wasm")],
    )

    dest = tmp_path / "resources" / "grammar.wasm"
    assert dest.read_text(encoding="utf-8") == "WASM"
    # A data file is not forced executable — only the source's own bits carry.
    assert not (dest.stat().st_mode & 0o111)
    assert result.executable is False and result.is_dir is False


def test_stages_a_whole_directory_recursively(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "share" / "tsx" / "queries" / "highlights.scm", "; hl", mode=0o644)
    _plant(prefix / "share" / "tsx" / "queries" / "sub" / "locals.scm", "; loc")

    (result,) = staging.stage(
        tmp_path,
        [config.StageEntry("tsx", "share/tsx/queries", "resources/queries")],
    )

    assert (tmp_path / "resources" / "queries" / "highlights.scm").read_text() == "; hl"
    assert (
        tmp_path / "resources" / "queries" / "sub" / "locals.scm"
    ).read_text() == "; loc"
    assert result.is_dir is True


def test_creates_missing_parent_dirs_for_a_nested_dest(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)

    staging.stage(
        tmp_path,
        [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/nested/deep/lsp")],
    )

    assert (tmp_path / "resources" / "nested" / "deep" / "lsp").is_file()


def test_multiple_entries_stage_in_declaration_order(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "bin", mode=0o755)
    _plant(prefix / "share" / "tsx" / "g.wasm", "wasm")

    result = staging.stage(
        tmp_path,
        [
            config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp"),
            config.StageEntry("tsx", "share/tsx/g.wasm", "resources/g.wasm"),
        ],
    )

    assert [r.dest for r in result] == ["resources/lexd-lsp", "resources/g.wasm"]


def test_empty_entry_list_is_a_clean_no_op(tmp_path):
    assert staging.stage(tmp_path, []) == []


# --------------------------------------------------------------------------
# Idempotent re-runs (a re-runnable build step)
# --------------------------------------------------------------------------


def test_rerun_overwrites_a_prior_file_stage(tmp_path):
    prefix = _prefix(tmp_path)
    entry = config.StageEntry("tsx", "share/tsx/g.wasm", "resources/g.wasm")
    _plant(prefix / "share" / "tsx" / "g.wasm", "v1")
    staging.stage(tmp_path, [entry])

    # The env re-resolves to a new version; a second stage replaces, not refuses.
    _plant(prefix / "share" / "tsx" / "g.wasm", "v2")
    staging.stage(tmp_path, [entry])

    assert (tmp_path / "resources" / "g.wasm").read_text(encoding="utf-8") == "v2"


def test_rerun_replaces_a_prior_directory_stage(tmp_path):
    prefix = _prefix(tmp_path)
    entry = config.StageEntry("tsx", "share/tsx/queries", "resources/queries")
    _plant(prefix / "share" / "tsx" / "queries" / "a.scm", "a")
    staging.stage(tmp_path, [entry])

    # The source dir loses a file across a re-resolve; the stale dest file must not
    # linger (the whole dir is replaced, not merged).
    (prefix / "share" / "tsx" / "queries" / "a.scm").unlink()
    _plant(prefix / "share" / "tsx" / "queries" / "b.scm", "b")
    staging.stage(tmp_path, [entry])

    dest = tmp_path / "resources" / "queries"
    assert (dest / "b.scm").read_text() == "b"
    assert not (dest / "a.scm").exists(), "a replaced dir must not keep stale files"


# --------------------------------------------------------------------------
# Loud refusals
# --------------------------------------------------------------------------


def test_missing_source_points_at_install(tmp_path):
    _prefix(tmp_path)  # env exists but the package was never resolved into it
    with pytest.raises(staging.StagingError, match="not materialized"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
        )


def test_symlinked_staging_root_out_of_tree_is_refused_and_touches_nothing(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    outside = tmp_path.parent / "outside-escape-target"
    outside.mkdir(exist_ok=True)
    # A committed `resources` -> /outside symlink must not steer the copy beyond
    # the checkout, even though the dest string itself is clean/relative.
    (tmp_path / "resources").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="must be a real directory"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
        )
    # The escape was refused BEFORE any write — nothing landed through the symlink.
    assert not (outside / "lexd-lsp").exists()


@pytest.mark.parametrize("target", [".", ".git"])
def test_symlinked_staging_root_redirecting_into_the_checkout_is_refused(
    tmp_path, target
):
    # A `resources` -> `.`/`.git` symlink resolves INSIDE the checkout, so a mere
    # "inside the tree" check would pass and let the strict-descendant bound point at
    # the checkout root or git metadata. Requiring the staging root to resolve to
    # EXACTLY <root>/resources refuses the redirect and protects those dirs.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (tmp_path / "resources").symlink_to(tmp_path / target, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="must be a real directory"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
        )
    assert (gitdir / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main"


def test_capitalized_real_staging_root_is_accepted(tmp_path):
    # On a case-insensitive FS (macOS/APFS) a real `Resources` dir is reached via
    # `resources`, and `resolve()` returns the on-disk case `Resources`. The guard
    # tests the component's OWN nature (not a symlink), so the capitalized real dir
    # is ACCEPTED — a string-equality check against `<root>/resources` would have
    # false-rejected it. On a case-sensitive FS `resources` simply doesn't exist yet
    # and is created; either way the stage succeeds and the file lands.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    (tmp_path / "Resources").mkdir()
    case_insensitive = (tmp_path / "resources").is_dir()  # True on APFS/macOS

    (result,) = staging.stage(
        tmp_path,
        [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
    )

    assert result.dest == "resources/lexd-lsp"
    landed = tmp_path / ("Resources" if case_insensitive else "resources") / "lexd-lsp"
    assert landed.read_text(encoding="utf-8") == "x"


# --------------------------------------------------------------------------
# Bounded destination — the data-loss class is unexpressible by construction
# --------------------------------------------------------------------------
#
# Every dest must resolve to a STRICT DESCENDANT of the staging root
# `<root>/resources`. `.`, the checkout root, `.git`/`.Git`, `.pixi` are all
# outside the staging root, so none of them can drive an rmtree — the class is
# closed by the one rule, not a per-name denylist.


@pytest.mark.parametrize("dest", [".", ".git/HEAD", ".Git/HEAD", ".pixi/envs", "x"])
def test_dest_outside_the_staging_root_is_refused_and_touches_nothing(tmp_path, dest):
    # A domain-level StageEntry bypasses the parse guard; the RUNTIME bound (compared
    # on resolved absolute paths, so the `.Git` case-alias cannot slip past) must
    # refuse each and leave the sentinels untouched. `.Git` aliases `.git` only on a
    # case-insensitive fs, but is rejected everywhere because it is not under
    # resources/ regardless.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    sentinel = tmp_path / "PRECIOUS.txt"
    sentinel.write_text("do not delete me", encoding="utf-8")
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    with pytest.raises(staging.StagingError, match="staging root"):
        staging.stage(tmp_path, [config.StageEntry("lexd-lsp", "bin/lexd-lsp", dest)])
    assert sentinel.read_text(encoding="utf-8") == "do not delete me"
    assert (gitdir / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main"


def test_dest_equal_to_the_staging_root_itself_is_refused(tmp_path):
    # The staging root is not a STRICT descendant of itself — `dest = "resources"`
    # would rmtree the whole bundle dir; refuse it.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    with pytest.raises(staging.StagingError, match="strict descendant"):
        staging.stage(
            tmp_path, [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources")]
        )


def test_file_source_refuses_to_overwrite_an_existing_directory_dest(tmp_path):
    # A file source mapped onto an existing directory dest would `rmtree` the whole
    # directory to drop one file — refuse loudly rather than wipe it.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "tool", "x", mode=0o755)
    existing = tmp_path / "resources" / "keep-me"
    existing.mkdir(parents=True)
    (existing / "data.txt").write_text("valuable", encoding="utf-8")

    with pytest.raises(staging.StagingError, match="wipe a directory"):
        staging.stage(
            tmp_path,
            [config.StageEntry("tool", "bin/tool", "resources/keep-me")],
        )
    assert (existing / "data.txt").read_text(encoding="utf-8") == "valuable"


# --------------------------------------------------------------------------
# Refuse links — the copy never follows a symlink/junction, it only copies real
# files and real directories, so the whole symlink-escape class is closed at once
# --------------------------------------------------------------------------


def test_top_level_source_symlink_is_refused(tmp_path):
    prefix = _prefix(tmp_path)
    secret = tmp_path.parent / "host-secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    # A conda package plants bin/evil -> /outside/host-secret.txt inside the env.
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "bin" / "evil").symlink_to(secret)

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(
            tmp_path, [config.StageEntry("evil", "bin/evil", "resources/evil")]
        )
    assert not (tmp_path / "resources" / "evil").exists()


def test_intermediate_source_symlink_component_is_refused(tmp_path):
    # A link is refused ANYWHERE on the source path, not just the leaf: `share/pkg`
    # here is a symlink out of the prefix, and `bin/tool` is the leaf under it.
    prefix = _prefix(tmp_path)
    outside = tmp_path.parent / "outside-pkg"
    _plant(outside / "tool", "loot", mode=0o755)
    (prefix / "share").mkdir(parents=True, exist_ok=True)
    (prefix / "share" / "pkg").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(
            tmp_path, [config.StageEntry("pkg", "share/pkg/tool", "resources/tool")]
        )
    assert not (tmp_path / "resources" / "tool").exists()


def test_nested_directory_source_symlink_is_refused(tmp_path):
    prefix = _prefix(tmp_path)
    outside = tmp_path.parent / "outside-tree"
    outside.mkdir(exist_ok=True)
    (outside / "loot.txt").write_text("LOOT", encoding="utf-8")
    tree = prefix / "share" / "pkg" / "data"
    _plant(tree / "real.txt", "real")
    # A symlink INSIDE the staged directory tree — refused during the copy walk, so
    # nothing is followed and nothing lands.
    (tree / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(
            tmp_path,
            [config.StageEntry("pkg", "share/pkg/data", "resources/data")],
        )
    assert not (tmp_path / "resources" / "data").exists()


def test_even_an_in_prefix_directory_symlink_is_refused_not_followed(tmp_path):
    # The class-closing point: a directory symlink whose target is INSIDE the prefix
    # (previously "followed and copied") is now REFUSED outright. We never follow a
    # link, so there is no divergence, no DAG/cycle question, no hidden-escape-behind-
    # a-good-link vector — the real files still stage, the link does not.
    prefix = _prefix(tmp_path)
    tree = prefix / "share" / "pkg" / "data"
    _plant(tree / "real.txt", "real")
    target = prefix / "share" / "pkg" / "other"
    _plant(target / "ok.txt", "ok")
    (tree / "linked").symlink_to(target, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(
            tmp_path,
            [config.StageEntry("pkg", "share/pkg/data", "resources/data")],
        )
    assert not (tmp_path / "resources" / "data").exists()


def test_symlinked_pixi_envs_redirecting_out_of_tree_is_refused(tmp_path):
    # The copilot finding: a symlinked `.pixi/envs` (or `.pixi`) redirecting outside
    # the checkout must not let staging read out-of-tree files. The env-anchor
    # link-refusal catches it before the prefix is even resolved.
    outside_env = tmp_path.parent / "outside-env" / "default"
    _plant(outside_env / "bin" / "lex", "loot", mode=0o755)
    pixi = tmp_path / ".pixi"
    pixi.mkdir()
    (pixi / "envs").symlink_to(
        tmp_path.parent / "outside-env", target_is_directory=True
    )

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(tmp_path, [config.StageEntry("lex", "bin/lex", "resources/lex")])
    assert not (tmp_path / "resources" / "lex").exists()


def test_real_files_and_directories_still_stage_fine(tmp_path):
    # Refusing links does NOT touch the normal path: a real binary and a real data
    # directory (what conda actually extracts) stage exactly as before.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "#!/bin/sh\n", mode=0o755)
    _plant(prefix / "share" / "ts" / "highlights.scm", "; hl")
    _plant(prefix / "share" / "ts" / "sub" / "locals.scm", "; loc")

    result = staging.stage(
        tmp_path,
        [
            config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp"),
            config.StageEntry("ts", "share/ts", "resources/ts"),
        ],
    )

    assert (tmp_path / "resources" / "lexd-lsp").stat().st_mode & 0o111
    assert (tmp_path / "resources" / "ts" / "highlights.scm").read_text() == "; hl"
    assert (tmp_path / "resources" / "ts" / "sub" / "locals.scm").read_text() == "; loc"
    assert [r.dest for r in result] == ["resources/lexd-lsp", "resources/ts"]


def test_directory_mode_is_preserved(tmp_path):
    # copystat restores the source dir's mode, so a 0o700 tree is not flattened to
    # the umask default in the shipped bundle (the round-3 dir-metadata minor).
    prefix = _prefix(tmp_path)
    tree = prefix / "share" / "pkg" / "secret"
    _plant(tree / "f.txt", "x")
    tree.chmod(0o700)

    staging.stage(
        tmp_path, [config.StageEntry("pkg", "share/pkg/secret", "resources/secret")]
    )

    assert (tmp_path / "resources" / "secret").stat().st_mode & 0o777 == 0o700


def test_directory_tree_preserves_per_file_exec_bit(tmp_path):
    # The exec-bit reassertion is applied UNIFORMLY per copied file, so a runnable
    # binary nested inside a staged directory keeps its +x (not only a lone-file
    # entry) — the inconsistency the round-2 minor flagged, resolved structurally.
    prefix = _prefix(tmp_path)
    tree = prefix / "share" / "pkg" / "tools"
    _plant(tree / "runme", "#!/bin/sh\n", mode=0o755)
    _plant(tree / "data.json", "{}", mode=0o644)

    staging.stage(
        tmp_path, [config.StageEntry("pkg", "share/pkg/tools", "resources/tools")]
    )

    assert (tmp_path / "resources" / "tools" / "runme").stat().st_mode & 0o111
    assert not (tmp_path / "resources" / "tools" / "data.json").stat().st_mode & 0o111


# --------------------------------------------------------------------------
# --feature validation — a path-shaped feature must not escape .pixi/envs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "x/../..", "foo/bar"])
def test_path_shaped_feature_is_refused(tmp_path, bad):
    _prefix(tmp_path)
    with pytest.raises(staging.StagingError, match="valid feature name"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd", "bin/lexd", "resources/lexd")],
            feature=bad,
        )


# --------------------------------------------------------------------------
# Feature (named-env) prefix resolution
# --------------------------------------------------------------------------


def test_feature_selects_the_named_feature_env_prefix(tmp_path):
    # A named feature resolves `<root>/.pixi/envs/shipit-artifacts-<feature>` —
    # the SAME env_name mapping the vsix staging uses (one source of truth).
    named = _prefix(tmp_path, env="shipit-artifacts-lint")
    _plant(named / "bin" / "lexd", "tool", mode=0o755)

    (result,) = staging.stage(
        tmp_path,
        [config.StageEntry("lexd", "bin/lexd", "resources/lexd")],
        feature="lint",
    )

    assert (tmp_path / "resources" / "lexd").read_text() == "tool"
    assert result.package == "lexd"


def test_feature_prefix_is_env_prefix_of_that_feature(tmp_path):
    # Guards the shared helper wiring: staging's prefix for a feature is exactly
    # artifactdeps.env_prefix for that feature.
    from shipit.install import artifactdeps

    assert artifactdeps.env_prefix(tmp_path, "lint") == (
        tmp_path / ".pixi" / "envs" / "shipit-artifacts-lint"
    )
    assert artifactdeps.env_prefix(tmp_path, None) == (
        tmp_path / ".pixi" / "envs" / "default"
    )
