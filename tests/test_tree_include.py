"""Unit tests for ``tree.include`` — the ``.treeinclude`` matching truth table.

Asserts EXTERNAL behavior: given ``.treeinclude`` text (and, for ``resolve``, a
real on-disk source tree), the module returns the right set of paths — globs,
negations, and repo-root anchoring — never "it called git". Matching is pure, so
the resolved list IS the contract.
"""

from __future__ import annotations

import os
from pathlib import Path

from shipit.tree import include
from shipit.tree.include import parse


# --------------------------------------------------------------------------
# parse().match — the pure pattern semantics
# --------------------------------------------------------------------------


def _matches(text: str, path: str) -> bool:
    return parse(text).match(path)


def test_literal_floating_pattern_matches_at_any_depth():
    # A bare name (no slash) floats: it matches at the root AND nested.
    spec = parse(".env")
    assert spec.match(".env")
    assert spec.match("services/api/.env")


def test_leading_slash_anchors_to_repo_root():
    spec = parse("/.env")
    assert spec.match(".env")
    assert not spec.match("services/api/.env")


def test_internal_slash_anchors_to_repo_root():
    spec = parse("config/secrets.yaml")
    assert spec.match("config/secrets.yaml")
    assert not spec.match("app/config/secrets.yaml")


def test_star_glob_stays_within_a_path_segment():
    spec = parse("*.env")
    assert spec.match(".env")  # "*" matches an empty-ish prefix too
    assert spec.match("prod.env")
    assert spec.match("deep/dir/prod.env")  # floats
    assert not spec.match("prod.env.bak")


def test_question_mark_matches_a_single_non_slash_char():
    spec = parse("/key?.pem")
    assert spec.match("key1.pem")
    assert not spec.match("key.pem")
    assert not spec.match("key12.pem")


def test_character_class_and_negated_class():
    assert _matches("/file[0-9].bin", "file3.bin")
    assert not _matches("/file[0-9].bin", "fileX.bin")
    assert _matches("/file[!0-9].bin", "fileX.bin")
    assert not _matches("/file[!0-9].bin", "file3.bin")


def test_trailing_slash_includes_a_directory_subtree_only():
    spec = parse("models/")
    # A file UNDER the directory is included...
    assert spec.match("models/saml.bin")
    assert spec.match("a/models/nested/deep.bin")  # floats to any depth
    # ...but a regular FILE literally named "models" is not (dir-only).
    assert not spec.match("models")


def test_directory_pattern_without_trailing_slash_carries_its_subtree():
    # An anchored name with no trailing slash matches the path itself OR anything
    # under it (a matched directory carries its whole subtree).
    spec = parse("/vendor")
    assert spec.match("vendor")
    assert spec.match("vendor/lib/thing.so")
    assert not spec.match("app/vendor/thing.so")  # anchored to root


def test_double_star_matches_across_segments():
    spec = parse("config/**/secret.key")
    assert spec.match("config/secret.key")
    assert spec.match("config/a/secret.key")
    assert spec.match("config/a/b/c/secret.key")
    assert not spec.match("other/a/secret.key")


def test_leading_double_star_floats_an_anchored_path():
    spec = parse("**/node-secrets/token")
    assert spec.match("node-secrets/token")
    assert spec.match("packages/web/node-secrets/token")


def test_last_matching_rule_wins_negation_excludes():
    # Include every ".env", then carve out the example one.
    text = "*.env\n!example.env\n"
    spec = parse(text)
    assert spec.match("prod.env")
    assert not spec.match("example.env")


def test_negation_then_reinclude_follows_file_order():
    text = "secrets/\n!secrets/public/\nsecrets/public/override.key\n"
    spec = parse(text)
    assert spec.match("secrets/private.key")
    assert not spec.match("secrets/public/readme.txt")
    assert spec.match("secrets/public/override.key")  # last rule re-includes


