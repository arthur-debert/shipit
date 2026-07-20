"""The shared refuse-links primitives (:mod:`shipit.fspath`) — the one answer
both declared-path surfaces give (staging's copy, the bundle's tar operands).

The policy tests live with each surface (they assert its own refusal message);
these pin the PRIMITIVE's contract, which is what keeps the two from drifting.
"""

from shipit.fspath import first_link_component, is_link


def test_a_real_chain_has_no_link_component(tmp_path):
    (tmp_path / "src/tree_sitter").mkdir(parents=True)
    (tmp_path / "src/tree_sitter/parser.h").write_text("/* h */")
    assert first_link_component(tmp_path, ("src", "tree_sitter", "parser.h")) is None


def test_a_redirect_is_reported_at_the_component_that_redirects(tmp_path):
    # Mid-chain, not at the leaf: `leak/passwd` is refused AT `leak`, which is
    # the whole point of walking one component at a time.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("HOST")
    base = tmp_path / "leg"
    base.mkdir()
    (base / "leak").symlink_to(outside, target_is_directory=True)

    offender = first_link_component(base, ("leak", "passwd"))

    assert offender == base / "leak"


def test_a_missing_path_is_not_a_link(tmp_path):
    assert first_link_component(tmp_path, ("nope", "still-nope")) is None
    assert not is_link(tmp_path / "nope")


def test_the_base_itself_is_never_inspected(tmp_path):
    # The base is the caller's own anchor, not a declared value — and on darwin
    # it routinely sits under a symlinked ancestor (`/tmp -> /private/tmp`).
    real = tmp_path / "real"
    real.mkdir()
    (real / "src").mkdir()
    linked_base = tmp_path / "linked"
    linked_base.symlink_to(real, target_is_directory=True)

    assert is_link(linked_base)  # the base IS a link…
    assert first_link_component(linked_base, ("src",)) is None  # …and is not walked


def test_a_leaf_symlink_is_reported(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    base = tmp_path / "leg"
    base.mkdir()
    (base / "queries").symlink_to(target, target_is_directory=True)
    assert first_link_component(base, ("queries",)) == base / "queries"
    assert is_link(base / "queries")


def test_a_real_file_and_a_real_dir_are_not_links(tmp_path):
    (tmp_path / "dir").mkdir()
    (tmp_path / "file").write_text("x")
    assert not is_link(tmp_path / "dir")
    assert not is_link(tmp_path / "file")
    assert first_link_component(tmp_path, ()) is None  # an empty chain is real
