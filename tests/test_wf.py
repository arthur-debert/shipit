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
import yaml

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
# Pure cores — the stage-caller uniform secret rule (#896)
# --------------------------------------------------------------------------

_APPLE = sorted(wf.SIGN_BLOCK_SECRETS)


def _caller(
    *,
    full=("RELEASE_TOKEN", "CARGO_REGISTRY_TOKEN", *_APPLE),
    prepare=("RELEASE_TOKEN", "CARGO_REGISTRY_TOKEN", *_APPLE),
    build=(),
    sign=_APPLE,
    publish=("CARGO_REGISTRY_TOKEN",),
) -> dict:
    """A parsed stage-choice caller for a signing+crates plan; each stage's
    forwarded secret names (or the string 'inherit') are injectable so every
    drift class is one keyword away from the compliant default."""

    def job(stage: str, secrets) -> dict:
        spec = {
            "if": f"inputs.stage == '{stage}'",
            "uses": f"arthur-debert/shipit/.github/workflows/wf-{stage}.yml@v1",
        }
        if secrets == "inherit":
            spec["secrets"] = "inherit"
        elif secrets:
            spec["secrets"] = {name: f"${{{{ secrets.{name} }}}}" for name in secrets}
        return spec

    return {
        "on": {
            "workflow_dispatch": {
                "inputs": {"stage": {"options": list(wf.CALLER_STAGES)}}
            }
        },
        "jobs": {
            "release": job("full", full),
            "prepare": job("prepare", prepare),
            "build": job("build", build),
            "sign": job("sign", sign),
            "publish": job("publish", publish),
        },
    }


def test_stage_caller_jobs_maps_the_blessed_shape():
    assert wf.stage_caller_jobs(_caller()) == {
        "release": "full",
        "prepare": "prepare",
        "build": "build",
        "sign": "sign",
        "publish": "publish",
    }


def test_stage_caller_jobs_tolerates_the_yaml_11_on_key():
    # Plain safe_load turns `on:` into the boolean True (the checks-module
    # gotcha); the detector must read either key.
    doc = _caller()
    doc[True] = doc.pop("on")
    assert wf.stage_caller_jobs(doc) is not None


def test_stage_caller_jobs_ignores_other_workflows():
    # Not a dict, no dispatch trigger, and a dispatch without the five-stage
    # choice: all outside the blessed shape, all None — the lint stays scoped
    # to callers and never fires on ordinary workflows.
    assert wf.stage_caller_jobs("nope") is None
    assert wf.stage_caller_jobs({"on": "push", "jobs": {"a": {}}}) is None
    trimmed = _caller()
    trimmed["on"]["workflow_dispatch"]["inputs"]["stage"]["options"] = [
        "full",
        "prepare",
    ]
    assert wf.stage_caller_jobs(trimmed) is None


def test_compliant_caller_has_no_drift():
    # The blessed grants: prepare == full; sign == full ∩ the Apple set;
    # publish == full ∩ the endpoint set; build empty.
    assert wf.caller_secret_drift(_caller()) == []


def test_minimal_plan_caller_has_no_drift():
    # shipit's own shape: full/prepare forward RELEASE_TOKEN alone, and the
    # trimmed stages forward nothing (RELEASE_TOKEN is not among their
    # blocks' declared names).
    doc = _caller(
        full=("RELEASE_TOKEN",), prepare=("RELEASE_TOKEN",), sign=(), publish=()
    )
    assert wf.caller_secret_drift(doc) == []


def test_prepare_narrower_than_full_is_the_896_defect():
    # The live fire (#896 defect 1): prepare forwards RELEASE_TOKEN alone on
    # a signing+crates plan — preflight validates the WHOLE plan's secret set
    # at prepare entry, so the standalone dispatch can never pass.
    drift = wf.caller_secret_drift(_caller(prepare=("RELEASE_TOKEN",)))
    assert len(drift) == 1
    assert "stage prepare" in drift[0]
    assert "CARGO_REGISTRY_TOKEN" in drift[0]
    assert "ASC_API_KEY_BASE64" in drift[0]


def test_sign_missing_one_notary_name_is_the_896_defect():
    # #896 defect 2: the consumer sign jobs omitted ASC_API_KEY_BASE64 — green
    # on the full chain (wf-release forwards internally), dead standalone.
    partial = tuple(n for n in _APPLE if n != "ASC_API_KEY_BASE64")
    drift = wf.caller_secret_drift(_caller(sign=partial))
    assert len(drift) == 1
    assert "stage sign" in drift[0]
    assert "missing ASC_API_KEY_BASE64" in drift[0]


