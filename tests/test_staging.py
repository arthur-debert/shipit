import pytest

from shipit import config, staging


def _prefix(root, env="default"):
    p = root / ".pixi" / "envs" / env
    p.mkdir(parents=True, exist_ok=True)
    return p


def _plant(path, content, *, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


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


def test_rerun_overwrites_a_prior_file_stage(tmp_path):
    prefix = _prefix(tmp_path)
    entry = config.StageEntry("tsx", "share/tsx/g.wasm", "resources/g.wasm")
    _plant(prefix / "share" / "tsx" / "g.wasm", "v1")
    staging.stage(tmp_path, [entry])

    _plant(prefix / "share" / "tsx" / "g.wasm", "v2")
    staging.stage(tmp_path, [entry])

    assert (tmp_path / "resources" / "g.wasm").read_text(encoding="utf-8") == "v2"


def test_rerun_replaces_a_prior_directory_stage(tmp_path):
    prefix = _prefix(tmp_path)
    entry = config.StageEntry("tsx", "share/tsx/queries", "resources/queries")
    _plant(prefix / "share" / "tsx" / "queries" / "a.scm", "a")
    staging.stage(tmp_path, [entry])

    (prefix / "share" / "tsx" / "queries" / "a.scm").unlink()
    _plant(prefix / "share" / "tsx" / "queries" / "b.scm", "b")
    staging.stage(tmp_path, [entry])

    dest = tmp_path / "resources" / "queries"
    assert (dest / "b.scm").read_text() == "b"
    assert not (dest / "a.scm").exists(), "a replaced dir must not keep stale files"


def test_missing_source_points_at_install(tmp_path):
    _prefix(tmp_path)
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
    (tmp_path / "resources").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="must be a real directory"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp")],
        )
    assert not (outside / "lexd-lsp").exists()


@pytest.mark.parametrize("target", [".", ".git"])
def test_symlinked_staging_root_redirecting_into_the_checkout_is_refused(
    tmp_path, target
):
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


# Bounded destination — the data-loss class is unexpressible by construction


@pytest.mark.parametrize("dest", [".", ".git/HEAD", ".Git/HEAD", ".pixi/envs", "x"])
def test_dest_outside_the_staging_root_is_refused_and_touches_nothing(tmp_path, dest):
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
    prefix = _prefix(tmp_path)
    _plant(prefix / "bin" / "lexd-lsp", "x", mode=0o755)
    with pytest.raises(staging.StagingError, match="strict descendant"):
        staging.stage(
            tmp_path, [config.StageEntry("lexd-lsp", "bin/lexd-lsp", "resources")]
        )


def test_file_source_refuses_to_overwrite_an_existing_directory_dest(tmp_path):
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


def test_top_level_source_symlink_is_refused(tmp_path):
    prefix = _prefix(tmp_path)
    secret = tmp_path.parent / "host-secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "bin" / "evil").symlink_to(secret)

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(
            tmp_path, [config.StageEntry("evil", "bin/evil", "resources/evil")]
        )
    assert not (tmp_path / "resources" / "evil").exists()


def test_intermediate_source_symlink_component_is_refused(tmp_path):
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
    (tree / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(staging.StagingError, match="symlink or junction"):
        staging.stage(
            tmp_path,
            [config.StageEntry("pkg", "share/pkg/data", "resources/data")],
        )
    assert not (tmp_path / "resources" / "data").exists()


def test_even_an_in_prefix_directory_symlink_is_refused_not_followed(tmp_path):
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


@pytest.mark.parametrize("bad_source", ["/etc/passwd", "../../etc/passwd", "a/../../x"])
def test_non_prefix_relative_source_is_refused_before_any_copy(tmp_path, bad_source):
    _prefix(tmp_path)
    (tmp_path / "resources").mkdir()
    with pytest.raises(staging.StagingError, match="prefix-relative"):
        staging.stage(tmp_path, [config.StageEntry("pkg", bad_source, "resources/x")])
    assert not (tmp_path / "resources" / "x").exists()


def test_real_files_and_directories_still_stage_fine(tmp_path):
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
    prefix = _prefix(tmp_path)
    tree = prefix / "share" / "pkg" / "secret"
    _plant(tree / "f.txt", "x")
    tree.chmod(0o700)

    staging.stage(
        tmp_path, [config.StageEntry("pkg", "share/pkg/secret", "resources/secret")]
    )

    assert (tmp_path / "resources" / "secret").stat().st_mode & 0o777 == 0o700


def test_directory_tree_preserves_per_file_exec_bit(tmp_path):
    prefix = _prefix(tmp_path)
    tree = prefix / "share" / "pkg" / "tools"
    _plant(tree / "runme", "#!/bin/sh\n", mode=0o755)
    _plant(tree / "data.json", "{}", mode=0o644)

    staging.stage(
        tmp_path, [config.StageEntry("pkg", "share/pkg/tools", "resources/tools")]
    )

    assert (tmp_path / "resources" / "tools" / "runme").stat().st_mode & 0o111
    assert not (tmp_path / "resources" / "tools" / "data.json").stat().st_mode & 0o111


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "x/../..", "foo/bar"])
def test_path_shaped_feature_is_refused(tmp_path, bad):
    _prefix(tmp_path)
    with pytest.raises(staging.StagingError, match="valid feature name"):
        staging.stage(
            tmp_path,
            [config.StageEntry("lexd", "bin/lexd", "resources/lexd")],
            feature=bad,
        )


def test_feature_selects_the_named_feature_env_prefix(tmp_path):
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
    from shipit.install import artifactdeps

    assert artifactdeps.env_prefix(tmp_path, "lint") == (
        tmp_path / ".pixi" / "envs" / "shipit-artifacts-lint"
    )
    assert artifactdeps.env_prefix(tmp_path, None) == (
        tmp_path / ".pixi" / "envs" / "default"
    )
