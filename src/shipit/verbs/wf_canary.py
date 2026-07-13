"""wf verify-canary — the standing sign e2e, dispatched at will (#899).

``shipit wf verify-canary`` drives shipit-canary's blessed stage-choice
caller (workflows.lex §8, the shipit-release.yml shape) through the FULL
sign matrix of proofs on live GitHub, and watches every run to its verdict:

- ``full`` — one ``stage=full`` rc: the composed chain including
  sign+notarize on a REAL macOS runner with the REAL fleet cert (the
  #873/#889 class: codesign identity resolution against a temp keychain —
  invisible to every unit test, because it needs the runner + cert).
- ``staged`` — ``prepare`` → ``build`` → ``sign`` → ``publish`` as four
  standalone dispatches, threading ``tag``/``run-id`` between them exactly
  as workflows.lex §8 prescribes (sign feeds off the build run; publish
  names the SIGN run, which carried the base families): the REAL cross-run
  artifact hand-off — the #898 regression surface, where a standalone sign
  dispatch died relaying ``release-notes``.

Why it exists (the owner directive behind #899): ``tests/test_release_sign.py``
carries 70 unit tests and both live sign-path failures of the rollout were
invisible to all of them, because the load-bearing facts only exist against
real infrastructure. The canary chain run is the guard that proves sign
changes BEFORE a consumer rc discovers them live; the runbook rule
(workflows.lex §9) makes citing these runs mandatory for sign/relay/wf-yml
PRs.

Siting — why this is a live-GitHub dispatcher and NOT a test check: it
dispatches real workflow runs (minutes to tens of minutes each, a real macOS
runner, real Apple notarization) against the canary repo, so it must never
run inside ``pixi run test`` / CI. It is an operator verb: explicit
invocation, tag-stamped rc's, and a printed teardown block (canary rc's are
torn down AFTER inspection, like every proof — the verb prints the exact
commands, it never auto-deletes what you have not yet inspected).

The pure cores — the per-mode version derivation (:func:`mode_versions`),
the stage-input threading table (:func:`stage_inputs` / :data:`RELAY_SOURCE`),
new-run discovery (:func:`new_run`) and the proof / teardown renderings —
are kept out of the GitHub boundary so they are fixture-tested with no
network anywhere near the tests, the same split ``wf test`` uses. Every gh
exchange rides the ONE gh Tool adapter (ADR-0028): :func:`shipit.gh.workflow_run`
dispatches, :func:`shipit.gh.run_list_dispatched` discovers the minted run,
:func:`shipit.gh.run_verdict` follows it — verb tests fake exactly those
three adapter calls, and the clock rides the ``sleep``/``monotonic`` seam
``pr wait`` established (ADR-0034).

Exit semantics are the uniform Tool contract: ``0`` when every dispatched
chain ran green, ``1`` a failed verdict (a red or timed-out run, a relay
stage skipped because its upstream failed), and a failed ``gh`` exec (missing
binary, unknown workflow, auth) is the standard HARD-fail through the Exec
runner's :class:`~shipit.execrun.ExecError` — never a silent skip.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import click

from .. import gh
from ._errors import cli_errors

logger = logging.getLogger("shipit.wf")

# --------------------------------------------------------------------------
# The dispatch surface (pure data)
# --------------------------------------------------------------------------

#: The standing ADP00 probe — the ONE repo this verb exists for. Overridable
#: (``--repo``) only so the harness can be pointed at a fork while the canary
#: itself is being provisioned; the proof the runbook accepts is the canary's.
CANARY_REPO = "arthur-debert/shipit-canary"

#: The canary's blessed stage-choice caller file — the same name shipit's own
#: dogfood caller wears (workflows.lex §8; the shape `shipit wf test` lints).
CALLER_WORKFLOW = "shipit-release.yml"

#: The proof modes: the composed chain, the standalone-dispatch relay, or
#: both (the default — the runbook rule requires BOTH cited green).
MODE_FULL = "full"
MODE_STAGED = "staged"
MODE_BOTH = "both"
MODES: tuple[str, ...] = (MODE_FULL, MODE_STAGED, MODE_BOTH)

#: The staged relay, in dispatch order — the four standalone stage dispatches
#: of workflows.lex §8 (the #898 regression surface).
RELAY_ORDER: tuple[str, ...] = ("prepare", "build", "sign", "publish")

#: Stage → the relay stage whose RUN feeds it as ``run-id`` (``None``: the
#: stage consumes no prior run). Sign's source is the build run (the bundles);
#: publish names the SIGN run — a standalone sign makes its OWN run a complete
#: publish source by carrying the base families forward (workflows.lex §8's
#: one-source-run rule), so naming the build run instead would publish the
#: unsigned bundles.
RELAY_SOURCE: dict[str, str | None] = {
    "prepare": None,
    "build": None,
    "sign": "build",
    "publish": "sign",
}

#: How long a dispatched run may take to APPEAR in the run list before the
#: chain is called failed (GitHub registers dispatch runs asynchronously).
DISPATCH_TIMEOUT: float = 300.0

#: Poll cadence while waiting for the dispatched run to appear.
DISPATCH_POLL_SECONDS: float = 5.0

#: How long one run may take to COMPLETE. Generous on purpose: the full
#: composed chain queues a macOS runner and waits on Apple notarization.
RUN_TIMEOUT: float = 3600.0

#: Poll cadence while waiting for a started run's verdict.
RUN_POLL_SECONDS: float = 30.0


# --------------------------------------------------------------------------
# Pure cores — versions, input threading, run discovery
# --------------------------------------------------------------------------


def tag_for(version: str) -> str:
    """The release tag a version cuts — ``v<version>`` (ADR-0041). Pure."""
    return f"v{version}"


def mode_versions(version: str, mode: str) -> dict[str, str]:
    """Mode → the rc version that mode's chain cuts. Pure.

    A single mode uses ``version`` verbatim. ``both`` needs TWO distinct
    versions — the staged relay's ``prepare`` creates its tag fresh, and the
    full chain already created ``v<version>`` — so each mode gets a semver
    prerelease suffix: ``1.2.3`` → ``1.2.3-full`` / ``1.2.3-staged``, and a
    version already carrying a prerelease extends it with a dot identifier
    (``1.2.3-rc`` → ``1.2.3-rc.full``), keeping both valid semver.
    """
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
    """The blessed caller's dispatch inputs for one stage. Pure.

    The workflows.lex §8 aligned stage-input contract, as the dispatcher
    threads it: ``full``/``prepare`` ride ``version`` (they create the tag);
    ``build`` rides ``tag`` alone; the artifact-consuming stages (``sign``,
    ``publish``) ride ``tag`` + ``run-id`` — the SOURCE run named by
    :data:`RELAY_SOURCE`, looked up in ``run_ids`` (stage → completed run id;
    a missing source is a caller bug and raises KeyError loudly).
    """
    if stage in (MODE_FULL, "prepare"):
        return {"stage": stage, "version": version}
    inputs = {"stage": stage, "tag": tag_for(version)}
    source = RELAY_SOURCE[stage]
    if source is not None:
        inputs["run-id"] = str((run_ids or {})[source])
    return inputs


def new_run(runs: list[dict], baseline: frozenset[int] | set[int]) -> dict | None:
    """The freshly-dispatched run: the newest listed run NOT in ``baseline``.

    ``None`` while GitHub has not registered it yet (the caller keeps
    polling). Two new runs at once (someone else dispatched the canary
    concurrently) resolve to the newest — acceptable on a single-operator
    probe repo, and the printed run URL makes a mix-up visible. Pure.
    """
    fresh = [r for r in runs if r.get("databaseId") not in baseline]
    if not fresh:
        return None
    return max(fresh, key=lambda r: r["databaseId"])


# --------------------------------------------------------------------------
# Results and renderings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainStep:
    """One dispatched (or refused) step of a proof chain: which ``mode`` and
    ``stage``, the rc ``version`` it rode, and the observed ``run_id`` /
    ``url`` / ``conclusion`` (``run_id`` ``None`` when the step never
    dispatched — an upstream relay failure skipped it)."""

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
    """The citation block a sign/relay/wf-yml PR pastes (workflows.lex §9).

    One line per step: mode/stage, the rc version, the verdict, the run URL —
    the runbook's required evidence, rendered so it can be cited verbatim.
    Pure.
    """
    lines = ["CANARY PROOF (cite on any shipit PR touching sign/relay/wf yml):"]
    for step in steps:
        label = step.mode if step.stage == MODE_FULL else f"{step.mode}/{step.stage}"
        url = step.url or "(no run)"
        lines.append(
            f"  {label:<16} {tag_for(step.version):<24} {step.conclusion:<10} {url}"
        )
    return "\n".join(lines)


def teardown_block(repo: str, versions: dict[str, str]) -> str:
    """The teardown commands — canary rc's are torn down AFTER inspection
    (#899 discipline), so the verb prints them instead of running them. Pure.
    """
    lines = ["teardown (after inspection — canary rc's never linger):"]
    for version in versions.values():
        lines.append(
            f"  gh release delete {tag_for(version)} -R {repo} --yes --cleanup-tag"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The dispatch-and-watch loop (gh boundary: the shipit.gh adapter, ADR-0028)
# --------------------------------------------------------------------------


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
    """Dispatch one caller stage and watch its run to a verdict.

    Snapshot the run-id baseline, dispatch, poll the list until the new run
    appears (:data:`DISPATCH_TIMEOUT`), then poll the run until it completes
    (:data:`RUN_TIMEOUT`). A timeout at either wait is a FAILED verdict
    (conclusion ``dispatch-timeout`` / ``watch-timeout``), not an exception:
    the operator inspects, tears down, re-runs.
    """
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


# --------------------------------------------------------------------------
# The verb
# --------------------------------------------------------------------------


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
    """Dispatch the selected proof chains on the canary and report.

    ``full`` runs one ``stage=full`` composed-chain rc; ``staged`` runs the
    four-dispatch relay, stopping at the first red stage (a later stage
    dispatched against a failed source would prove nothing — the un-dispatched
    remainder is recorded ``skipped``); ``both`` (default) runs full first,
    then the relay, each on its own derived rc version
    (:func:`mode_versions`). Prints the proof-citation block and the teardown
    commands either way, and returns ``0`` only when EVERY step ran green.
    """
    started = time.monotonic()
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
            "duration_ms": int((time.monotonic() - started) * 1000),
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
        "chain (`.full` / `.staged`)."
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
    """Dispatch the canary's sign-proof chains on live GitHub and watch them.

    The standing sign e2e (#899): drives shipit-canary's blessed stage-choice
    caller through the composed `full` chain (sign+notarize on a real macOS
    runner) and/or the standalone-dispatch relay (prepare, build, sign,
    publish — the real cross-run artifact hand-off), waits for every run's
    verdict, prints the proof-citation block the sign runbook requires
    (workflows.lex §9) plus the teardown commands, and exits 0 only when
    every run is green. Live GitHub, real runs, real minutes: an operator
    verb, never part of `pixi run test`.
    """
    raise SystemExit(run(version, mode=mode, repo=repo, workflow=workflow, ref=ref))
