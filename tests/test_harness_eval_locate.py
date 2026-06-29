"""Run locator: a terminal-hook payload -> the right transcript + meta paths.

The load-bearing case is the coordinator (session transcript, NO meta sidecar) vs
subagent (`agent-<id>.jsonl` with a sibling `agent-<id>.meta.json`) split — the
locator reads it off the transcript filename, so the eval record can attribute the
run to its role.
"""

from __future__ import annotations

from shipit.harness.eval.locate import RunFiles, locate_run


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_coordinator_run_resolves_session_transcript_with_no_meta(tmp_path):
    # The coordinator run is the top-level session transcript; it has no sidecar.
    transcript = _write(tmp_path / "57d92339-f3c3-45e8.jsonl")
    run = locate_run({"transcript_path": str(transcript)})
    assert run == RunFiles(transcript=transcript, meta=None)


def test_subagent_run_resolves_agent_transcript_and_its_meta(tmp_path):
    # A subagent run is `…/subagents/agent-<id>.jsonl`, co-located with its meta.
    subdir = tmp_path / "session" / "subagents"
    transcript = _write(subdir / "agent-a7c77e10.jsonl")
    meta = _write(subdir / "agent-a7c77e10.meta.json", '{"agentType":"implementer"}')
    run = locate_run({"transcript_path": str(transcript)})
    assert run == RunFiles(transcript=transcript, meta=meta)


def test_subagent_without_meta_sidecar_degrades_to_no_meta(tmp_path):
    # A subagent transcript whose meta is missing must not return a dangling path —
    # it degrades to meta=None (a coordinator-shaped record) rather than crash.
    transcript = _write(tmp_path / "subagents" / "agent-deadbeef.jsonl")
    run = locate_run({"transcript_path": str(transcript)})
    assert run is not None
    assert run.transcript == transcript
    assert run.meta is None


def test_missing_transcript_path_returns_none():
    # No transcript named → nothing to evaluate; the boundary fails open on None.
    assert locate_run({}) is None
    assert locate_run({"transcript_path": ""}) is None
