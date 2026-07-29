"""wf verify-canary — dispatch the canary's sign-proof chains on live GitHub and watch them; an operator verb, never part of ``pixi run test``."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import click

from .. import checks, gh
from ._errors import cli_errors

logger = logging.getLogger("shipit.wf")


CANARY_REPO = "arthur-debert/shipit-canary"

CALLER_WORKFLOW = checks.RELEASE_CALLER_WORKFLOW

MODE_FULL = "full"
MODE_STAGED = "staged"
MODE_BOTH = "both"
MODES: tuple[str, ...] = (MODE_FULL, MODE_STAGED, MODE_BOTH)

RELAY_ORDER: tuple[str, ...] = ("prepare", "build", "sign", "publish")

RELAY_SOURCE: dict[str, str | None] = {
    "prepare": None,
    "build": None,
    "sign": "build",
    "publish": "sign",
}

DISPATCH_TIMEOUT: float = 300.0

DISPATCH_POLL_SECONDS: float = 5.0

RUN_TIMEOUT: float = 3600.0

RUN_POLL_SECONDS: float = 30.0


def tag_for(version: str) -> str:
    """The release tag a version cuts — ``v<version>``."""
    return f"v{version}"


def mode_versions(version: str, mode: str) -> dict[str, str]:
    """Mode → the rc version that mode's chain cuts; ``both`` suffixes each with a semver prerelease identifier so the two chains cut distinct tags."""
    if mode != MODE_BOTH:
        return {mode: version}
    sep = "." if "-" in version else "-"
    return {
        MODE_FULL: f"{version}{sep}full",
        MODE_STAGED: f"{version}{sep}staged",
    }


def stage_inputs(
    stage: str, *, version: str, run_ids: dict[str, int] | None = None
) -> dict[str, str]:
    """The blessed caller's dispatch inputs for one stage; a missing relay source run raises KeyError."""
    if stage in (MODE_FULL, "prepare"):
        return {"stage": stage, "version": version}
    inputs = {"stage": stage, "tag": tag_for(version)}
    source = RELAY_SOURCE[stage]
    if source is not None:
        inputs["run-id"] = str((run_ids or {})[source])
    return inputs


def new_run(runs: list[dict], baseline: frozenset[int] | set[int]) -> dict | None:
    """The newest listed run not in ``baseline``, or None while GitHub has not registered it yet."""
    fresh = [r for r in runs if r.get("databaseId") not in baseline]
    if not fresh:
        return None
    return max(fresh, key=lambda r: r["databaseId"])


@dataclass(frozen=True)
class ChainStep:
    """One dispatched (or refused) step of a proof chain; ``run_id`` is None when the step never dispatched."""

    mode: str
    stage: str
    version: str
    run_id: int | None
    url: str
    conclusion: str

    @property
    def passed(self) -> bool:
        """True only for a completed, green run."""
        return self.conclusion == "success"


def proof_block(steps: list[ChainStep]) -> str:
    """The citation block a sign/relay/wf-yml PR pastes: one line per step."""
    lines = ["CANARY PROOF (cite on any shipit PR touching sign/relay/wf yml):"]
    for step in steps:
        label = step.mode if step.stage == MODE_FULL else f"{step.mode}/{step.stage}"
        url = step.url or "(no run)"
        lines.append(
            f"  {label:<16} {tag_for(step.version):<24} {step.conclusion:<10} {url}"
        )
    return "\n".join(lines)


def teardown_block(repo: str, versions: dict[str, str]) -> str:
    """The teardown commands, printed rather than run so rc's are torn down after inspection."""
    lines = ["teardown (after inspection — canary rc's never linger):"]
    for version in versions.values():
        lines.append(
            f"  gh release delete {tag_for(version)} -R {repo} --yes --cleanup-tag"
        )
    return "\n".join(lines)