def test_stray_and_build_secrets_are_drift():
    # A stage forwarding a name outside full's trimmed set is as non-uniform
    # as a missing one; wf-build declares no secrets at all.
    extra = ("CARGO_REGISTRY_TOKEN", "PYPI_TOKEN")
    drift = wf.caller_secret_drift(_caller(publish=extra))
    assert len(drift) == 1
    assert "stray PYPI_TOKEN" in drift[0]
    drift = wf.caller_secret_drift(_caller(build=("RELEASE_TOKEN",)))
    assert len(drift) == 1
    assert "stage build" in drift[0]
    assert "stray RELEASE_TOKEN" in drift[0]


def test_inherit_is_never_too_narrow():
    # `secrets: inherit` forwards the repo's whole set — a superset of any
    # plan-required set — so an inheriting stage job always satisfies the
    # rule, and an all-inherit caller trivially does.
    assert wf.caller_secret_drift(_caller(prepare="inherit")) == []
    doc = _caller(
        full="inherit", prepare="inherit", build=(), sign="inherit", publish="inherit"
    )
    assert wf.caller_secret_drift(doc) == []


def test_enumerating_under_an_inheriting_full_is_checked_or_flagged():
    # full inherits, so the plan-required set is unknowable from the caller:
    # a TRIMMED stage's enumeration is still checkable — it must cover its
    # block's whole declared surface (forwarding a declared-but-unset name is
    # harmless; omitting a needed one is the #896 death) — while an
    # un-trimmed prepare enumeration cannot be proven complete and is
    # flagged as drift outright.
    endpoints = tuple(sorted(wf.PUBLISH_BLOCK_SECRETS))
    ok = _caller(full="inherit", prepare="inherit", publish=endpoints)
    assert wf.caller_secret_drift(ok) == []
    partial = tuple(n for n in _APPLE if n != "ASC_API_KEY_BASE64")
    drift = wf.caller_secret_drift(
        _caller(full="inherit", prepare="inherit", publish=endpoints, sign=partial)
    )
    assert len(drift) == 1
    assert "missing ASC_API_KEY_BASE64" in drift[0]
    drift = wf.caller_secret_drift(_caller(full="inherit", publish=endpoints))
    assert len(drift) == 1
    assert "stage prepare" in drift[0]
    assert "inherit" in drift[0]


def test_caller_without_a_full_job_makes_no_claim():
    doc = _caller()
    del doc["jobs"]["release"]
    assert wf.caller_secret_drift(doc) == []


def test_every_job_gating_a_stage_is_checked():
    # Two jobs gate `sign`: one compliant, one missing a notary name. There
    # is no arbitrary pick — the drifting duplicate is flagged regardless of
    # declaration order.
    doc = _caller()
    partial = tuple(n for n in _APPLE if n != "ASC_API_KEY_BASE64")
    doc["jobs"]["sign-two"] = {
        "if": "inputs.stage == 'sign'",
        "uses": "arthur-debert/shipit/.github/workflows/wf-sign-mac.yml@v1",
        "secrets": {name: f"${{{{ secrets.{name} }}}}" for name in partial},
    }
    drift = wf.caller_secret_drift(doc)
    assert len(drift) == 1
    assert "'sign-two'" in drift[0]
    assert "missing ASC_API_KEY_BASE64" in drift[0]


def test_duplicate_full_jobs_are_ambiguous_and_flagged():
    # Two jobs gating `full` leave the plan-required set ambiguous: that is
    # itself the violation, and no per-stage claim is made on top of it.
    doc = _caller()
    doc["jobs"]["release-two"] = dict(doc["jobs"]["release"])
    drift = wf.caller_secret_drift(doc)
    assert len(drift) == 1
    assert "'release'" in drift[0]
    assert "'release-two'" in drift[0]
    assert "ambiguous" in drift[0]


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


def test_drifting_stage_caller_refuses_before_act(tmp_path, capsys):
    """The #896 lint at the verb seam: a stage-choice caller whose `prepare`
    grant is narrower than `full`'s is refused loudly BEFORE act — act cannot
    see the class (secrets never ride a local smoke), so a green act run must
    not be allowed to launder the broken shape."""
    path = tmp_path / "release.yml"
    path.write_text(
        yaml.safe_dump(_caller(prepare=("RELEASE_TOKEN",))), encoding="utf-8"
    )
    rec = _Recorder()
    rc = wf.run(str(path), event=wf.EVENT_WORKFLOW_DISPATCH, run_cmd=rec)
    assert rc == 1
    err = capsys.readouterr().err
    assert "per-stage secret drift (#896)" in err
    assert "stage prepare" in err
    assert rec.calls == []


def test_compliant_stage_caller_proceeds_to_act(tmp_path):
    path = tmp_path / "release.yml"
    path.write_text(yaml.safe_dump(_caller()), encoding="utf-8")
    rec = _Recorder()
    rc = wf.run(str(path), event=wf.EVENT_WORKFLOW_DISPATCH, run_cmd=rec)
    assert rc == 0
    assert any(c[0] == "act" for c in rec.calls)


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
