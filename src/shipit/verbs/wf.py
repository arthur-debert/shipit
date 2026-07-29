"""wf — validate GitHub Actions workflows locally by running them under act."""

from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

import click
import yaml

from .. import execrun
from ..lint import data_path
from ._errors import cli_errors

logger = logging.getLogger("shipit.wf")


EVENT_PUSH = "push"
EVENT_PULL_REQUEST = "pull_request"
EVENT_WORKFLOW_DISPATCH = "workflow_dispatch"
EVENT_WORKFLOW_CALL = "workflow_call"
EVENT_KINDS: tuple[str, ...] = (
    EVENT_PUSH,
    EVENT_PULL_REQUEST,
    EVENT_WORKFLOW_DISPATCH,
    EVENT_WORKFLOW_CALL,
)

INPUT_EVENT_KINDS: tuple[str, ...] = (EVENT_WORKFLOW_DISPATCH, EVENT_WORKFLOW_CALL)

WF_IMAGE = "shipit-wf-ubuntu:24.04"

WF_DOCKERFILE = "ubuntu.Dockerfile"

ACT_PLATFORMS: tuple[str, ...] = ("ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04")

UNTESTABLE_SURFACE_VERSION = 2

UNTESTABLE_SURFACE: tuple[str, ...] = (
    "macOS and Windows runner jobs (act runs linux containers only)",
    "GPU and special-hardware runners",
    "cross-workflow cascade (workflow_run chains, repository_dispatch fan-out)",
    "workflow_call fidelity is partial (nested reusable-workflow plumbing "
    "diverges under act)",
    "workflow_dispatch UX (the Actions-tab form: input rendering, defaults, "
    "validation)",
    "the wf-sign-mac signer leg (macOS runner, Apple keychain import, "
    "codesign/notarytool) — no linux analogue exists (TOL02-WS06)",
    "release side effects (wf-prepare's push, wf-publish's endpoint "
    "dispatches): the wf-* block smokes run act in dry-run mode, which "
    "executes no step (TOL02-WS06)",
)

ACT_TIMEOUT: float = 600.0

IMAGE_BUILD_TIMEOUT: float = 600.0


class RunCmd(Protocol):
    """The injectable Exec seam every act/docker invocation goes through."""

    def __call__(
        self, argv: list[str], *, timeout: float, check: bool = False
    ) -> execrun.ExecResult: ...


def parse_inputs(pairs: tuple[str, ...]) -> dict[str, str]:
    """``KEY=VALUE`` pairs → the dispatch-inputs mapping; splits on the first ``=``, raises ValueError on a malformed pair."""
    inputs: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(
                f"malformed --input {pair!r} (expected KEY=VALUE, e.g. version=1.2.3)"
            )
        inputs[key] = value
    return inputs


def craft_event(
    kind: str, *, branch: str = "main", inputs: dict[str, str] | None = None
) -> dict[str, Any]:
    """The crafted event payload for ``kind``; raises ValueError for a kind outside EVENT_KINDS."""
    if kind == EVENT_PUSH:
        return {
            "ref": f"refs/heads/{branch}",
            "head_commit": {"message": "shipit wf test: crafted push event"},
        }
    if kind == EVENT_PULL_REQUEST:
        return {
            "action": "opened",
            "number": 1,
            "pull_request": {
                "title": "shipit wf test: crafted pull_request event",
                "draft": False,
                "head": {"ref": branch},
                "base": {"ref": "main"},
            },
        }
    if kind in (EVENT_WORKFLOW_DISPATCH, EVENT_WORKFLOW_CALL):
        return {
            "ref": f"refs/heads/{branch}",
            "inputs": dict(inputs or {}),
        }
    raise ValueError(f"unknown event kind {kind!r} (one of: {', '.join(EVENT_KINDS)})")


