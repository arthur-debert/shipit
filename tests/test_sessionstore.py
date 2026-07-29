"""The per-repo session store (ADR-0073): slug derivation, the plant ladder, adoption.

**Every test runs against a tmp ``home``.** Nothing here may read, move, or refuse
against the developer's real ``~/.claude`` — a test that did would BE the data-loss bug
this module exists to prevent. :func:`store_dir` / :func:`link_path` / :func:`plant` all
take a ``home`` override for exactly this reason, and the one test that pins the real
default (:func:`test_home_defaults_to_real_home`) asserts on a *path value* and touches
no filesystem.

The adoption matrix is a data-loss boundary, so it is tested cell by cell rather than
by a few happy paths: every (source, target) type pair the ADR defines has a test, and
the REFUSE cells assert that BOTH sides survive untouched.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from shipit import sessionstore
from shipit.identity import Owner, Repo

REPO = Repo(owner=Owner(login="arthur-debert"), name="shipit")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _store(home: Path) -> Path:
    return sessionstore.store_dir(REPO, home=home)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _assert_no_store_side_effects(home: Path) -> None:
    assert not _store(home).exists(), "a refusal created the store dir"
    assert not sessionstore.lock_path(REPO, home=home).exists(), (
        "a refusal created the lock file"
    )
    assert not (home / ".claude" / "stores").exists(), (
        "a refusal created the stores tree"
    )


def test_slug_replaces_separators_with_dashes():
    assert sessionstore.slug_for("/Users/adebert/h/shipit") == "-Users-adebert-h-shipit"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("has_underscore", "has-underscore"),
        ("has.dot", "has-dot"),
        ("plain-dash", "plain-dash"),
        ("with space", "with-space"),
        ("plus+at@sign", "plus-at-sign"),
    ],
)
def test_slug_replaces_every_non_alphanumeric(name, expected):
    """Not a separators-only denylist: EVERY non-alphanumeric maps to a dash.

    Verified against Claude Code 2.1.212 by probing real sessions in directories with
    each of these characters. A dash-preserving rule that missed ``_`` or ``.`` would
    plant the link at a name no session ever reads — a bug that looks exactly like
    doing nothing.

    The probe path is deliberately one that exists on NO platform: ``resolve()`` leaves
    a fully-nonexistent absolute path alone, so this pins the character rule and only
    the character rule. (``/tmp`` would drag in the macOS ``/private`` symlink and make
    the expectation OS-dependent — the resolve behaviour has its own test below.)
    """
    assert sessionstore.slug_for(f"/ws04probe/{name}") == f"-ws04probe-{expected}"


def test_slug_does_not_collapse_runs():
    assert sessionstore.slug_for("/ws04probe/claude-501/-Users-x") == (
        "-ws04probe-claude-501--Users-x"
    )


def test_slug_resolves_symlinks(tmp_path: Path):
    """The harness slugs the cwd's REAL path, so the slug must resolve first.

    The macOS ``/tmp`` → ``/private/tmp`` case is the one that bites: a session started
    in ``/tmp/x`` writes to ``-private-tmp-x``.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert sessionstore.slug_for(link) == sessionstore.slug_for(real)


def test_slug_is_pure(tmp_path: Path):
    assert sessionstore.slug_for(tmp_path / "nope") == sessionstore.slug_for(
        tmp_path / "nope"
    )


def test_store_is_keyed_on_repo_not_path(home: Path):
    assert _store(home) == home / ".claude" / "stores" / "arthur-debert" / "shipit"


def test_store_lives_outside_projects(home: Path):
    assert "projects" not in _store(home).parts


def test_link_path_is_projects_slug(home: Path):
    assert sessionstore.link_path("/Users/adebert/h/shipit", home=home) == (
        home / ".claude" / "projects" / "-Users-adebert-h-shipit"
    )


@pytest.mark.real_session_store_home
def test_home_defaults_to_real_home():
    assert sessionstore.store_dir(REPO) == (
        Path.home() / ".claude" / "stores" / "arthur-debert" / "shipit"
    )


def test_the_suite_guard_replaces_the_default_home():
    assert Path.home() not in sessionstore.store_dir(REPO).parents


def test_plant_creates_link_when_absent(home: Path, tmp_path: Path):
    result = sessionstore.plant(tmp_path, REPO, home=home)

    link = sessionstore.link_path(tmp_path, home=home)
    assert result.outcome == sessionstore.LINKED
    assert link.is_symlink()
    assert os.readlink(link) == str(_store(home))
    assert _store(home).is_dir()
    assert result.refusals == []


