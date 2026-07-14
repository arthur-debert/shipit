"""The `shipit ci plan` shell (TOL01-WS05) — config read, path-diff seam,
JSON hand-off, uniform exit contract.

The pure planning is pinned in ``tests/test_tools_lanes.py``; these tests pin
the effectful shell around it: the machine-clean stdout contract (single-line
JSON matrix, human summary on stderr), the pointed no-``[lanes]`` refusal
(exit 1) vs the legitimate empty thin plan (``[]``, exit 0), the usage tier
(unknown event, exit 2), and the git path-diff boundary through the injected
``changed_paths_fn`` seam — called only on ``pr`` events with a base ref, and
failing SAFE to full scope when git cannot answer.
"""

from __future__ import annotations

import json
import logging

import pytest

from shipit import logcontext
from shipit.verbs import ci as ci_verb

LANES_TOML = """
[toolchains]
"." = "python"
"crates/wasm" = "rust"

[lanes.lint]
run = "lint-full"
required = true
local = true

[lanes.wasm]
run = "build crates/wasm"
scope = "crates/wasm"
"""

PIXI_TOML = """
[feature.lint.tasks]
lint-full = "./bin/shipit lint"

[feature.test.tasks]
build = "./bin/shipit build"

[environments]
lint = ["lint"]
test = ["test"]
"""


