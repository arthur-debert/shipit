from __future__ import annotations

import json

import pytest

from shipit.review import artifacts


def test_round_root_lives_beside_the_round_store(tmp_path):
    root = artifacts.round_root("Owner/Repo", "r" * 32, base_dir=tmp_path)
    assert root == tmp_path / "review-artifacts" / "owner" / "repo" / ("r" * 32)


@pytest.mark.parametrize("slug", [None, "", "  ", "not-a-slug"])
def test_round_root_fails_open_on_an_unusable_slug(tmp_path, slug, caplog):
    caplog.set_level("WARNING", logger="shipit.review")
    assert artifacts.round_root(slug, "rid", base_dir=tmp_path) is None
    assert any("bundle disabled" in r.message for r in caplog.records)


def test_bundle_writes_prompt_streams_and_accreting_meta(tmp_path):
    run_dir = tmp_path / "round" / "run1"
    bundle = artifacts.RunArtifacts(run_dir)

    bundle.write_prompt("the exact task text")
    bundle.record(run_id="run1", backend="codex")
    bundle.write_streams("raw out", "raw err")
    bundle.record(exit_code=0, duration_ms=1234, timed_out=False)

    assert (run_dir / artifacts.PROMPT_FILENAME).read_text() == "the exact task text"
    assert (run_dir / artifacts.STDOUT_FILENAME).read_text() == "raw out"
    assert (run_dir / artifacts.STDERR_FILENAME).read_text() == "raw err"
    meta = json.loads((run_dir / artifacts.META_FILENAME).read_text())
    assert meta == {
        "run_id": "run1",
        "backend": "codex",
        "exit_code": 0,
        "duration_ms": 1234,
        "timed_out": False,
    }


def test_streams_tolerate_none(tmp_path):
    bundle = artifacts.RunArtifacts(tmp_path / "run")
    bundle.write_streams(None, None)
    assert (tmp_path / "run" / artifacts.STDOUT_FILENAME).read_text() == ""
    assert (tmp_path / "run" / artifacts.STDERR_FILENAME).read_text() == ""


def test_meta_degrades_unserializable_values_to_repr(tmp_path):
    bundle = artifacts.RunArtifacts(tmp_path / "run")
    bundle.record(error=ValueError("boom"))
    meta = json.loads((tmp_path / "run" / artifacts.META_FILENAME).read_text())
    assert "boom" in meta["error"]


def test_streams_over_the_cap_are_truncated_with_a_marker_and_flagged_in_meta(tmp_path):
    bundle = artifacts.RunArtifacts(tmp_path / "run")
    huge = "z" * (artifacts.MAX_STREAM_CHARS + 100)
    bundle.write_streams(huge, "short err")

    out = (tmp_path / "run" / artifacts.STDOUT_FILENAME).read_text()
    assert len(out) <= artifacts.MAX_STREAM_CHARS
    assert out.startswith("z" * 1000)
    assert out.endswith("…\n") and "truncated" in out
    assert (tmp_path / "run" / artifacts.STDERR_FILENAME).read_text() == "short err"
    meta = json.loads((tmp_path / "run" / artifacts.META_FILENAME).read_text())
    assert meta["stdout_truncated"] is True
    assert meta["stderr_truncated"] is False


def test_meta_rewrite_is_atomic_and_leaves_no_temp_file(tmp_path):
    run_dir = tmp_path / "run"
    bundle = artifacts.RunArtifacts(run_dir)
    bundle.record(a=1)
    bundle.record(b=2)
    assert json.loads((run_dir / artifacts.META_FILENAME).read_text()) == {
        "a": 1,
        "b": 2,
    }
    assert not list(run_dir.glob("*.tmp"))


def test_disabled_sink_noops_every_write(tmp_path):
    bundle = artifacts.RunArtifacts.disabled()
    bundle.write_prompt("p")
    bundle.write_streams("o", "e")
    bundle.record(k="v")
    assert bundle.dir is None
    assert list(tmp_path.iterdir()) == []


def test_under_composes_the_run_dir_or_stays_disabled(tmp_path):
    assert artifacts.RunArtifacts.under(tmp_path, "abc").dir == tmp_path / "abc"
    assert artifacts.RunArtifacts.under(None, "abc").dir is None


def test_writes_are_fail_open_on_an_unwritable_root(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory must go")
    bundle = artifacts.RunArtifacts(blocker / "run")
    caplog.set_level("WARNING", logger="shipit.review")
    bundle.write_prompt("p")
    bundle.record(k="v")
    assert any("artifact write failed" in r.message for r in caplog.records)