def workflow_jobs(text: str) -> list[str]:
    """The job ids a workflow body declares, in document order; raises ValueError on unparseable YAML or no ``jobs:``."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not parseable as workflow YAML: {exc}") from exc
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        raise ValueError("workflow declares no jobs (missing or empty `jobs:` map)")
    return [str(job) for job in jobs]


CALLER_STAGES: tuple[str, ...] = ("full", "prepare", "build", "sign", "publish")

SIGN_BLOCK_SECRETS = frozenset(
    {
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "ASC_API_KEY_BASE64",
        "ASC_API_KEY_ID",
        "ASC_API_ISSUER_ID",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    }
)

PUBLISH_BLOCK_SECRETS = frozenset(
    {
        "CARGO_REGISTRY_TOKEN",
        "PYPI_TOKEN",
        "NPM_TOKEN",
        "HOMEBREW_TAP_TOKEN",
        "VSCE_PAT",
        "OVSX_PAT",
        "DOWNSTREAM_DISPATCH_TOKEN",
        "ARTIFACT_CHANNEL_KEY_ID",
        "ARTIFACT_CHANNEL_SECRET_KEY",
    }
)

_STAGE_TRIM: dict[str, frozenset[str] | None] = {
    "prepare": None,
    "build": frozenset(),
    "sign": SIGN_BLOCK_SECRETS,
    "publish": PUBLISH_BLOCK_SECRETS,
}

_STAGE_GATE_RE = re.compile(r"inputs\.stage\s*==\s*'([a-z]+)'")


def stage_caller_jobs(doc: object) -> dict[str, str] | None:
    """Job id → stage when ``doc`` parses as the stage-choice dispatch caller, else None."""
    if not isinstance(doc, dict):
        return None
    on = doc.get("on", doc.get(True))
    if not isinstance(on, dict):
        return None
    dispatch = on.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return None
    inputs = dispatch.get("inputs")
    stage = inputs.get("stage") if isinstance(inputs, dict) else None
    if not isinstance(stage, dict):
        return None
    if sorted(stage.get("options") or []) != sorted(CALLER_STAGES):
        return None
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return None
    out: dict[str, str] = {}
    for job_id, job in jobs.items():
        cond = job.get("if", "") if isinstance(job, dict) else ""
        match = _STAGE_GATE_RE.search(str(cond))
        if match and match.group(1) in CALLER_STAGES:
            out[str(job_id)] = match.group(1)
    return out or None


def caller_secret_drift(doc: object) -> list[str]:
    """Violations of the per-stage secret rule, one line each; empty when ``doc`` is not a stage-choice caller or its grants hold."""
    stages = stage_caller_jobs(doc)
    if stages is None:
        return []
    jobs = doc["jobs"]
    by_stage: dict[str, list[str]] = {}
    for job_id, stage in stages.items():
        by_stage.setdefault(stage, []).append(job_id)
    full_ids = by_stage.get("full", [])
    if not full_ids:
        return []
    if len(full_ids) > 1:
        named = ", ".join(repr(j) for j in full_ids)
        return [
            f"jobs {named} all gate stage full — the blessed caller has ONE "
            "job per stage, and duplicate full jobs leave the plan-required "
            "secret set ambiguous; keep a single full job"
        ]
    full_id = full_ids[0]

    def forwarded(job_id: str) -> frozenset[str] | None:
        job = jobs[job_id]
        secrets = job.get("secrets") if isinstance(job, dict) else None
        if secrets == "inherit":
            return None
        return frozenset(secrets) if isinstance(secrets, dict) else frozenset()

    full = forwarded(full_id)
    violations: list[str] = []
    for stage in CALLER_STAGES[1:]:
        for job_id in by_stage.get(stage, ()):
            got = forwarded(job_id)
            if got is None:
                continue
            trim = _STAGE_TRIM[stage]
            if full is None and trim is None:
                violations.append(
                    f"job {job_id!r} (stage {stage}) enumerates its secrets "
                    f"while {full_id!r} (stage full) rides `secrets: inherit` "
                    "— a list cannot be proven to cover the plan; inherit "
                    "here too"
                )
                continue
            want = trim if full is None else (full if trim is None else full & trim)
            missing = sorted(want - got)
            stray = sorted(got - want)
            if missing or stray:
                parts = []
                if missing:
                    parts.append("missing " + ", ".join(missing))
                if stray:
                    parts.append("stray " + ", ".join(stray))
                violations.append(
                    f"job {job_id!r} (stage {stage}) must forward the same "
                    f"plan-required secret set as {full_id!r} (stage full), "
                    f"trimmed to its block's declared names ({'; '.join(parts)})"
                )
    return violations


def act_argv(
    *,
    event: str,
    workflow: str,
    event_path: str,
    job: str | None = None,
    image: str = WF_IMAGE,
    dry_run: bool = False,
    local_repositories: tuple[str, ...] = (),
) -> list[str]:
    """The act invocation for one workflow/job × crafted event."""
    argv = [
        "act",
        event,
        "--workflows",
        workflow,
        "--eventpath",
        event_path,
        "--pull=false",
    ]
    for platform in ACT_PLATFORMS:
        argv += ["--platform", f"{platform}={image}"]
    if job is not None:
        argv += ["--job", job]
    if dry_run:
        argv += ["--dryrun"]
    for mapping in local_repositories:
        argv += ["--local-repository", mapping]
    return argv


def untestable_notice() -> str:
    lines = [
        f"act cannot verify (surface statement v{UNTESTABLE_SURFACE_VERSION}) — "
        "a green run here proves nothing about:"
    ]
    lines += [f"  - {item}" for item in UNTESTABLE_SURFACE]
    return "\n".join(lines)


def _run_cmd(
    argv: list[str], *, timeout: float, check: bool = False
) -> execrun.ExecResult:
    """Run ``argv`` through the one Exec runner; ``check=False`` treats a nonzero rc as a verdict rather than a transport failure."""
    return execrun.run(argv, check=check, timeout=timeout)


def ensure_image(run_cmd: RunCmd) -> bool:
    """Make WF_IMAGE exist locally; return True when it was built."""
    probe = run_cmd(
        ["docker", "image", "inspect", WF_IMAGE],
        timeout=execrun.DEFAULT_TIMEOUT,
        check=False,
    )
    if probe.rc == 0:
        return False
    dockerfile = data_path(WF_DOCKERFILE)
    run_cmd(
        [
            "docker",
            "build",
            "--tag",
            WF_IMAGE,
            "--file",
            dockerfile,
            str(Path(dockerfile).parent),
        ],
        timeout=IMAGE_BUILD_TIMEOUT,
        check=True,
    )
    return True


def _fail(message: str) -> int:
    """One refusal line on stderr (collapsed to a single line) + the runtime-failure exit."""
    line = " ".join(message.split())
    print(f"wf test: {line}", file=sys.stderr)
    logger.error("wf test refused", extra={"reason": line})
    return 1


@cli_errors
def run(
    workflow: str,
    *,
    job: str | None = None,
    event: str = EVENT_PUSH,
    branch: str = "main",
    inputs: tuple[str, ...] = (),
    dry_run: bool = False,
    local_repositories: tuple[str, ...] = (),
    run_cmd: RunCmd | None = None,
) -> int:
    """Run ``workflow`` (or one ``job`` of it) under act against a crafted ``event``; returns 0 clean, 1 on a failed verdict or refusal."""
    started = time.monotonic()
    wf_path = Path(workflow)
    if not wf_path.is_file():
        return _fail(f"{workflow} is not a workflow file")
    if inputs and event not in INPUT_EVENT_KINDS:
        return _fail(
            f"--input only applies to --event {' / '.join(INPUT_EVENT_KINDS)} "
            f"(got --event {event})"
        )
    text = wf_path.read_text(encoding="utf-8")
    try:
        dispatch_inputs = parse_inputs(inputs)
        jobs = workflow_jobs(text)
    except ValueError as exc:
        return _fail(str(exc))
    if job is not None and job not in jobs:
        return _fail(f"no job {job!r} in {workflow} — jobs: {', '.join(jobs)}")
    drift = caller_secret_drift(yaml.safe_load(text))
    if drift:
        return _fail("per-stage secret drift (#896): " + "; ".join(drift))

    run_cmd = run_cmd or _run_cmd
    payload = craft_event(event, branch=branch, inputs=dispatch_inputs)

    print(f"wf test: {workflow} (event {event}" + (f", job {job})" if job else ")"))
    if not dry_run and ensure_image(run_cmd):
        print(f"  built {WF_IMAGE} (stock-Ubuntu act runner image)")

    with tempfile.TemporaryDirectory(prefix="shipit-wf-") as tmp:
        event_path = str(Path(tmp) / "event.json")
        Path(event_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        argv = act_argv(
            event=event,
            workflow=workflow,
            event_path=event_path,
            job=job,
            dry_run=dry_run,
            local_repositories=local_repositories,
        )
        result = run_cmd(argv, timeout=ACT_TIMEOUT, check=False)

    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    print(untestable_notice())
    rc = 0 if result.rc == 0 else 1
    if rc == 0:
        print(f"WF TEST: OK ({workflow}, event {event})")
    else:
        print(f"WF TEST: FAILED ({workflow}, event {event}, act rc {result.rc})")
    logger.info(
        "wf test complete",
        extra={
            "workflow": workflow,
            "event": event,
            "job": job or "",
            "rc": rc,
            "act_rc": result.rc,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return rc


@click.group(name="wf")
def wf() -> None:
    """Workflow tools — validate workflows locally; prove sign live."""


@wf.command(name="test")
@click.argument("workflow")
@click.option("--job", help="Run only this job id (default: the whole workflow).")
@click.option(
    "--event",
    type=click.Choice(EVENT_KINDS),
    default=EVENT_PUSH,
    show_default=True,
    help="The crafted event kind to trigger the workflow with.",
)
@click.option(
    "--branch",
    default="main",
    show_default=True,
    help=(
        "The crafted event's branch: the push target, the PR head ref, or the "
        "dispatch ref."
    ),
)
@click.option(
    "--input",
    "inputs",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "A workflow input (repeatable; only with --event workflow_dispatch "
        "or workflow_call)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Run act in dry-run mode (-n): parse, match the trigger, evaluate "
        "the job graph — execute nothing. The smoke mode for side-effectful "
        "workflows (the wf-* release blocks)."
    ),
)
@click.option(
    "--local-repository",
    "local_repositories",
    multiple=True,
    metavar="OWNER/REPO@REF=PATH",
    help=(
        "Resolve a remote reusable-workflow/action ref against a local tree "
        "(repeatable; act --local-repository passthrough)."
    ),
)
def test_cmd(
    workflow: str,
    job: str | None,
    event: str,
    branch: str,
    inputs: tuple[str, ...],
    dry_run: bool,
    local_repositories: tuple[str, ...],
) -> None:
    """Run WORKFLOW under act in a container, against a crafted event."""
    raise SystemExit(
        run(
            workflow,
            job=job,
            event=event,
            branch=branch,
            inputs=inputs,
            dry_run=dry_run,
            local_repositories=local_repositories,
        )
    )