def test_comments_blank_lines_and_escaped_specials_are_handled():
    text = "# a comment\n\n   \n\\#literal-hash\n\\!literal-bang\n"
    spec = parse(text)
    assert spec.match("#literal-hash")
    assert spec.match("!literal-bang")
    # The comment / blank lines produced no rules of their own.
    assert not spec.match("a comment")


def test_empty_or_negation_only_spec_includes_nothing():
    assert parse("").is_empty()
    assert parse("# only a comment\n").is_empty()
    assert parse("!nope\n").is_empty()


# --------------------------------------------------------------------------
# resolve() — match against a real source tree, with directory pruning
# --------------------------------------------------------------------------


def _write(root: Path, rel: str, body: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_resolve_returns_no_treeinclude_as_empty(tmp_path: Path):
    assert include.resolve(tmp_path) == []


def test_resolve_selects_globs_and_directories_and_honors_negation(tmp_path: Path):
    _write(tmp_path, ".treeinclude", "*.env\n!example.env\nmodels/\n/.doppler.yaml\n")
    _write(tmp_path, ".env")
    _write(tmp_path, "example.env")
    _write(tmp_path, "services/api/prod.env")
    _write(tmp_path, "models/saml.bin")
    _write(tmp_path, "models/sub/extra.bin")
    _write(tmp_path, ".doppler.yaml")
    _write(tmp_path, "README.md")  # tracked-but-not-selected

    assert include.resolve(tmp_path) == [
        ".doppler.yaml",
        ".env",
        "models/saml.bin",
        "models/sub/extra.bin",
        "services/api/prod.env",
    ]


def test_resolve_prunes_git_and_unmatched_directories(tmp_path: Path, monkeypatch):
    # The patterns are all anchored under config/, so the walk must never descend
    # into a giant unrelated tree like node_modules/.
    _write(tmp_path, ".treeinclude", "/config/**/secret.key\n")
    _write(tmp_path, "config/a/secret.key")
    _write(tmp_path, ".git/objects/pack/whatever")
    _write(tmp_path, "node_modules/pkg/secret.key")  # NOT under config/

    # Trip a failure if the walk ever steps into node_modules. ``os.walk`` lists
    # each directory via ``os.scandir`` (NOT ``os.listdir``), so the guard has to
    # wrap ``scandir`` to actually observe a descent into a pruned directory.
    real_scandir = os.scandir

    def guarded_scandir(path):
        assert "node_modules" not in str(path), "walked a pruned directory"
        return real_scandir(path)

    monkeypatch.setattr("os.scandir", guarded_scandir)

    assert include.resolve(tmp_path) == ["config/a/secret.key"]


def test_apply_copies_selected_files_into_dest(tmp_path: Path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(src, ".treeinclude", ".env\nmodels/\n")
    _write(src, ".env", "TOKEN=1")
    _write(src, "models/saml.bin", "BIN")
    _write(src, "ignored.txt", "no")

    written = include.apply(src, dest)

    assert (dest / ".env").read_text() == "TOKEN=1"
    assert (dest / "models" / "saml.bin").read_text() == "BIN"
    assert not (dest / "ignored.txt").exists()
    assert {p.relative_to(dest).as_posix() for p in written} == {
        ".env",
        "models/saml.bin",
    }


def test_apply_never_clobbers_an_existing_dest_file(tmp_path: Path):
    # A selected path that already exists at dest came from the fresh checkout of
    # the base (a tracked file): that version is authoritative and must NOT be
    # overwritten by a stale/dirty copy from the source checkout.
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(src, ".treeinclude", ".env\nmodels/saml.bin\n")
    _write(src, ".env", "STALE")
    _write(src, "models/saml.bin", "FRESH-FROM-SRC")
    _write(dest, "models/saml.bin", "CHECKED-OUT")  # already present at dest

    written = include.apply(src, dest)

    assert (dest / "models" / "saml.bin").read_text() == "CHECKED-OUT"
    assert (dest / ".env").read_text() == "STALE"  # genuinely missing file is filled
    assert {p.relative_to(dest).as_posix() for p in written} == {".env"}
