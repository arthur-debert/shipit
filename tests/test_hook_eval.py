"""Eval hook boundary: a terminal-hook payload -> one eval record on disk.

One thin end-to-end thread per the PRD's "payload → record on disk" — plus the
fail-open contract (eval must never break a session: any bad input is a silent
no-op, exit 0, nothing written).
"""

from __future__ import annotations

import io
import json

import pytest
from shipit import gh
from shipit.harness.eval import store
from shipit.verbs.hook.eval import run


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    """Redirect the eval store to a tmp state dir (never the real platformdirs one)."""
    base = tmp_path / "state"
    monkeypatch.setattr(store.platformdirs, "user_state_dir", lambda *a, **k: str(base))
    return base


def _records(state_dir):
    files = list((state_dir / "eval").glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def _write_transcript(path, *tool_names):
    blocks = [
        {"type": "tool_use", "id": f"toolu_{n}", "name": n, "input": {}}
        for n in tool_names
    ]
    line = json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": blocks}}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line + "\n", encoding="utf-8")


def test_subagent_stop_writes_a_record_with_role_and_metric(state_dir, tmp_path):
    sub = tmp_path / "session" / "subagents"
    transcript = sub / "agent-abc123.jsonl"
    _write_transcript(transcript, "Read", "Bash", "Edit")
    (sub / "agent-abc123.meta.json").write_text(
        json.dumps({"agentType": "implementer", "spawnMode": "bypassPermissions"}),
        encoding="utf-8",
    )
    payload = {"transcript_path": str(transcript), "cwd": str(tmp_path)}

    assert run(stdin=io.StringIO(json.dumps(payload))) == 0

    records = _records(state_dir)
    assert len(records) == 1
    rec = records[0]
    assert rec["gen_ai.agent.name"] == "implementer"
    assert rec["eval.tool_call_count"] == 3
    assert rec["eval.tool_call_vector"] == {"Read": 1, "Bash": 1, "Edit": 1}
    # WS03: the record now carries the implementer role-prompt content-hash.
    assert rec["eval.variant"]["content_hash"].startswith("sha256:")
    assert rec["eval.variant"]["label"] is None
    assert "git.commit" in rec
    # A subagent run carries no exit-hygiene block (that check is coordinator-only).
    assert rec["eval.exit_hygiene.worktree_clean"] is None


def test_stop_writes_a_coordinator_record(state_dir, tmp_path):
    transcript = tmp_path / "57d92339.jsonl"
    _write_transcript(transcript, "Read")
    payload = {"transcript_path": str(transcript), "cwd": str(tmp_path)}

    assert run(stdin=io.StringIO(json.dumps(payload))) == 0

    records = _records(state_dir)
    assert len(records) == 1
    assert records[0]["gen_ai.agent.name"] == "coordinator"
    assert records[0]["eval.tool_call_count"] == 1
    # The coordinator run is stamped with the coordinator role-prompt hash.
    assert records[0]["eval.variant"]["content_hash"].startswith("sha256:")


def test_stop_record_carries_coordinator_exit_hygiene(state_dir, tmp_path, monkeypatch):
    # The coordinator run runs the one live check; a clean porcelain → worktree_clean.
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    transcript = tmp_path / "57d92339.jsonl"
    _write_transcript(transcript, "Read")
    payload = {"transcript_path": str(transcript), "cwd": str(tmp_path)}

    assert run(stdin=io.StringIO(json.dumps(payload))) == 0

    rec = _records(state_dir)[0]
    assert rec["eval.exit_hygiene.worktree_clean"] is True
    assert rec["eval.exit_hygiene.dirty_file_count"] == 0
    assert rec["eval.exit_hygiene.stray_pid_count"] == 0


@pytest.mark.parametrize(
    "garbage", ["", "not json", "{", "[]", json.dumps({"no": "transcript"})]
)
def test_fails_open_writing_nothing_on_bad_input(state_dir, garbage):
    # Malformed / empty / transcript-less input → exit 0, no record, no crash.
    assert run(stdin=io.StringIO(garbage)) == 0
    assert _records(state_dir) == []