def test_plant_is_idempotent(home: Path, tmp_path: Path):
    first = sessionstore.plant(tmp_path, REPO, home=home)
    second = sessionstore.plant(tmp_path, REPO, home=home)

    assert first.outcome == sessionstore.LINKED
    assert second.outcome == sessionstore.NOOP
    assert second.refusals == []


def test_plant_refuses_foreign_symlink(home: Path, tmp_path: Path, caplog):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = sessionstore.link_path(tmp_path, home=home)
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)

    with caplog.at_level(logging.WARNING):
        result = sessionstore.plant(tmp_path, REPO, home=home)

    assert result.outcome == sessionstore.REFUSED
    assert os.readlink(link) == str(elsewhere), "the foreign link was retargeted"
    assert "refusing to retarget" in caplog.text
    _assert_no_store_side_effects(home)


def test_plant_refuses_a_file_at_the_slug_path(home: Path, tmp_path: Path, caplog):
    link = write(sessionstore.link_path(tmp_path, home=home), "not a store")

    with caplog.at_level(logging.WARNING):
        result = sessionstore.plant(tmp_path, REPO, home=home)

    assert result.outcome == sessionstore.REFUSED
    assert link.read_text() == "not a store"
    _assert_no_store_side_effects(home)


def test_plant_adopts_a_real_directory(home: Path, tmp_path: Path):
    link = sessionstore.link_path(tmp_path, home=home)
    write(link / "memory" / "provisioning-doctrine.md", "durable")
    write(link / "abc-123.jsonl", "transcript")

    result = sessionstore.plant(tmp_path, REPO, home=home)

    store = _store(home)
    assert result.outcome == sessionstore.ADOPTED
    assert result.refusals == []
    assert link.is_symlink() and os.readlink(link) == str(store)
    assert (store / "memory" / "provisioning-doctrine.md").read_text() == "durable"
    assert (store / "abc-123.jsonl").read_text() == "transcript"


def test_plant_keeps_the_dir_when_adoption_refuses(home: Path, tmp_path: Path, caplog):
    link = sessionstore.link_path(tmp_path, home=home)
    write(link / "memory", "a FILE where the store has a dir")
    (_store(home) / "memory").mkdir(parents=True)

    with caplog.at_level(logging.WARNING):
        result = sessionstore.plant(tmp_path, REPO, home=home)

    assert result.outcome == sessionstore.REFUSED
    assert not link.is_symlink()
    assert (link / "memory").read_text() == "a FILE where the store has a dir"
    assert "entr" in caplog.text and "remain" in caplog.text


def test_lock_sits_beside_the_store_never_inside_it(home: Path):
    lock = sessionstore.lock_path(REPO, home=home)

    assert lock.parent == _store(home).parent
    assert lock not in _store(home).parents


def test_lock_name_appends_rather_than_replaces_a_suffix(home: Path):
    io_repo = Repo(owner=Owner(login="acme"), name="docs.github.io")
    com_repo = Repo(owner=Owner(login="acme"), name="docs.github.com")

    assert sessionstore.lock_path(io_repo, home=home).name == "docs.github.io.lock"
    assert sessionstore.lock_path(io_repo, home=home) != sessionstore.lock_path(
        com_repo, home=home
    )


def _plant_concurrently(checkouts: list[Path], home: Path) -> list[Exception]:
    barrier = threading.Barrier(len(checkouts))
    errors: list[Exception] = []

    def run(checkout: Path) -> None:
        barrier.wait()
        try:
            sessionstore.plant(checkout, REPO, home=home)
        except Exception as exc:  # noqa: BLE001 — the test asserts on what escaped
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(c,)) for c in checkouts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a planter deadlocked on the lock"
    return errors


@pytest.fixture
def stale_classification(monkeypatch):
    original = sessionstore._settle
    barrier = threading.Barrier(2, timeout=30)
    first_call = threading.local()

    def settle_in_lockstep(link: Path, store: Path):
        is_first = not getattr(first_call, "done", False)
        first_call.done = True
        result = original(link, store)
        if is_first:
            barrier.wait()
        return result

    monkeypatch.setattr(sessionstore, "_settle", settle_in_lockstep)


@pytest.fixture
def slow_move(monkeypatch):
    original = sessionstore._move_file

    def delayed(src: Path, dst: Path) -> list[str]:
        time.sleep(0.05)
        return original(src, dst)

    monkeypatch.setattr(sessionstore, "_move_file", delayed)


