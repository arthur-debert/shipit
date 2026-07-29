from shipit import staging
from shipit.verbs import stage as stage_verb


def _plant_prefix_tool(root, package="lexd-lsp", binary="lexd-lsp"):
    binpath = root / ".pixi" / "envs" / "default" / "bin" / binary
    binpath.parent.mkdir(parents=True, exist_ok=True)
    binpath.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    binpath.chmod(0o755)
    return binpath


def test_run_stages_declared_map_and_returns_zero(tmp_path):
    _plant_prefix_tool(tmp_path)
    (tmp_path / ".shipit.toml").write_text(
        '[stage.lexd-lsp]\n"bin/lexd-lsp" = "resources/lexd-lsp"\n', encoding="utf-8"
    )

    rc = stage_verb.run(str(tmp_path))

    assert rc == 0
    dest = tmp_path / "resources" / "lexd-lsp"
    assert dest.is_file() and dest.stat().st_mode & 0o111


def test_run_with_no_stage_map_is_a_clean_no_op(tmp_path):
    (tmp_path / ".shipit.toml").write_text("[shipit]\n", encoding="utf-8")
    assert stage_verb.run(str(tmp_path)) == 0


def test_run_with_no_config_is_a_clean_no_op(tmp_path):
    assert stage_verb.run(str(tmp_path)) == 0


def test_run_missing_source_maps_to_exit_one(tmp_path, capsys):
    (tmp_path / ".pixi" / "envs" / "default").mkdir(parents=True)
    (tmp_path / ".shipit.toml").write_text(
        '[stage.lexd-lsp]\n"bin/lexd-lsp" = "resources/lexd-lsp"\n', encoding="utf-8"
    )

    rc = stage_verb.run(str(tmp_path))

    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_run_malformed_config_maps_to_exit_one(tmp_path, capsys):
    (tmp_path / ".shipit.toml").write_text(
        '[stage.LexdLsp]\n"bin/x" = "resources/x"\n', encoding="utf-8"
    )
    rc = stage_verb.run(str(tmp_path))
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_run_path_shaped_feature_maps_to_exit_one(tmp_path, capsys):
    _plant_prefix_tool(tmp_path)
    (tmp_path / ".shipit.toml").write_text(
        '[stage.lexd-lsp]\n"bin/lexd-lsp" = "resources/lexd-lsp"\n', encoding="utf-8"
    )

    rc = stage_verb.run(str(tmp_path), feature="../../etc")

    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_format_staged_empty_says_nothing_to_stage():
    assert "nothing to stage" in stage_verb.format_staged([])


def test_format_staged_lists_each_item_with_kind_and_exec_note():
    text = stage_verb.format_staged(
        [
            staging.StagedFile(
                "lexd-lsp", "bin/lexd-lsp", "resources/lexd-lsp", False, True
            ),
            staging.StagedFile(
                "tsx", "share/tsx/queries", "resources/queries", True, False
            ),
        ]
    )
    assert "file bin/lexd-lsp -> resources/lexd-lsp (executable)" in text
    assert "dir  share/tsx/queries -> resources/queries" in text
    assert "resources/queries (executable)" not in text
