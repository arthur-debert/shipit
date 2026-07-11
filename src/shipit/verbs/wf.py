"""wf — workflow tools: validate GitHub Actions workflows locally (TOL01-WS04).

``shipit wf test`` runs ONE workflow (or one job of it) under `act
<https://github.com/nektos/act>`_ in a container, against a CRAFTED event
payload (a push to a branch, an opened pull request, a dispatch with inputs,
a direct reusable-workflow call with inputs),
so a workflow edit is validated locally BEFORE the first push — the
push-to-find-out loop the TOL01 PRD's problem statement opens with (stories 40
and 41; ADR-0039 gives it the uniform Tool verb shape, ADR-0040 makes shipit's
published workflow blocks its standing subjects).

The container act runs the job in is built from shipit's stock-Ubuntu baseline
(the packaged ``shipit/data/ubuntu.Dockerfile`` — the docs/dev/containers.md
image, byte-identical to ``docker/ubuntu.Dockerfile``; a drift test pins the
two together), tagged :data:`WF_IMAGE` and mapped over every ubuntu runner
label act may resolve (:data:`ACT_PLATFORMS`). ``--pull=false`` keeps act off
the network for it. act's default (non-``--bind``) mode COPIES the tree into a
container volume — the same no-bind-mount posture as the self-provision
harness's tar pipe, so host-uid ownership never leaks into the verdict.

Every run — green or red — prints the ACT-UNTESTABLE SURFACE
(:data:`UNTESTABLE_SURFACE`, a fixed, versioned statement, never free text):
macOS/Windows runners, GPU, cross-workflow cascade, partial ``workflow_call``
fidelity, dispatch UX, the wf-sign-mac signer leg, and the release blocks'
real side effects are outside act's reach, and the standing notice is
what keeps a local green trusted only where it is valid (PRD story 41).

The pure cores — event-payload crafting (:func:`craft_event` /
:func:`parse_inputs`), job selection (:func:`workflow_jobs`), and the act argv
encoding (:func:`act_argv`) — are kept out of the Exec boundary so they are
fixture-testable with no docker anywhere near the tests, the same split the
lint verb uses. Every act/docker invocation goes through the one Exec runner
(:mod:`shipit.execrun`, ADR-0028) via the injectable ``run_cmd`` seam; verb
tests assert the RECORDED argv.

Exit semantics are the uniform Tool contract (PRD story 8): ``0`` clean, ``1``
a failed verdict (act's nonzero — a red job, an event the workflow does not
listen to) or a refusal (missing workflow file, unknown job selector), and a
missing ``act``/``docker`` binary is the standard HARD-fail (the Exec runner's
launch failure, mapped by :func:`~._errors.cli_errors` to one ``error: …``
line + exit 1) — never a silent skip.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

import click
import yaml

from .. import execrun
from ..lint import _data_path
from ._errors import cli_errors

logger = logging.getLogger("shipit.wf")

# --------------------------------------------------------------------------
# The crafted-event registry (pure data)
# --------------------------------------------------------------------------

#: The closed set of event kinds `wf test` can craft (PRD story 40): a push to
#: a branch, an opened pull request, a dispatch with inputs, and a direct
#: reusable-workflow call with inputs (what smokes shipit's own `wf-*` blocks,
#: ADR-0040 — act invokes the `workflow_call` workflow as the top level, so
#: the nested-plumbing caveat on the untestable surface still stands).
#: Extending it is a registry entry in :func:`craft_event`, never a
#: caller-side payload.
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

#: The event kinds whose crafted payload carries ``inputs`` — the only kinds
#: ``--input`` applies to (the verb refuses it elsewhere, never drops it).
INPUT_EVENT_KINDS: tuple[str, ...] = (EVENT_WORKFLOW_DISPATCH, EVENT_WORKFLOW_CALL)

#: The act runner image: shipit's stock-Ubuntu baseline (docs/dev/containers.md),
#: built locally from the packaged Dockerfile (:data:`WF_DOCKERFILE`) and PINNED
#: by tag — `act_argv` maps every ubuntu label to THIS image and passes
#: ``--pull=false``, so the job container is never a network pull and never
#: act's own default image choice.
WF_IMAGE = "shipit-wf-ubuntu:24.04"

#: The packaged Dockerfile the image is built from — the shipped copy of
#: ``docker/ubuntu.Dockerfile`` (byte-identical; tests pin the pair), resolved
#: via the same packaged-data path the lint gate's canonical configs use.
WF_DOCKERFILE = "ubuntu.Dockerfile"

#: Every runner label the pinned image stands in for. GitHub-hosted ubuntu
#: labels only: act runs linux containers, so macOS/Windows labels are part of
#: the untestable surface below, never silently mapped.
ACT_PLATFORMS: tuple[str, ...] = ("ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04")

#: Version of the untestable-surface statement below. Bump it when the list
#: changes, so "which notice did that run print?" stays answerable from logs.
UNTESTABLE_SURFACE_VERSION = 2

#: What act CANNOT verify (PRD story 41) — a FIXED, versioned statement printed
#: on EVERY run, green or red. A local green is trusted only where it is valid;
#: this list is the boundary, and printing it unconditionally is what stops the
#: harness from quietly overselling its coverage.
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

#: Each act Exec's stated timeout, in seconds (ADR-0028: every Exec states its
#: bound deliberately). A containerized job legitimately outlives the runner's
#: 5-minute default — a first `run:` step often apt-installs — so the bound is
#: doubled, stated on the wire rather than inherited.
ACT_TIMEOUT: float = 600.0

#: The docker image build's stated timeout: one apt-get layer over ubuntu:24.04,
#: network-bound on first build, a cache hit afterwards.
IMAGE_BUILD_TIMEOUT: float = 600.0


class RunCmd(Protocol):
    """The injectable Exec seam every act/docker invocation goes through."""

    def __call__(
        self, argv: list[str], *, timeout: float, check: bool = False
    ) -> execrun.ExecResult: ...


# --------------------------------------------------------------------------
# Pure cores — event crafting, job selection, argv encoding
# --------------------------------------------------------------------------


def parse_inputs(pairs: tuple[str, ...]) -> dict[str, str]:
    """``KEY=VALUE`` pairs → the dispatch-inputs mapping. Pure.

    Splits on the FIRST ``=`` so a value may itself carry one. A pair with no
    ``=`` or an empty key raises :class:`ValueError` (the verb maps it to the
    one ``error: …`` line) — a malformed input must never reach the payload as
    a silently-dropped or misparsed key.
    """
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
    """The crafted event payload for ``kind``. Pure.

    Minimal-but-real payloads: only the fields workflows actually branch on
    (``ref``, ``pull_request.head/base``, ``inputs``) are crafted; act
    synthesizes the repository plumbing around them. The set is CLOSED
    (:data:`EVENT_KINDS`) — an unknown kind is a :class:`ValueError`, though the
    CLI's ``click.Choice`` normally stops one earlier.

    ``branch`` is the push target for a push, the HEAD branch of the crafted
    (base ``main``) pull request, and the dispatch/call ref. ``inputs`` feed
    the input-bearing kinds alone (:data:`INPUT_EVENT_KINDS`); the CALLER
    enforces that scoping (the verb refuses ``--input`` on other kinds rather
    than dropping it silently).
    """
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
        # Same minimal shape for both: act reads the call/dispatch inputs off
        # the payload's `inputs` map and runs the workflow as the top level.
        return {
            "ref": f"refs/heads/{branch}",
            "inputs": dict(inputs or {}),
        }
    raise ValueError(f"unknown event kind {kind!r} (one of: {', '.join(EVENT_KINDS)})")


def workflow_jobs(text: str) -> list[str]:
    """The job ids a workflow body declares, in document order. Pure.

    The job-selection core behind ``--job``: the verb validates the selector
    against THIS list and hard-errors naming the available jobs on a miss
    (the Tool-verb selector rule, ADR-0039 — never a silent no-op run).
    Unparseable YAML or a workflow with no ``jobs:`` mapping raises
    :class:`ValueError` — a file that is not a workflow is a refusal, not an
    act invocation.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not parseable as workflow YAML: {exc}") from exc
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        raise ValueError("workflow declares no jobs (missing or empty `jobs:` map)")
    return [str(job) for job in jobs]


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
    """The act invocation for one workflow/job × crafted event. Pure.

    The encoding the verb tests pin through the recorded-argv seam: the event
    name is act's positional trigger; ``--workflows`` scopes act to the ONE
    file; ``--eventpath`` feeds the crafted payload; every ubuntu runner label
    is mapped to the pinned image (:data:`ACT_PLATFORMS` — one ``-P`` each) and
    ``--pull=false`` keeps act from pulling over the local build; ``--job``
    rides only when a selector was given. ``dry_run`` rides as act's ``-n``
    (plan + step walk, no containers) — the smoke mode for workflows whose
    real steps carry side effects (the release blocks: prepare pushes,
    publish publishes). Each ``local_repositories`` entry rides verbatim as
    ``--local-repository OWNER/REPO@REF=PATH``, resolving a remote
    reusable-workflow/action ref against a local tree — what lets the
    composed ``wf-release.yml`` chain (full ``@vN`` refs, the remote-ref
    scar) compile offline against the checkout under test.
    """
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
    """The act-untestable surface as one printable block. Pure.

    A FIXED, versioned statement (story 41) — the caller prints it verbatim on
    every run; tests assert its header is present in green AND red output.
    """
    lines = [
        f"act cannot verify (surface statement v{UNTESTABLE_SURFACE_VERSION}) — "
        "a green run here proves nothing about:"
    ]
    lines += [f"  - {item}" for item in UNTESTABLE_SURFACE]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The Exec boundary (injected in tests)
