"""The PreToolUse managed shell command (#529) — the OUTER wrapper boundary.

A DIFFERENT boundary than ``test_hook_pretooluse.py``: that file exercises
:func:`shipit.verbs.hook.pretooluse.run` IN-PROCESS — the INNER Python
contract, which deliberately fails open (allow) on a malformed payload it
actually received (bad JSON, a missing field). This file drives the real
managed ``.claude/settings.json`` command STRING through an actual subprocess
shell — the boundary a live Claude Code ``PreToolUse`` hook invocation crosses
BEFORE any shipit Python runs — and pins the #529 invariant at that outer
boundary: when shipit cannot be RESOLVED at all (no pixi on `PATH`, the
`pixi run`/`./bin/shipit` chain exiting non-zero for any reason), the wrapper
must BLOCK the tool call, never silently allow it.

#505/#491 regressed this exact path: it dropped `pixi run` and replaced the
resolution guard with `test -x ./bin/shipit || { echo ...; exit 0; }` — a
bare fail-open. Since a `PreToolUse` process never sources `CLAUDE_ENV_FILE`
(only Bash *tool calls* do), it runs with a bare `PATH` and no pixi
activation, so guard liveness silently depended on ambient resolution. These
tests fail on that code (asserting `returncode == 2` where the old command
would exit `0` with empty stdout) and pass after #529's fix.
"""

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
    # The #529 headline failure mode: a bare PATH with no pixi at all (exactly
    # what a PreToolUse process sees with no CLAUDE_ENV_FILE sourcing). Pre-#529
    # this exited 0 with empty stdout — a silent ALLOW of a coordinator code
    # edit. It must now BLOCK.
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = _run_wrapper(tmp_path, env)
    assert result.returncode == 2  # a blocking exit code, never 0-with-no-decision
    assert result.stdout == ""  # no synthetic ALLOW-shaped output on stdout
    assert "could not run" in result.stderr
    assert "pixi" in result.stderr  # names the actual unresolved dependency


def test_wrapper_blocks_on_any_nonzero_resolution_chain_exit(tmp_path):
    # Generic coverage of the OTHER named failure modes (launcher missing/not
    # executable, pin/uv unresolvable): whatever the underlying cause, the
    # `pixi run ./bin/shipit hook pretooluse` chain surfaces it as a non-zero
    # exit, and the wrapper must block on ANY such exit — deterministic here via
    # a stub `pixi` that fails the way a broken resolution would, without
    # depending on real network/solve behavior.
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
    # The happy path must be untouched: with a real, resolvable pixi/./bin/shipit
    # chain (this checkout), a coordinator code edit still gets denied with the
    # normal decision JSON on stdout and exit 0 — "ran and decided" is NOT the
    # same as "could not run", and the fix must not conflate the two.
    if shutil.which("pixi") is None:
        pytest.skip("pixi not on PATH in this environment")
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    result = _run_wrapper(REPO_ROOT, env)
    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
