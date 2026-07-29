from __future__ import annotations

from shipit.harness.eval.locate import RunFiles, locate_run


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_coordinator_run_resolves_session_transcript_with_no_meta(tmp_path):
    transcript = _write(tmp_path / "57d92339-f3c3-45e8.jsonl")
    run = locate_run({"transcript_path": str(transcript)})
    assert run == RunFiles(transcript=transcript, meta=None)
    assert run.is_coordinator is True


def test_subagent_run_resolves_agent_transcript_and_its_meta(tmp_path):
    subdir = tmp_path / "session" / "subagents"
    transcript = _write(subdir / "agent-a7c77e10.jsonl")
    meta = _write(subdir / "agent-a7c77e10.meta.json", '{"agentType":"implementer"}')
    run = locate_run({"transcript_path": str(transcript)})
    assert run == RunFiles(transcript=transcript, meta=meta)


def test_subagent_without_meta_sidecar_degrades_to_no_meta(tmp_path):
    transcript = _write(tmp_path / "subagents" / "agent-deadbeef.jsonl")
    run = locate_run({"transcript_path": str(transcript)})
    assert run is not None
    assert run.transcript == transcript
    assert run.meta is None
    assert run.is_coordinator is False


def test_run_id_is_the_transcript_stem_for_both_run_kinds(tmp_path):
    session = _write(tmp_path / "57d92339-f3c3-45e8.jsonl")
    agent = _write(tmp_path / "subagents" / "agent-a7c77e10.jsonl")
    assert locate_run({"transcript_path": str(session)}).run_id == "57d92339-f3c3-45e8"
    assert locate_run({"transcript_path": str(agent)}).run_id == "agent-a7c77e10"


def test_missing_transcript_path_returns_none():
    assert locate_run({}) is None
    assert locate_run({"transcript_path": ""}) is None


def test_named_but_nonexistent_transcript_returns_none(tmp_path):
    ghost = tmp_path / "session" / "gone.jsonl"
    assert locate_run({"transcript_path": str(ghost)}) is None