# --------------------------------------------------------------------------


def _run_cmd(
    argv: list[str], *, timeout: float, check: bool = False
) -> execrun.ExecResult:
    """Run ``argv`` through the one Exec runner (ADR-0028).

    ``check=False`` for act itself — its nonzero rc is the workflow's VERDICT,
    not a transport failure; ``check=True`` for the image build, whose failure
    is infrastructure and surfaces as :class:`~shipit.execrun.ExecError`. A
    missing ``act``/``docker`` binary raises the runner's launch-failure
    :class:`~shipit.execrun.ExecError` either way — the hard-fail, never a
    skip. No env scrub here: act legitimately reads ``DOCKER_HOST`` & co., and
    the workflow under test is not a hermetic lint verdict.
    """
    return execrun.run(argv, check=check, timeout=timeout)


def ensure_image(run_cmd: RunCmd) -> bool:
    """Make :data:`WF_IMAGE` exist locally; return True when it was built.

    Probe (``docker image inspect``) then build from the packaged Dockerfile —
    idempotent: a present image is a cheap probe hit, so repeated `wf test`
    runs never rebuild. The build context is the Dockerfile's own (data) dir;
    the Dockerfile COPYs nothing, so the context content is irrelevant.
    """
    probe = run_cmd(
        ["docker", "image", "inspect", WF_IMAGE],
        timeout=execrun.DEFAULT_TIMEOUT,
        check=False,
    )
    if probe.rc == 0:
        return False
    dockerfile = _data_path(WF_DOCKERFILE)
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


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def _fail(message: str) -> int:
    """One refusal line on stderr + the runtime-failure exit, lint-style.

    ``message`` is collapsed to a single line before printing: some refusals
    carry embedded newlines — a YAML parse error surfaced by
    :func:`workflow_jobs` tails multi-line parser context — and the
    ``wf test: …`` contract is ONE stderr line, matching
    :func:`~._errors.cli_errors`.
    """
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
    """Run ``workflow`` (or one ``job`` of it) under act against a crafted
    ``event``. Returns the uniform Tool exit: 0 clean, 1 failed verdict or
    refusal; a missing act/docker hard-fails via the Exec seam (never a skip).

    ``dry_run`` runs act in plan/dry-run mode (``-n``): the workflow is
    parsed, the trigger matched, expressions and the job graph evaluated, but
    no step executes — the smoke mode for side-effectful workflows (the wf-*
    release blocks). Because a dry run never instantiates the runner image,
    :func:`ensure_image` is skipped for it (act still needs a reachable
    daemon). ``local_repositories`` maps remote ``owner/repo@ref``
    workflow/action refs to local paths (see :func:`act_argv`).

    The act-untestable surface (:func:`untestable_notice`) is printed on EVERY
    completed run, green or red, before the verdict line.
    """
    started = time.monotonic()
    wf_path = Path(workflow)
    if not wf_path.is_file():
        return _fail(f"{workflow} is not a workflow file")
    if inputs and event not in INPUT_EVENT_KINDS:
        return _fail(
            f"--input only applies to --event {' / '.join(INPUT_EVENT_KINDS)} "
            f"(got --event {event})"
        )
    try:
        dispatch_inputs = parse_inputs(inputs)
        jobs = workflow_jobs(wf_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return _fail(str(exc))
    if job is not None and job not in jobs:
        # The Tool-verb selector rule (ADR-0039): a selector that matches
        # nothing is a hard error NAMING the valid selectors, never a no-op.
        return _fail(f"no job {job!r} in {workflow} — jobs: {', '.join(jobs)}")

    run_cmd = run_cmd or _run_cmd
    payload = craft_event(event, branch=branch, inputs=dispatch_inputs)

    print(f"wf test: {workflow} (event {event}" + (f", job {job})" if job else ")"))
    # A dry run evaluates the graph without instantiating containers, so the
    # runner image is never used — building it first is wasted work. (act's
    # dry run still needs a REACHABLE daemon, so the smoke tests keep their
    # docker-daemon skip; they just no longer need the image built.)
    if not dry_run and ensure_image(run_cmd):
        print(f"  built {WF_IMAGE} (stock-Ubuntu act runner image)")

    # The payload lives in a temp file only as long as act needs to read it.
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
    # Story 41: the boundary statement rides EVERY run, green or red.
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


# --------------------------------------------------------------------------
# CLI glue
# --------------------------------------------------------------------------


@click.group(name="wf")
def wf() -> None:
    """Workflow tools — validate GitHub Actions workflows locally."""


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
    """Run WORKFLOW under act in a container, against a crafted event.

    Validates a workflow edit locally before any push: the selected workflow
    (or one --job of it) runs under act in shipit's stock-Ubuntu container
    image, triggered by a crafted push / pull_request / workflow_dispatch /
    workflow_call payload. Every run prints the act-untestable surface — the part of CI only
    a real push can verify. Exits 0 on a green run, 1 on a failed one; a
    missing act or docker fails hard, it never skips.
    """
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
