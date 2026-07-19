"""The stage-from-prefix copy step (:mod:`shipit.staging`, conda-direct #1079) —
the durable, generic mirror of `fetch-deps`.

Unit-tested against a CONSTRUCTED env prefix (the copy logic is pure filesystem;
the REAL `.conda` → pixi resolve round trip that produces such a prefix is proven
in test_release_publish.py's staging round-trip test). Covers file + directory
copies, the executable-bit round trip a shipped binary needs, idempotent re-runs,
the loud missing-source refusal, the symlinked-parent escape guard, and feature
(named-env) prefix resolution.
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


def test_symlinked_parent_escape_is_refused_and_touches_nothing(tmp_path):
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    outside = tmp_path.parent / "outside-escape-target"
    outside.mkdir(exist_ok=True)
    # A committed `resources` -> /outside symlink must not steer the copy beyond
    # the checkout, even though the dest string itself is clean/relative.
    (tmp_path / "resources").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="outside the checkout"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
        )
    # The escape was refused BEFORE any write — nothing landed through the symlink.
    assert not (outside / "lexd-lsp").exists()


# --------------------------------------------------------------------------
# Data-loss defenses — a dest that would wipe the checkout or repo-critical dirs
# --------------------------------------------------------------------------


def test_dest_of_dot_refuses_and_never_rmtrees_the_checkout(tmp_path):
    # `dest = "."` makes dst == root; the pre-fix code would `shutil.rmtree(root)`
    # and delete .git + all work. The guard must refuse LOUDLY, touching nothing.
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    sentinel = tmp_path / "PRECIOUS.txt"
    sentinel.write_text("do not delete me", encoding="utf-8")

    with pytest.raises(staging.StagingError, match="checkout root"):
        staging.stage(tmp_path, [config.StageEntry("lexd-lsp", "bin/lexd-lsp", ".")])
    assert sentinel.read_text(encoding="utf-8") == "do not delete me"


@pytest.mark.parametrize("protected", [".git", ".pixi"])
def test_dest_into_a_repo_critical_dir_refuses_and_touches_nothing(tmp_path, protected):
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    keep = tmp_path / protected / "KEEP"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("keep", encoding="utf-8")

    with pytest.raises(staging.StagingError, match="repo-critical"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", f"{protected}/lexd-lsp")],
        )
    assert keep.read_text(encoding="utf-8") == "keep"


def test_symlinked_dest_onto_root_is_refused(tmp_path):
    # A committed `resources` symlink pointing AT the checkout root resolves dst
    # onto root; the resolved-path root guard must catch it, not just the lexical
    # parse guard (which sees a clean relative dest).
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    (tmp_path / "resources").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="checkout root"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources")],
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
# Source symlink escape — a package-planted link that leaves the prefix
# --------------------------------------------------------------------------


def test_top_level_source_symlink_escaping_the_prefix_is_refused(tmp_path):
    prefix = _prefix(tmp_path)
    secret = tmp_path.parent / "host-secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    # A conda package plants bin/evil -> /outside/host-secret.txt inside the env.
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "bin" / "evil").symlink_to(secret)

    with pytest.raises(staging.StagingError, match="outside the env prefix"):
        staging.stage(
            tmp_path, [config.StageEntry("evil", "bin/evil", "resources/evil")]
        )
    assert not (tmp_path / "resources" / "evil").exists()


def test_nested_directory_source_symlink_escaping_the_prefix_is_refused(tmp_path):
    prefix = _prefix(tmp_path)
    outside = tmp_path.parent / "outside-tree"
    outside.mkdir(exist_ok=True)
    (outside / "loot.txt").write_text("LOOT", encoding="utf-8")
    tree = prefix / "share" / "pkg" / "data"
    _plant(tree / "real.txt", "real")
    # A symlink INSIDE the staged directory tree points outside the prefix;
    # copytree would dereference it and copy host files into the bundle.
    (tree / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="outside the env prefix"):
        staging.stage(
            tmp_path,
            [config.StageEntry("pkg", "share/pkg/data", "resources/data")],
        )
    assert not (tmp_path / "resources" / "data").exists()


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