def test_concurrent_adopters_never_lose_content(home: Path, tmp_path: Path, slow_move):
    """The data-loss race the lock exists for: two checkouts, one store, one dest path.

    Unserialized, both adopters classify `memory/MEMORY.md` as absent, both copy, and the
    second's bytes land on the first's — after which each verifies and unlinks its own
    source and one memory is gone for good. Serialized, the second adopter meets a store
    the first has already finished with, so the collision resolves through the matrix'
    keep-both rung and BOTH memories survive.
    """
    checkouts = [tmp_path / "tree-a", tmp_path / "tree-b"]
    for checkout, text in zip(
        checkouts, ("memory from A", "memory from B"), strict=True
    ):
        checkout.mkdir()
        write(
            sessionstore.link_path(checkout, home=home) / "memory" / "MEMORY.md", text
        )

    errors = _plant_concurrently(checkouts, home)

    assert not errors, f"planting raised under concurrency: {errors}"
    memories = sorted(
        p.read_text() for p in (_store(home) / "memory").iterdir() if p.is_file()
    )
    assert memories == ["memory from A", "memory from B"]
    for checkout in checkouts:
        link = sessionstore.link_path(checkout, home=home)
        assert link.is_symlink() and os.readlink(link) == str(_store(home))


def test_concurrent_adopters_dedupe_identical_content(
    home: Path, tmp_path: Path, slow_move
):
    checkouts = [tmp_path / "tree-a", tmp_path / "tree-b"]
    for checkout in checkouts:
        checkout.mkdir()
        write(
            sessionstore.link_path(checkout, home=home) / "memory" / "MEMORY.md",
            "one memory, copied to two checkouts",
        )

    errors = _plant_concurrently(checkouts, home)

    assert not errors, f"planting raised under concurrency: {errors}"
    memories = sorted(p.name for p in (_store(home) / "memory").iterdir())
    assert memories == ["MEMORY.md"]
    assert (_store(home) / "memory" / "MEMORY.md").read_text() == (
        "one memory, copied to two checkouts"
    )


