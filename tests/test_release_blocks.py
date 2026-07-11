"""Drift guards + act smokes for the release workflow blocks (TOL02-WS06).

The published wf-* release surface (ADR-0040): four stage blocks —
wf-prepare, wf-build, wf-sign-mac, wf-publish — plus the composed
wf-release.yml chaining them via nested workflow_call. Two kinds of test:

- Structural drift guards (pure YAML reads, no act): the invariants a code
  review cannot be trusted to re-catch every time — the remote-ref scar
  (a `./` reusable-workflow ref compiles fine locally and startup-fails on
  EVERY consumer; actionlint passes the broken form, so a test is the only
  automated guard), the scar placements inside the blocks, the story-42
  check-name surface, the never-upload-target/ rule, and the pixi-pin
  lockstep across every block.

- act dry-run smokes (PRD story 41): each block runs under `shipit wf test`
  against a crafted workflow_call event in dry-run mode — parse, trigger
  match, expression/graph evaluation, matrix fan — because the real steps
  carry side effects (prepare pushes, publish publishes) and the mac signer
  leg has no linux analogue; both live on the printed untestable surface.
  The composed chain's smoke resolves its remote @v1 refs against THIS
  checkout (--local-repository) and scopes to the entry job: nested
  runtime-output plumbing is the printed workflow_call-fidelity hole.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from test_ci_workflow import _WORKFLOWS, _load

from shipit import checks
from shipit.verbs import wf

#: The published stage blocks and the composed chain (ADR-0040 names).
STAGE_BLOCKS = ("wf-prepare.yml", "wf-build.yml", "wf-sign-mac.yml", "wf-publish.yml")
COMPOSED = "wf-release.yml"

#: The story-42 check-name contract: block file -> its stable job ids, in
#: document order. ADP02 holds consumer branch protection stable against
#: these — a rename here is a breaking change, and this pin makes it loud.
STABLE_JOBS = {
    "wf-prepare.yml": ["prepare"],
    "wf-build.yml": ["build"],
    "wf-sign-mac.yml": ["sign"],
    "wf-publish.yml": ["assert", "publish"],
    "wf-release.yml": ["prepare", "build", "sign", "publish"],
}

#: A crafted plan matrix entry (the preflight MatrixEntry hand-off shape) on
#: a runner label act maps — the smokes' synthetic fan input.
_ENTRY = (
    '[{"artifact":"demo","platform":"linux-x86_64",'
    '"target":"x86_64-unknown-linux-gnu","runner":"ubuntu-latest",'
    '"sign":false,"ext_archive":".tar.gz","ext_bin":"","package_arch":"amd64"}]'
)
_STAGES = '["preflight","prepare","bundle","assert-bundle","publish"]'


def _steps(name: str, job: str) -> list[dict]:
    return _load(name)["jobs"][job]["steps"]


def _runs(steps: list[dict]) -> str:
    return "".join(step.get("run", "") for step in steps)


# --------------------------------------------------------------------------
# Structural drift guards
# --------------------------------------------------------------------------


def test_blocks_are_workflow_call_only():
    # The published blocks are reusable workflows, nothing else: no push/PR
    # trigger may sneak in and run a release block on shipit's own CI.
    for name in (*STAGE_BLOCKS, COMPOSED):
        doc = _load(name)
        assert checks.workflow_triggers(doc) == ["workflow_call"], name


def test_stable_job_names_hold_for_adp02():
    # PRD story 42: these job ids are the consumer-visible check-name
    # surface. Changing one breaks consumer branch protection — this pin
    # turns a casual rename into an explicit decision.
    for name, jobs in STABLE_JOBS.items():
        assert list(_load(name)["jobs"]) == jobs, name


def test_composed_chain_refs_are_remote_never_local():
    # THE REMOTE-REF SCAR (legacy release RC #823): a `./` reusable-workflow
    # ref resolves against the TOP-LEVEL caller's repo — the consumer — and
    # the whole graph startup-fails there with 0 jobs. actionlint passes the
    # broken form; this test is the automated guard.
    jobs = _load(COMPOSED)["jobs"]
    for job_id, job in jobs.items():
        ref = job.get("uses", "")
        assert ref.startswith("arthur-debert/shipit/.github/workflows/wf-"), (
            job_id,
            ref,
        )
        assert ref.endswith("@v1"), (job_id, ref)


def test_composed_chain_carries_zero_logic():
    # ADR-0040: the chain reads plan outputs and passes stage results
    # verbatim — the scar-#3 result expression must NOT reappear here (it
    # lives in the publish verb, fed by wf-publish's explicit inputs).
    jobs = _load(COMPOSED)["jobs"]
    publish = jobs["publish"]
    assert "needs.build.result ==" not in publish.get("if", "")
    assert "needs.sign.result ==" not in publish.get("if", "")
    # `!cancelled()` is a status function: it overrides the default
    # skip-when-a-need-failed/was-skipped, so publish still RUNS to reach the
    # unsigned path or the verb's scar-#3 refusal (a plain `needs:` would
    # wrongly skip it). Prepare-success is the only gate. `always()` would be
    # wrong here — it runs even on cancellation.
    assert "!cancelled()" in publish["if"]
    assert "needs.prepare.result == 'success'" in publish["if"]
    assert publish["with"]["build-result"] == "${{ needs.build.result }}"
    assert publish["with"]["bundle-result"] == "${{ needs.build.result }}"
    assert publish["with"]["sign-result"] == "${{ needs.sign.result }}"
    # The one sign decision is the plan's projection, read, never remade.
    assert jobs["sign"]["if"] == "needs.prepare.outputs.sign-matrix != '[]'"


def test_assert_bundle_runs_at_sign_entry_before_any_secret():
    # Scar #2, first placement (ADR-0040): the right-binary guard runs AT
    # wf-sign-mac's ENTRY — strictly before the signing step that imports
    # the Apple secrets.
    steps = _steps("wf-sign-mac.yml", "sign")
    order = [i for i, s in enumerate(steps) if "assert-bundle" in s.get("run", "")]
    sign = [i for i, s in enumerate(steps) if "release sign" in s.get("run", "")]
    assert order and sign and order[0] < sign[0]


def test_assert_bundle_guards_publishes_unsigned_path():
    # Scar #2, second placement (ADR-0040): wf-publish's assert job fans
    # over the plan's UNSIGNED projection — the trees that never traversed
    # wf-sign-mac — and the publish job is ordered after it.
    doc = _load("wf-publish.yml")
    assert_job = doc["jobs"]["assert"]
    assert "unsigned-matrix" in assert_job["strategy"]["matrix"]["include"]
    assert "assert-bundle" in _runs(assert_job["steps"])
    publish = doc["jobs"]["publish"]
    assert publish["needs"] == ["assert"]
    # `!cancelled()` overrides the default skip so publish still RUNS when the
    # assert job legitimately skips (all-signed plan / no bundle stage); the
    # result gate below then admits skipped-or-success and blocks failure.
    assert "!cancelled()" in publish["if"]
    assert "needs.assert.result != 'failure'" in publish["if"]


def test_publish_block_feeds_results_to_the_verb_not_yaml():
    # Scar #3 lives in the verb (ADR-0009/0040): the block hands the three
    # stage results to `shipit release publish` verbatim and its own `if:`
    # never re-derives the gate from them.
    doc = _load("wf-publish.yml")
    publish = doc["jobs"]["publish"]
    script = _runs(publish["steps"])
    for flag in ("--build-result", "--bundle-result", "--sign-result"):
        assert flag in script
    assert "inputs.build-result" not in publish["if"]
    assert "inputs.sign-result" not in publish["if"]


def test_prepare_pipeline_steps_set_pipefail():
    # The plan and prepare steps pipe a shipit invocation into jq. The default
    # step shell is `bash -e` WITHOUT pipefail, so a failed producer would be
    # masked by jq's 0 on empty input — the step would emit blank outputs and
    # let a release the plan never sanctioned proceed. Every run step that
    # pipes into jq must set pipefail so the whole pipeline fails.
    steps = _steps("wf-prepare.yml", "prepare")
    piped = [s for s in steps if "| jq -c" in s.get("run", "")]
    assert piped, "expected the plan/prepare steps to pipe into jq"
    for step in piped:
        assert "set -euo pipefail" in step["run"], step.get("name")


def test_build_block_never_ships_the_target_tree():
    # workflows.lex §1: the cross-job artifact is the staged dist tree,
    # never the multi-GB target/ tree; the unsigned .app directory stays on
    # the runner (upload destroys symlinks/exec bits — the reseal payload
    # carries it, §3.1).
    steps = _steps("wf-build.yml", "build")
    uploads = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    assert len(uploads) == 1
    path = uploads[0]["with"]["path"]
    assert "target" not in path
    assert path.splitlines()[0].strip() == "dist/**"
    assert "!dist/**/*.app/**" in path


def test_pixi_pin_is_lockstep_across_all_blocks():
    # The wf-checks.yml pin is the one test_install.py locks to the Layer 0
    # bootstrap; every release block must ride the SAME pin so a bump is
    # one sweep, never a partial drift.
    reference = None
    for name in ("wf-checks.yml", *STAGE_BLOCKS):
        pins = {
            step["with"]["pixi-version"]
            for job in _load(name)["jobs"].values()
            for step in job.get("steps", [])
            if "setup-pixi" in step.get("uses", "")
        }
        assert len(pins) == 1, name
        reference = reference or pins
        assert pins == reference, name


def test_advance_major_moves_the_floating_branch_on_stable_tags_only():
    # The @vN propagation path (issue #564 AC: decision recorded — a
    # BRANCH, force-moved with lease, prereleases never advance it).
    doc = _load("advance-major.yml")
    assert checks.workflow_triggers(doc) == ["push"]
    script = _runs(doc["jobs"]["advance"]["steps"])
    assert "git branch -f" in script
    assert "--force-with-lease" in script
    assert "*-*)" in script  # the prerelease skip


# --------------------------------------------------------------------------
# act dry-run smokes (story 41)
# --------------------------------------------------------------------------


def _docker_daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


_SMOKES: dict[str, dict] = {
    "wf-prepare.yml": {"inputs": ("version=1.2.3",)},
    "wf-build.yml": {
        "inputs": (
            "version=1.2.3",
            "tag=v1.2.3",
            f"matrix={_ENTRY}",
            f"stages={_STAGES}",
        ),
    },
    # The crafted entry rides an act-mapped ubuntu label: the smoke walks
    # the block's routing; the REAL mac leg (macos runner, keychain,
    # codesign/notarytool) is the printed untestable hole.
    "wf-sign-mac.yml": {"inputs": ("tag=v1.2.3", f"sign-matrix={_ENTRY}")},
    "wf-publish.yml": {
        "inputs": (
            "version=1.2.3",
            "tag=v1.2.3",
            "build-result=success",
            "bundle-result=success",
            "sign-result=skipped",
            f"unsigned-matrix={_ENTRY}",
            f"stages={_STAGES}",
        ),
    },
    # The chain's remote @v1 refs resolve against THIS checkout; the smoke
    # scopes to the entry job because nested workflow outputs only exist at
    # runtime — the printed workflow_call-fidelity hole covers the rest.
    "wf-release.yml": {"inputs": ("version=1.2.3",), "job": "prepare", "local": True},
}


@pytest.mark.skipif(shutil.which("act") is None, reason="act not on PATH")
@pytest.mark.skipif(not _docker_daemon_up(), reason="docker daemon unavailable")
@pytest.mark.parametrize("name", sorted(_SMOKES))
def test_block_smokes_green_under_act_dry_run(name, monkeypatch, capsys):
    # One act dry-run per block against a crafted workflow_call event:
    # parse, trigger match, graph + expression evaluation, matrix fan — and
    # the untestable-surface statement rides the output (story 41).
    spec = _SMOKES[name]
    root = _WORKFLOWS.parents[1]
    monkeypatch.chdir(root)
    local = (f"arthur-debert/shipit@v1={root}",) if spec.get("local") else ()
    rc = wf.run(
        f".github/workflows/{name}",
        event=wf.EVENT_WORKFLOW_CALL,
        inputs=spec["inputs"],
        job=spec.get("job"),
        dry_run=True,
        local_repositories=local,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WF TEST: OK" in out
    assert "act cannot verify" in out
