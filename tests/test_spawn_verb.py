from __future__ import annotations

import json
from dataclasses import replace

from click.testing import CliRunner
from test_spawn_subagent import _PR, bounds

from shipit import gh
from shipit.harness import prompts
from shipit.verbs import spawn as spawn_verb


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
        "--pr",
        "--backend",
    ):
        assert token in result.output
    assert "Tree" in result.output


def test_spawn_brief_prints_the_template_with_every_mandatory_slot():
    for role in prompts.BRIEF_ROLES:
        result = CliRunner().invoke(spawn_verb.spawn, ["brief", role.value])

        assert result.exit_code == 0
        for slot in prompts.MANDATORY_BRIEF_SLOTS:
            assert slot in result.output


def test_spawn_brief_refuses_a_role_without_a_template():
    result = CliRunner().invoke(spawn_verb.spawn, ["brief", "explorer"])

    assert result.exit_code == 2


def test_run_renders_the_byte_stable_spawned_block(tmp_path, capsys):
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
    assert out == "SPAWNED\n" + json.dumps(payload, indent=2) + "\n"


def test_reviewer_spawned_block_omits_the_pr_linkage(tmp_path, capsys):
    b, _calls = bounds(tmp_path)

    rc = spawn_verb.run(
        repo="widget",
        role="reviewer",
        epic="TRE03",
        ws=3,
        backend="codex",
        bounds=b,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "SPAWNED"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload["role"] == "reviewer"
    assert "pr" not in payload and "sentinel" not in payload


def test_a_pipeline_refusal_maps_to_the_error_shell(tmp_path, capsys):
    b, _calls = bounds(tmp_path, pr=replace(_PR, is_draft=False))

    rc = spawn_verb.run(
        repo="widget", role="implementer", epic="TRE03", ws=1, issue=156, bounds=b
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert "SPAWNED" not in captured.out
    assert captured.err.startswith("error: ")
    assert "is not a draft" in captured.err
    assert captured.err == "".join(captured.err.splitlines()) + "\n"


def test_cli_reviewer_spawn_without_issue_is_not_a_usage_error(tmp_path, monkeypatch):
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

    assert result.exit_code == 1
    assert seen["spec"].role == "reviewer"
    assert seen["spec"].issue is None


def test_cli_write_spawn_without_issue_is_a_clean_runtime_refusal():
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