def test_concurrent_planters_of_one_checkout_keep_the_store(
    home: Path, tmp_path: Path, stale_classification
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    write(
        sessionstore.link_path(checkout, home=home) / "memory" / "MEMORY.md",
        "the only copy",
    )

    errors = _plant_concurrently([checkout, checkout], home)

    assert not errors, f"planting raised under concurrency: {errors}"
    assert (_store(home) / "memory" / "MEMORY.md").read_text() == "the only copy"
    link = sessionstore.link_path(checkout, home=home)
    assert link.is_symlink() and os.readlink(link) == str(_store(home))


def test_adopt_file_into_absent(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    write(source / "note.md", "content")

    assert sessionstore.adopt(source, target) == []
    assert (target / "note.md").read_text() == "content"
    assert not (source / "note.md").exists(), "the source copy was not removed"


def test_adopt_file_identical_drops_duplicate(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "note.md", "same")
    write(target / "note.md", "same")

    assert sessionstore.adopt(source, target) == []
    assert (target / "note.md").read_text() == "same"
    assert not (source / "note.md").exists()


def test_adopt_file_differs_keeps_both(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "note.md", "from the ephemeral store")
    write(target / "note.md", "from the frozen store")

    assert sessionstore.adopt(source, target) == []
    assert (target / "note.md").read_text() == "from the frozen store", (
        "target clobbered"
    )
    assert (target / "note.adopted-1.md").read_text() == "from the ephemeral store"
    assert not (source / "note.md").exists()


def test_keep_both_preserves_the_extension(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "MEMORY.md", "a")
    write(target / "MEMORY.md", "b")

    sessionstore.adopt(source, target)

    assert (target / "MEMORY.adopted-1.md").exists()


def test_keep_both_never_collides(tmp_path: Path):
    target = tmp_path / "t"
    write(target / "note.md", "target")
    for n, text in enumerate(["first", "second"], start=1):
        source = tmp_path / f"s{n}"
        write(source / "note.md", text)
        sessionstore.adopt(source, target)

    assert (target / "note.adopted-1.md").read_text() == "first"
    assert (target / "note.adopted-2.md").read_text() == "second"


def test_memory_md_is_not_special_cased(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "MEMORY.md", "- a pointer\n")
    write(target / "MEMORY.md", "- another pointer\n")

    assert sessionstore.adopt(source, target) == []
    assert (target / "MEMORY.md").read_text() == "- another pointer\n"
    assert (target / "MEMORY.adopted-1.md").read_text() == "- a pointer\n"


def test_adopt_dir_into_absent(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    write(source / "memory" / "deep" / "note.md", "content")

    assert sessionstore.adopt(source, target) == []
    assert (target / "memory" / "deep" / "note.md").read_text() == "content"
    assert not (source / "memory").exists()


def test_adopt_dir_merges_recursively(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "memory" / "from-source.md", "s")
    write(target / "memory" / "from-target.md", "t")

    assert sessionstore.adopt(source, target) == []
    assert (target / "memory" / "from-source.md").read_text() == "s"
    assert (target / "memory" / "from-target.md").read_text() == "t"
    assert not (target / "memory.adopted-1").exists(), (
        "the tree was renamed, not merged"
    )
    assert not (source / "memory").exists()


def test_adopt_merges_deeply(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "a" / "b" / "c" / "deep.md", "s")
    write(target / "a" / "b" / "other.md", "t")

    assert sessionstore.adopt(source, target) == []
    assert (target / "a" / "b" / "c" / "deep.md").read_text() == "s"
    assert (target / "a" / "b" / "other.md").read_text() == "t"


def test_adopt_symlink_into_absent(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    source.mkdir()
    (source / "link").symlink_to("/some/where")

    assert sessionstore.adopt(source, target) == []
    assert (target / "link").is_symlink(), "the link became a copy"
    assert os.readlink(target / "link") == "/some/where"
    assert not (source / "link").is_symlink()


def test_adopt_symlink_same_text_drops_duplicate(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    source.mkdir()
    target.mkdir()
    (source / "link").symlink_to("/dangling/target")
    (target / "link").symlink_to("/dangling/target")

    assert sessionstore.adopt(source, target) == []
    assert os.readlink(target / "link") == "/dangling/target"
    assert not (source / "link").is_symlink()


def test_adopt_symlink_differs_keeps_both(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    source.mkdir()
    target.mkdir()
    (source / "link").symlink_to("/a")
    (target / "link").symlink_to("/b")

    assert sessionstore.adopt(source, target) == []
    assert os.readlink(target / "link") == "/b"
    assert os.readlink(target / "link.adopted-1") == "/a"


def test_adopt_never_follows_a_symlink(tmp_path: Path):
    outside = write(tmp_path / "outside" / "precious.md", "not the store's to move")
    source, target = tmp_path / "s", tmp_path / "t"
    source.mkdir()
    target.mkdir()
    (source / "link").symlink_to(outside)

    assert sessionstore.adopt(source, target) == []
    assert outside.read_text() == "not the store's to move", "content outside was moved"
    assert (target / "link").is_symlink()


@pytest.mark.parametrize(
    ("src_kind", "dst_kind"),
    [
        ("file", "dir"),
        ("file", "symlink"),
        ("dir", "file"),
        ("dir", "symlink"),
        ("symlink", "file"),
        ("symlink", "dir"),
    ],
)
def test_adopt_refuses_every_type_conflict(tmp_path: Path, src_kind, dst_kind, caplog):
    source, target = tmp_path / "s", tmp_path / "t"
    source.mkdir()
    target.mkdir()
    _make(source / "x", src_kind, "source")
    _make(target / "x", dst_kind, "target")

    with caplog.at_level(logging.WARNING):
        refusals = sessionstore.adopt(source, target)

    assert refusals == [str(source / "x")]
    assert "REFUSED" in caplog.text
    _assert_intact(source / "x", src_kind, "source")
    _assert_intact(target / "x", dst_kind, "target")


def test_a_refusal_does_not_stop_the_rest(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "conflict", "a file")
    write(source / "fine.md", "adopt me")
    (target / "conflict").mkdir(parents=True)

    refusals = sessionstore.adopt(source, target)

    assert refusals == [str(source / "conflict")]
    assert (target / "fine.md").read_text() == "adopt me"
    assert (source / "conflict").read_text() == "a file"


def test_refused_content_is_never_deleted_with_its_dir(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    write(source / "memory" / "conflict", "irreplaceable")
    write(source / "memory" / "ok.md", "moved")
    (target / "memory" / "conflict").mkdir(parents=True)

    refusals = sessionstore.adopt(source, target)

    assert refusals == [str(source / "memory" / "conflict")]
    assert (source / "memory").is_dir(), "the source dir was removed with content in it"
    assert (source / "memory" / "conflict").read_text() == "irreplaceable"
    assert (target / "memory" / "ok.md").read_text() == "moved"


def test_adopt_file_never_clobbers_a_destination_that_appeared(
    tmp_path: Path, monkeypatch
):
    """A live harness write into the store between classify and publish MUST survive.

    The per-store lock serializes shipit adopters, but live Claude sessions write the same
    store (ADR-0073) holding no lock. ``_adopt_entry`` classifies ``note.md`` absent and
    calls ``_move_file``; the bug was that a session creating ``note.md`` in that window got
    overwritten by ``copy2``, and on a verify mismatch outright ``unlink``ed. The fix stages
    the copy and publishes with ``os.link``'s ``EEXIST`` no-clobber semantics, so an appeared
    destination is a keep-both collision — BOTH memories survive and nothing is unlinked.

    The race is modelled by wrapping ``_move_file`` (the seam present before AND after the
    fix) to create ``dst`` just before it runs — i.e. after ``_adopt_entry`` classified it
    absent. Without the fix this test fails: ``note.md`` ends up holding the ADOPTED bytes,
    the live content gone and no ``note.adopted-1.md`` minted.
    """
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    write(source / "note.md", "adopted content")

    original_move = sessionstore._move_file

    def racing_move(src: Path, dst: Path) -> list[str]:
        write(dst, "live session content")
        return original_move(src, dst)

    monkeypatch.setattr(sessionstore, "_move_file", racing_move)

    assert sessionstore.adopt(source, target) == []
    assert (target / "note.md").read_text() == "live session content", (
        "the live destination was clobbered"
    )
    assert (target / "note.adopted-1.md").read_text() == "adopted content", (
        "the adopted content was not kept beside the live file"
    )
    assert not (source / "note.md").exists(), "the adopted source was not drained"


def test_adopt_symlink_never_clobbers_a_destination_that_appeared(
    tmp_path: Path, monkeypatch
):
    source, target = tmp_path / "s", tmp_path / "t"
    source.mkdir()
    target.mkdir()
    (source / "link").symlink_to("/adopted/target")

    original_move = sessionstore._move_symlink

    def racing_move(src: Path, dst: Path) -> list[str]:
        dst.symlink_to("/live/target")
        return original_move(src, dst)

    monkeypatch.setattr(sessionstore, "_move_symlink", racing_move)

    assert sessionstore.adopt(source, target) == []
    assert os.readlink(target / "link") == "/live/target", "the live link was clobbered"
    assert os.readlink(target / "link.adopted-1") == "/adopted/target"
    assert not (source / "link").is_symlink(), "the adopted source was not drained"


def test_adopt_symlink_survives_a_post_publish_swap(tmp_path: Path, monkeypatch):
    """A successful ``os.symlink`` is its own proof — no read-back may unlink the pathname.

    ``os.symlink`` atomically publishes exactly the adopted text or raises, so success
    needs no verification. The danger is the window AFTER that publish: if the lock-less
    harness replaces ``target`` with its OWN link, a read-back verify would see a mismatch
    and unlink ``target`` — deleting the harness's link, the no-clobber violation this
    forbids. The round-1 test (:func:`test_adopt_symlink_never_clobbers_a_destination_that_appeared`)
    only races BEFORE :func:`_move_symlink`, so it misses this post-publication window;
    here the harness republishes its own link right after the store's ``symlink_to``
    succeeds, and the harness link must survive untouched.
    """
    source, target = tmp_path / "s", tmp_path / "t"
    source.mkdir()
    target.mkdir()
    (source / "link").symlink_to("/adopted/target")

    real_symlink_to = Path.symlink_to

    def swapping_symlink_to(self, link_target, *args, **kwargs):
        real_symlink_to(self, link_target, *args, **kwargs)
        if link_target == "/adopted/target":
            os.unlink(self)
            os.symlink("/harness/target", self)

    monkeypatch.setattr(Path, "symlink_to", swapping_symlink_to)

    assert sessionstore.adopt(source, target) == []
    assert os.readlink(target / "link") == "/harness/target", (
        "the harness link was clobbered"
    )
    assert not (source / "link").is_symlink(), "the adopted source was not drained"


def test_adopt_is_generic(tmp_path: Path):
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    for n in range(7):
        write(source / f"dir{n}" / f"file{n}.txt", f"content {n}")

    assert sessionstore.adopt(source, target) == []
    for n in range(7):
        assert (target / f"dir{n}" / f"file{n}.txt").read_text() == f"content {n}"


def _make(path: Path, kind: str, text: str) -> None:
    if kind == "file":
        write(path, text)
    elif kind == "dir":
        write(path / "child.md", text)
    elif kind == "symlink":
        path.symlink_to(f"/{text}")


def _assert_intact(path: Path, kind: str, text: str) -> None:
    if kind == "file":
        assert path.is_file() and path.read_text() == text
    elif kind == "dir":
        assert path.is_dir() and (path / "child.md").read_text() == text
    elif kind == "symlink":
        assert path.is_symlink() and os.readlink(path) == f"/{text}"