def _dispatch_and_watch(
    *,
    repo: str,
    workflow: str,
    ref: str,
    mode: str,
    stage: str,
    version: str,
    run_ids: dict[str, int],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> ChainStep:
    """Dispatch one caller stage and watch its run to a verdict; a timeout at either wait is a failed verdict, not an exception."""
    inputs = stage_inputs(stage, version=version, run_ids=run_ids)
    baseline = {r.get("databaseId") for r in gh.run_list_dispatched(repo, workflow)}
    gh.workflow_run(workflow, repo=repo, ref=ref, fields=inputs)
    rendered = " ".join(f"{k}={v}" for k, v in inputs.items())
    print(f"  dispatched {rendered}", flush=True)

    deadline = monotonic() + DISPATCH_TIMEOUT
    run: dict | None = None
    while run is None:
        if monotonic() >= deadline:
            return ChainStep(mode, stage, version, None, "", "dispatch-timeout")
        sleep(DISPATCH_POLL_SECONDS)
        run = new_run(gh.run_list_dispatched(repo, workflow), baseline)
    run_id = int(run["databaseId"])
    url = str(run.get("url") or "")
    print(f"  run {run_id} started: {url}", flush=True)

    deadline = monotonic() + RUN_TIMEOUT
    while True:
        doc = gh.run_verdict(repo, run_id)
        url = str(doc.get("url") or url)
        if doc.get("status") == "completed":
            conclusion = str(doc.get("conclusion") or "unknown")
            print(f"  run {run_id}: {conclusion}", flush=True)
            return ChainStep(mode, stage, version, run_id, url, conclusion)
        if monotonic() >= deadline:
            print(f"  run {run_id}: still running at deadline", flush=True)
            return ChainStep(mode, stage, version, run_id, url, "watch-timeout")
        sleep(RUN_POLL_SECONDS)


@cli_errors
def run(
    version: str,
    *,
    mode: str = MODE_BOTH,
    repo: str = CANARY_REPO,
    workflow: str = CALLER_WORKFLOW,
    ref: str = "main",
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Dispatch the selected proof chains on the canary, print the proof and teardown blocks, and return 0 only when every step ran green."""
    started = monotonic()
    versions = mode_versions(version, mode)
    print(f"wf verify-canary: {repo} {workflow} (mode {mode})")

    steps: list[ChainStep] = []
    for chain_mode, chain_version in versions.items():
        print(f"== {chain_mode}: version {chain_version} ==")
        stages = (MODE_FULL,) if chain_mode == MODE_FULL else RELAY_ORDER
        run_ids: dict[str, int] = {}
        failed = False
        for stage in stages:
            if failed:
                steps.append(
                    ChainStep(chain_mode, stage, chain_version, None, "", "skipped")
                )
                continue
            step = _dispatch_and_watch(
                repo=repo,
                workflow=workflow,
                ref=ref,
                mode=chain_mode,
                stage=stage,
                version=chain_version,
                run_ids=run_ids,
                sleep=sleep,
                monotonic=monotonic,
            )
            steps.append(step)
            if step.run_id is not None:
                run_ids[stage] = step.run_id
            failed = not step.passed

    print(proof_block(steps))
    print(teardown_block(repo, versions))
    rc = 0 if all(step.passed for step in steps) else 1
    if rc == 0:
        print(f"WF VERIFY-CANARY: OK ({len(steps)} run(s) green)")
    else:
        red = [s for s in steps if not s.passed]
        summary = ", ".join(f"{s.mode}/{s.stage}={s.conclusion}" for s in red)
        print(f"WF VERIFY-CANARY: FAILED ({summary})")
    logger.info(
        "wf verify-canary complete",
        extra={
            "repo": repo,
            "mode": mode,
            "rc": rc,
            "steps": len(steps),
            "duration_ms": int((monotonic() - started) * 1000),
        },
    )
    return rc


@click.command(name="verify-canary")
@click.option(
    "--version",
    required=True,
    help=(
        "The rc version the proof chains cut (bare semver, e.g. "
        "0.0.7-canary-rc). Mode `both` derives a distinct sub-version per "
        "chain: a plain version gets `-full` / `-staged`; a version already "
        "carrying a prerelease extends it with `.full` / `.staged`."
    ),
)
@click.option(
    "--mode",
    type=click.Choice(MODES),
    default=MODE_BOTH,
    show_default=True,
    help=(
        "Which proof to dispatch: the composed `full` chain, the four-"
        "dispatch `staged` relay (the #898 surface), or `both`."
    ),
)
@click.option("--repo", default=CANARY_REPO, show_default=True, help="The canary repo.")
@click.option(
    "--workflow",
    default=CALLER_WORKFLOW,
    show_default=True,
    help="The canary's blessed stage-choice caller workflow file.",
)
@click.option(
    "--ref",
    default="main",
    show_default=True,
    help="The git ref the caller dispatches on.",
)
def verify_canary_cmd(
    version: str, mode: str, repo: str, workflow: str, ref: str
) -> None:
    """Dispatch the canary's sign-proof chains on live GitHub and watch them."""
    raise SystemExit(run(version, mode=mode, repo=repo, workflow=workflow, ref=ref))
