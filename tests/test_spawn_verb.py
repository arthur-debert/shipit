"""Wiring smoke tests for the ``shipit spawn`` verb layer (ADR-0030, CLI02-WS02).

The THIN layer over the promoted pipeline: the domain behavior (every stage,
every refusal) is covered typed-in/typed-out in ``test_spawn_subagent.py``;
these tests prove only the click binding, the byte-stable agent-parsed SPAWNED
render, and the exit contract — a completed spawn exits 0 with the sentinel
block, a pipeline refusal reaches the shared error shell as one clean
``error: …`` stderr line + exit 1 (never a traceback, never a SPAWNED block),
and a malformed OPTION (unknown backend) is click's usage error, exit 2.
"""

from __future__ import annotations

import json
from dataclasses import replace

from click.testing import CliRunner

from shipit import gh
from shipit.verbs import spawn as spawn_verb

# The typed suite's boundary fakes are reused wholesale: the smoke layer needs
# the same fake edges, not a second copy of them.
from test_spawn_subagent import _PR, bounds


def test_spawn_subagent_help_documents_the_verb():
    result = CliRunner().invoke(spawn_verb.spawn, ["subagent", "--help"])

    assert result.exit_code == 0
    for token in (
        "--repo",
        "--epic",
        "--ws",
        "--issue",
        "--session",
        "--role",
        "--backend",
    ):
        assert token in result.output
    assert "Tree" in result.output


def test_run_renders_the_byte_stable_spawned_block(tmp_path, capsys):
    """argv → exit code round trip on the write path: exit 0 and the frozen
    agent-parsed surface — the SPAWNED sentinel line + the indented-JSON payload,
    byte-identical to the pre-promotion output."""
    b, _calls = bounds(tmp_path)

    rc = spawn_verb.run(
        repo="widget", role="implementer", epic="TRE03", ws=1, issue=156, bounds=b
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload == {
        "tree": str(tmp_path / "tree"),
        "branch": "TRE03/WS01",
        "base": "origin/TRE03/umbrella",
        "role": "implementer",
        "backend": "claude",
        "pr": 321,
        "pr_state": "OPEN",
        "pr_is_draft": True,
    }
    # Byte-stable: exactly the old print("SPAWNED") + print(json.dumps(indent=2)).
    assert out == "SPAWNED\n" + json.dumps(payload, indent=2) + "\n"


def test_reviewer_spawned_block_omits_the_pr_linkage(tmp_path, capsys):
    """A reviewer reports through the EXISTING PR: its SPAWNED payload carries
    the coordinates only — no pr/pr_state/pr_is_draft keys."""
    b, _calls = bounds(tmp_path)

    rc = spawn_verb.run(repo="widget", role="reviewer", epic="TRE03", ws=3, bounds=b)

    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["role"] == "reviewer"
    assert "pr" not in payload and "sentinel" not in payload


def test_a_pipeline_refusal_maps_to_the_error_shell(tmp_path, capsys):
    """A refusal (here: the handshake audit — a non-draft PR) reaches the shared
    shell: exit 1, ONE `error: …` stderr line, no SPAWNED block, no traceback."""
    b, _calls = bounds(tmp_path, pr=replace(_PR, is_draft=False))

    rc = spawn_verb.run(
        repo="widget", role="implementer", epic="TRE03", ws=1, issue=156, bounds=b
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert "SPAWNED" not in captured.out
    assert captured.err.startswith("error: ")
    assert "is not a draft" in captured.err
    assert captured.err == "".join(captured.err.splitlines()) + "\n"  # one line


def test_cli_reviewer_spawn_without_issue_is_not_a_usage_error(tmp_path, monkeypatch):
    """--issue stays optional at the click layer: a reviewer spawn (which needs
    no issue) must reach the pipeline, never be rejected at parse (exit 2). The
    pipeline is faked at the verb's seam; the spec it receives is asserted."""
    seen: dict = {}

    def fake_pipeline(spec, bounds=None):
        seen["spec"] = spec
        raise spawn_verb.subagent.SpawnError("stop here")

    monkeypatch.setattr(spawn_verb.subagent, "spawn_subagent", fake_pipeline)

    result = CliRunner().invoke(
        spawn_verb.spawn,
        [
            "subagent",
            "--repo",
            "widget",
            "--epic",
            "TRE03",
            "--ws",
            "3",
            "--role",
            "reviewer",
        ],
    )

    assert result.exit_code == 1  # the pipeline's clean refusal, NOT click's 2
    assert seen["spec"].role == "reviewer"
    assert seen["spec"].issue is None  # no --issue reached the spec as None


def test_cli_write_spawn_without_issue_is_a_clean_runtime_refusal():
    """A write role with no --issue is the pipeline's shape refusal (exit 1 with
    the positive-integer message through the shell) — not a click usage error.
    Reached with NO fakes: the shape gate fires before any I/O."""
    result = CliRunner().invoke(
        spawn_verb.spawn,
        [
            "subagent",
            "--repo",
            "widget",
            "--epic",
            "TRE03",
            "--ws",
            "3",
            "--role",
            "implementer",
        ],
    )

    assert result.exit_code == 1
    assert "error: " in result.output
    assert "--issue must be a positive integer" in result.output


def test_cli_unknown_backend_is_a_usage_error_exit_2():
    """--backend is gated by click.Choice over the adapter registry: a name
    outside it is a parse-time usage error (the exit-2 tier), while the
    pipeline's own gate guards programmatic callers (typed test)."""
    result = CliRunner().invoke(
        spawn_verb.spawn,
        [
            "subagent",
            "--repo",
            "widget",
            "--issue",
            "1",
            "--role",
            "implementer",
            "--backend",
            "nonexistent",
        ],
    )

    assert result.exit_code == 2
    assert "nonexistent" in result.output


def test_format_spawned_is_a_pure_string_function(tmp_path):
    """The renderer is drivable with no terminal: sentinel + indented payload,
    no trailing newline (the shared emit owns the terminal write)."""
    result = gh.HeadPr(number=9, state="OPEN", is_draft=True, base_ref="main")
    spawned = spawn_verb.subagent.SpawnResult(
        tree="/trees/x",
        branch="issues/9/work",
        base="origin/main",
        role="implementer",
        backend="claude",
        pr=result.number,
        pr_state=result.state,
        pr_is_draft=result.is_draft,
    )
    text = spawn_verb.format_spawned(spawned)
    assert text.startswith("SPAWNED\n{")
    assert not text.endswith("\n")
    assert json.loads(text.removeprefix("SPAWNED\n"))["pr"] == 9
