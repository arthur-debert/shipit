from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import managed_pretooluse_hook_command

REPO_ROOT = Path(__file__).resolve().parents[1]


def _coordinator_edit_payload() -> str:
    return json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/shipit/harness/policy.py"},
        }
    )


def _run_wrapper(cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", managed_pretooluse_hook_command()],
        cwd=cwd,
        env=env,
        input=_coordinator_edit_payload(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_wrapper_blocks_when_pixi_is_entirely_absent(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = _run_wrapper(tmp_path, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "could not run" in result.stderr
    assert "pixi" in result.stderr


def test_wrapper_blocks_on_any_nonzero_resolution_chain_exit(tmp_path):
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "pixi"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "pixi: simulated resolution failure (e.g. launcher missing/pin unresolvable)" >&2
            exit 1
            """
        )
    )
    stub.chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = _run_wrapper(tmp_path, env)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "could not run" in result.stderr


def test_wrapper_passes_a_real_decided_guard_through_unchanged():
    if shutil.which("pixi") is None:
        pytest.skip("pixi not on PATH in this environment")
    if not (REPO_ROOT / ".pixi" / "envs" / "default").exists():
        pytest.skip("no provisioned default env — refusing to trigger a solve")
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    result = _run_wrapper(REPO_ROOT, env)
    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