@pytest.fixture
def laned_repo(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(LANES_TOML, encoding="utf-8")
    (tmp_path / "pixi.toml").write_text(PIXI_TOML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _matrix(capsys):
    out, err = capsys.readouterr()
    return json.loads(out), err


def test_plan_emits_the_matrix_as_single_line_json_on_stdout(laned_repo, capsys):
    rc = ci_verb.run(event="pr")
    assert rc == 0
    out, err = capsys.readouterr()
    # stdout is EXACTLY the one-line matrix (the block pipes it into
    # $GITHUB_OUTPUT); everything human goes to stderr.
    assert out == json.dumps(json.loads(out)) + "\n"
    assert json.loads(out) == [
        {
            "name": "lint",
            "run": "lint-full",
            "runner": "ubuntu-latest",
            "required": True,
            "envs": "lint",
            "envset": "lint",
            "caches": {"rust": False, "sccache": False, "uv": False},
            "rust_workspaces": "",
            "secrets": [],
        },
        {
            "name": "wasm",
            "run": "build crates/wasm",
            "runner": "ubuntu-latest",
            "required": False,
            "envs": "test",
            "envset": "test",
            "caches": {"rust": True, "sccache": False, "uv": False},
            "rust_workspaces": "crates/wasm -> ../../target",
            "secrets": [],
        },
    ]
    assert "2 of 2 lanes: lint, wasm" in err


def test_plan_logs_work_env_evidence_for_each_ci_lane(laned_repo, capsys, caplog):
    caplog.set_level(logging.INFO, logger="shipit.ci")
    logcontext.bind(repo="acme/widget")
    try:
        rc = ci_verb.run(event="pr")
    finally:
        logcontext.unbind("repo")

    assert rc == 0
    capsys.readouterr()
    records = [
        record
        for record in caplog.records
        if getattr(record, "work_env_boundary", None) == "ci.lane-job"
    ]
    assert [record.lane for record in records] == ["lint", "wasm"]
    assert {record.checkout_strategy for record in records} == {"direct-checkout"}
    assert {record.routing for record in records} == {"pixi-run"}
    assert records[0].working_dir == str(laned_repo.resolve())
    assert records[0].working_dir_repo == "acme/widget"
    assert records[0].pixi_environment_name == "lint"
    assert "pixi_run_id" not in records[0].__dict__


def test_plan_accepts_the_github_event_name_verbatim(laned_repo, capsys):
    # The block passes `github.event_name` untranslated; the mapping is the
    # planner's (fixture-tested), never YAML's.
    assert ci_verb.run(event="workflow_dispatch") == 0
    matrix, err = _matrix(capsys)
    assert [job["name"] for job in matrix] == ["lint", "wasm"]
    assert "dispatch ->" in err


def test_unknown_event_is_the_usage_tier(laned_repo, capsys):
    rc = ci_verb.run(event="merge_group")
    assert rc == 2
    out, err = capsys.readouterr()
    assert out == ""  # never half a matrix on a usage error
    assert "unknown event 'merge_group'" in err


def test_no_lanes_declared_is_a_pointed_refusal(tmp_path, monkeypatch, capsys):
    # Fail closed: a repo calling the checks block with nothing to run is a
    # misconfiguration, not a green no-op. The message carries the fix.
    (tmp_path / ".shipit.toml").write_text('[toolchains]\n"." = "python"\n')
    monkeypatch.chdir(tmp_path)
    rc = ci_verb.run(event="pr")
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "no [lanes] declared" in err and "[lanes.lint]" in err


def test_missing_config_file_is_the_same_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert ci_verb.run(event="pr") == 1
    assert "no [lanes] declared" in capsys.readouterr().err


def test_non_utf8_pixi_toml_is_a_config_error(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text(LANES_TOML, encoding="utf-8")
    (tmp_path / "pixi.toml").write_bytes(b"\xff")
    monkeypatch.chdir(tmp_path)

    assert ci_verb.run(event="pr") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "malformed" in err
    assert "pixi.toml" in err


def test_pr_with_base_ref_thins_through_the_diff_seam(laned_repo, capsys):
    seen = {}

    def fake_diff(base_ref, cwd):
        seen["base_ref"], seen["cwd"] = base_ref, cwd
        return ["README.md"]

    rc = ci_verb.run(event="pull_request", base_ref="main", changed_paths_fn=fake_diff)
    assert rc == 0
    assert seen["base_ref"] == "main"
    assert seen["cwd"] == str(laned_repo.resolve())
    matrix, err = _matrix(capsys)
    assert [job["name"] for job in matrix] == ["lint"]  # wasm thinned out
    assert "1 of 2 lanes" in err


def test_an_empty_thin_plan_is_a_valid_zero_exit(tmp_path, monkeypatch, capsys):
    (tmp_path / ".shipit.toml").write_text(
        '[lanes.wasm]\nrun = "build crates/wasm"\nscope = "crates/wasm"\n'
    )
    monkeypatch.chdir(tmp_path)
    rc = ci_verb.run(event="pr", base_ref="main", changed_paths_fn=lambda r, c: ["x"])
    assert rc == 0
    matrix, err = _matrix(capsys)
    assert matrix == []  # the block skips the run job on '[]'
    assert "0 of 1 lane: none" in err


def test_an_unanswerable_diff_fails_safe_to_full_scope(laned_repo, capsys):
    rc = ci_verb.run(event="pr", base_ref="main", changed_paths_fn=lambda r, c: None)
    assert rc == 0
    matrix, err = _matrix(capsys)
    assert [job["name"] for job in matrix] == ["lint", "wasm"]  # more, never fewer
    assert "planning full scope" in err


def test_the_diff_seam_is_never_consulted_off_pr_events(laned_repo, capsys):
    def explode(base_ref, cwd):  # pragma: no cover — the assertion is "not called"
        raise AssertionError("path-diff consulted on a non-PR event")

    rc = ci_verb.run(event="push", base_ref="main", changed_paths_fn=explode)
    assert rc == 0
    matrix, _ = _matrix(capsys)
    assert [job["name"] for job in matrix] == ["lint", "wasm"]


def test_a_blank_base_ref_plans_full_without_touching_git(laned_repo, capsys):
    # The block passes `github.base_ref` verbatim — empty off pull_request.
    rc = ci_verb.run(event="pr", base_ref="  ", changed_paths_fn=None)
    assert rc == 0
    matrix, _ = _matrix(capsys)
    assert [job["name"] for job in matrix] == ["lint", "wasm"]
