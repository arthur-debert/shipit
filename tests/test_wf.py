"""Unit tests for `shipit wf test` — the act harness (TOL01-WS04 #553).

Three layers, mirroring test_lint.py:

* PURE CORES — event-payload crafting, workflow/job selection, and the act
  argv encoding, fixture-tested with no exec anywhere near them (PRD Testing
  Decisions: pure cores get full unit coverage).

* THE EXEC SEAM — the verb's act/docker invocations pinned through a
  recording ``run_cmd`` (ADR-0028): image pin, event path, job selector, and
  the build-on-miss docker flow are asserted from RECORDED argv, with no
  docker daemon involved. The untestable-surface notice (PRD story 41) is
  asserted present on green AND red runs.

* ONE REAL ACT SMOKE — a fixture workflow runs green end-to-end under act in
  the stock-Ubuntu container (the WS acceptance smoke). It needs act on PATH
  (pinned in the pixi `test` feature) and a live docker daemon; absent either
  it skips with a loud reason — the harness is exercised wherever the full
  gate runs (CI's ubuntu runner carries docker).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shipit import execrun, lint
from shipit.verbs import wf

# --------------------------------------------------------------------------
# Pure cores — event crafting
# --------------------------------------------------------------------------


def test_craft_push_event_targets_the_branch():
    payload = wf.craft_event(wf.EVENT_PUSH, branch="feature/x")
    assert payload["ref"] == "refs/heads/feature/x"
    assert "head_commit" in payload


def test_craft_pull_request_event_is_an_opened_pr():
    payload = wf.craft_event(wf.EVENT_PULL_REQUEST, branch="feature/x")
    assert payload["action"] == "opened"
    assert payload["pull_request"]["head"]["ref"] == "feature/x"
    assert payload["pull_request"]["base"]["ref"] == "main"


def test_craft_dispatch_event_carries_inputs():
    payload = wf.craft_event(
        wf.EVENT_WORKFLOW_DISPATCH, branch="main", inputs={"version": "1.2.3"}
    )
    assert payload["ref"] == "refs/heads/main"
    assert payload["inputs"] == {"version": "1.2.3"}


def test_craft_dispatch_event_defaults_to_no_inputs():
    assert wf.craft_event(wf.EVENT_WORKFLOW_DISPATCH)["inputs"] == {}


def test_craft_workflow_call_event_carries_inputs():
    """workflow_call is a first-class crafted kind (TOL02-WS06): the wf-*
    blocks are call-only, so smoking them needs a call payload — same minimal
    ref+inputs shape as a dispatch."""
    payload = wf.craft_event(
        wf.EVENT_WORKFLOW_CALL, branch="main", inputs={"version": "1.2.3"}
    )
    assert payload["ref"] == "refs/heads/main"
    assert payload["inputs"] == {"version": "1.2.3"}
    assert wf.EVENT_WORKFLOW_CALL in wf.EVENT_KINDS
    assert wf.EVENT_WORKFLOW_CALL in wf.INPUT_EVENT_KINDS


def test_craft_event_unknown_kind_is_a_value_error():
    with pytest.raises(ValueError, match="unknown event kind"):
        wf.craft_event("issues")


def test_parse_inputs_splits_on_the_first_equals():
    assert wf.parse_inputs(("version=1.2.3", "note=a=b")) == {
        "version": "1.2.3",
        "note": "a=b",
    }
    assert wf.parse_inputs(()) == {}


@pytest.mark.parametrize("bad", ["noequals", "=value"])
def test_parse_inputs_malformed_pair_is_a_value_error(bad):
    with pytest.raises(ValueError, match="malformed --input"):
        wf.parse_inputs((bad,))


# --------------------------------------------------------------------------
# Pure cores — workflow/job selection
# --------------------------------------------------------------------------

_WORKFLOW = """\
name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo build
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo test
"""


def test_workflow_jobs_lists_job_ids_in_order():
    assert wf.workflow_jobs(_WORKFLOW) == ["build", "test"]


def test_workflow_jobs_no_jobs_is_a_value_error():
    with pytest.raises(ValueError, match="declares no jobs"):
        wf.workflow_jobs("name: empty\non: push\n")


def test_workflow_jobs_unparseable_yaml_is_a_value_error():
    with pytest.raises(ValueError, match="not parseable"):
        wf.workflow_jobs("on: [push\n")


# --------------------------------------------------------------------------
# Pure cores — the act argv encoding
# --------------------------------------------------------------------------


def test_act_argv_pins_image_event_and_payload():
    argv = wf.act_argv(
        event="push", workflow=".github/workflows/ci.yml", event_path="/tmp/e.json"
    )
    assert argv[:2] == ["act", "push"]
    assert argv[argv.index("--workflows") + 1] == ".github/workflows/ci.yml"
    assert argv[argv.index("--eventpath") + 1] == "/tmp/e.json"
    assert "--pull=false" in argv
    # Every ubuntu runner label maps to the ONE pinned local image.
    for platform in wf.ACT_PLATFORMS:
        assert f"{platform}={wf.WF_IMAGE}" in argv
    assert "--job" not in argv


def test_act_argv_carries_the_job_selector_when_given():
    argv = wf.act_argv(
        event="push",
        workflow="w.yml",
        event_path="/tmp/e.json",
        job="build",
    )
    assert argv[argv.index("--job") + 1] == "build"


def test_untestable_notice_is_versioned_and_complete():
    notice = wf.untestable_notice()
    assert f"v{wf.UNTESTABLE_SURFACE_VERSION}" in notice
    for item in wf.UNTESTABLE_SURFACE:
        assert item in notice


# --------------------------------------------------------------------------
# The Exec seam — recorded argv, no docker anywhere
# --------------------------------------------------------------------------


class _Recorder:
    """Records every ``run_cmd`` invocation; scripts outcomes per (argv[0], argv[1]).

    ``codes`` maps a ``(binary, subcommand)`` head to an rc int or an
    :class:`~shipit.execrun.ExecError` to raise (the missing-binary hard-fail).
    Unlisted heads succeed. The act call's crafted payload is captured AT CALL
    TIME (the temp file is gone once ``run`` returns).
    """

    def __init__(self, codes=None):
        self.codes = codes or {}
        self.calls: list[list[str]] = []
        self.payloads: list[dict] = []

    def __call__(self, argv, *, timeout, check=False):
        self.calls.append(list(argv))
        outcome = self.codes.get((argv[0], argv[1]), 0)
        if isinstance(outcome, execrun.ExecError):
            raise outcome
        if "--eventpath" in argv:
            path = Path(argv[argv.index("--eventpath") + 1])
            self.payloads.append(json.loads(path.read_text(encoding="utf-8")))
        if check and outcome != 0:
            raise execrun.ExecError(list(argv), rc=outcome)
        return execrun.ExecResult(
            argv=tuple(argv), rc=outcome, stdout="", stderr="", duration_ms=1
        )


@pytest.fixture
def workflow_file(tmp_path):
    path = tmp_path / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text(_WORKFLOW, encoding="utf-8")
    return path


def test_green_run_records_the_act_invocation(workflow_file, capsys):
    rec = _Recorder()  # image probe hits, act exits 0
    rc = wf.run(str(workflow_file), job="build", run_cmd=rec)
    assert rc == 0
    act = next(c for c in rec.calls if c[0] == "act")
    assert act[1] == "push"
    assert act[act.index("--workflows") + 1] == str(workflow_file)
    assert act[act.index("--job") + 1] == "build"
    assert f"ubuntu-latest={wf.WF_IMAGE}" in act
    # The crafted payload really rode the wire.
    assert rec.payloads == [wf.craft_event(wf.EVENT_PUSH, branch="main")]
    out = capsys.readouterr().out
    assert "WF TEST: OK" in out


def test_notice_prints_on_green_and_red_runs(workflow_file, capsys):
    """PRD story 41: the untestable-surface statement rides EVERY run — a green
    one must not oversell its coverage, a red one still states the boundary."""
    header = f"act cannot verify (surface statement v{wf.UNTESTABLE_SURFACE_VERSION})"
    assert wf.run(str(workflow_file), run_cmd=_Recorder()) == 0
    assert header in capsys.readouterr().out
    rc = wf.run(str(workflow_file), run_cmd=_Recorder(codes={("act", "push"): 1}))
    assert rc == 1
    out = capsys.readouterr().out
    assert header in out
    assert "WF TEST: FAILED" in out


def test_dispatch_inputs_reach_the_payload(workflow_file):
    rec = _Recorder()
    rc = wf.run(
        str(workflow_file),
        event=wf.EVENT_WORKFLOW_DISPATCH,
        inputs=("version=1.2.3",),
        run_cmd=rec,
    )
    assert rc == 0
    assert rec.payloads == [
        wf.craft_event(wf.EVENT_WORKFLOW_DISPATCH, inputs={"version": "1.2.3"})
    ]


def test_image_is_built_from_the_packaged_dockerfile_on_miss(workflow_file):
    """A missing local image is built (docker build, pinned tag, the SHIPPED
    Dockerfile) before act runs; a present one is only probed — idempotent."""
    rec = _Recorder(codes={("docker", "image"): 1})  # inspect misses
    assert wf.run(str(workflow_file), run_cmd=rec) == 0
    build = next(c for c in rec.calls if c[:2] == ["docker", "build"])
    assert build[build.index("--tag") + 1] == wf.WF_IMAGE
    dockerfile = build[build.index("--file") + 1]
    assert dockerfile == lint.data_path(wf.WF_DOCKERFILE)
    # build happens BEFORE act.
    heads = [c[0] for c in rec.calls]
    assert heads.index("docker") < heads.index("act")

    hit = _Recorder()  # probe hits: no build call
    assert wf.run(str(workflow_file), run_cmd=hit) == 0
    assert not any(c[:2] == ["docker", "build"] for c in hit.calls)


def test_missing_act_or_docker_hard_fails(workflow_file, capsys):
    """The hard-fail contract (PRD story 8): a missing binary is the Exec
    runner's launch failure, surfaced as one `error: …` line + exit 1 — never
    a silent skip, never a green."""
    missing = execrun.ExecError(
        ["docker", "image", "inspect", wf.WF_IMAGE],
        rc=None,
        cause=execrun.CAUSE_MISSING_BINARY,
    )
    rc = wf.run(
        str(workflow_file), run_cmd=_Recorder(codes={("docker", "image"): missing})
    )
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_unknown_job_selector_hard_errors_listing_jobs(workflow_file, capsys):
    """The Tool-verb selector rule (ADR-0039): a selector matching nothing is a
    hard error NAMING the valid selectors — and act is never invoked."""
    rec = _Recorder()
    rc = wf.run(str(workflow_file), job="deploy", run_cmd=rec)
    assert rc == 1
    err = capsys.readouterr().err
    assert "deploy" in err
    assert "build, test" in err
    assert rec.calls == []


def test_missing_workflow_file_refuses(tmp_path, capsys):
    rec = _Recorder()
    assert wf.run(str(tmp_path / "nope.yml"), run_cmd=rec) == 1
    assert "not a workflow file" in capsys.readouterr().err
    assert rec.calls == []


def test_inputs_refused_outside_input_bearing_events(workflow_file, capsys):
    rec = _Recorder()
    rc = wf.run(str(workflow_file), inputs=("version=1",), run_cmd=rec)
    assert rc == 1
    assert "--input only applies" in capsys.readouterr().err
    assert rec.calls == []


def test_dry_run_and_local_repositories_ride_the_act_argv():
    """The block-smoke encodings (TOL02-WS06): --dry-run is act's -n (the
    side-effectful wf-* blocks' smoke mode) and each --local-repository
    mapping rides verbatim (the composed chain's offline @vN resolution)."""
    argv = wf.act_argv(
        event=wf.EVENT_WORKFLOW_CALL,
        workflow="wf.yml",
        event_path="e.json",
        dry_run=True,
        local_repositories=("arthur-debert/shipit@v1=/tree",),
    )
    assert "--dryrun" in argv
    assert argv[argv.index("--local-repository") + 1] == "arthur-debert/shipit@v1=/tree"
    plain = wf.act_argv(event=wf.EVENT_PUSH, workflow="wf.yml", event_path="e.json")
    assert "--dryrun" not in plain
    assert "--local-repository" not in plain


def test_workflow_call_inputs_reach_the_payload(workflow_file):
    rec = _Recorder()
    rc = wf.run(
        str(workflow_file),
        event=wf.EVENT_WORKFLOW_CALL,
        inputs=("version=1.2.3",),
        run_cmd=rec,
    )
    assert rc == 0
    assert rec.payloads == [
        wf.craft_event(wf.EVENT_WORKFLOW_CALL, inputs={"version": "1.2.3"})
    ]
    act = next(c for c in rec.calls if c[0] == "act")
    assert act[1] == "workflow_call"


def test_malformed_input_refuses(workflow_file, capsys):
    rc = wf.run(
        str(workflow_file),
        event=wf.EVENT_WORKFLOW_DISPATCH,
        inputs=("oops",),
        run_cmd=_Recorder(),
    )
    assert rc == 1
    assert "malformed --input" in capsys.readouterr().err


def test_jobless_workflow_refuses(tmp_path, capsys):
    path = tmp_path / "w.yml"
    path.write_text("name: empty\non: push\n", encoding="utf-8")
    assert wf.run(str(path), run_cmd=_Recorder()) == 1
    assert "declares no jobs" in capsys.readouterr().err


def test_unparseable_workflow_refuses_on_a_single_stderr_line(tmp_path, capsys):
    """A YAML parse error tails multi-line parser context, but the refusal must
    still be ONE stderr line — the `wf test: …` contract, matching cli_errors."""
    path = tmp_path / "bad.yml"
    # An unterminated flow sequence: PyYAML reports it across several lines.
    path.write_text("on: push\njobs: [build, test\n", encoding="utf-8")
    # Guard: the raw parser message really is multi-line, so this has teeth.
    with pytest.raises(ValueError) as excinfo:
        wf.workflow_jobs(path.read_text(encoding="utf-8"))
    assert "\n" in str(excinfo.value)

    assert wf.run(str(path), run_cmd=_Recorder()) == 1
    err = capsys.readouterr().err
    assert "not parseable as workflow YAML" in err
    assert err.strip().count("\n") == 0


# --------------------------------------------------------------------------
# The packaged Dockerfile is the containers-doc image, never a fork
# --------------------------------------------------------------------------


def test_packaged_dockerfile_matches_the_containers_doc_image():
    """docs/dev/containers.md earmarks docker/ubuntu.Dockerfile as THE base for
    the act harness; the packaged copy `shipit/data/ubuntu.Dockerfile` (what a
    wheel install builds from) must stay byte-identical — the dogfood drift
    pattern (one body, two readers, a test pinning the pair)."""
    repo_copy = Path(__file__).resolve().parents[1] / "docker" / "ubuntu.Dockerfile"
    packaged = Path(lint.data_path(wf.WF_DOCKERFILE))
    assert packaged.read_bytes() == repo_copy.read_bytes()


# --------------------------------------------------------------------------
# The real act smoke (WS acceptance): one fixture workflow, green end-to-end
# --------------------------------------------------------------------------


# `docker info` can hang when the daemon is wedged, and this probe runs at
# COLLECTION time (the skipif below), so an unbounded call would stall the whole
# suite. Bound it and treat a timeout (or a vanished CLI) as "daemon unavailable".
_DOCKER_PROBE_TIMEOUT = 10


def _docker_daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=_DOCKER_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return probe.returncode == 0


@pytest.mark.skipif(shutil.which("act") is None, reason="act not on PATH")
@pytest.mark.skipif(not _docker_daemon_up(), reason="docker daemon unavailable")
def test_real_act_smoke_runs_a_fixture_workflow_green(tmp_path, monkeypatch, capsys):
    """THE act smoke (issue #553 acceptance): `shipit wf test` runs a fixture
    workflow green end-to-end — real act, real docker, the stock-Ubuntu image
    built from the packaged Dockerfile. cwd is the fixture tree so act copies
    a two-file tree into the job container, not this repo."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    smoke = workflows / "smoke.yml"
    smoke.write_text(
        "name: smoke\n"
        "on: push\n"
        "jobs:\n"
        "  smoke:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hello from act\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rc = wf.run(".github/workflows/smoke.yml", job="smoke")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WF TEST: OK" in out
    assert "act cannot verify" in out
