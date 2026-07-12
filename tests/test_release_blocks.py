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

import json
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
#: The `plan` jobs joined in TOL02-WS09 (#780): the standalone
#: re-derivation, a skipped no-op on every fact-supplied (composed-chain)
#: run — additive, so existing required checks were untouched. The
#: `carry-bundles`/`carry-notes` jobs joined too (#780): standalone-only
#: base-artifact carry-forward, likewise skipped no-ops on the composed
#: chain, so likewise additive.
STABLE_JOBS = {
    "wf-prepare.yml": ["prepare"],
    "wf-build.yml": ["plan", "build"],
    "wf-sign-mac.yml": ["plan", "sign", "carry-bundles", "carry-notes"],
    "wf-publish.yml": ["plan", "assert", "publish"],
    "wf-release.yml": ["prepare", "build", "sign", "publish"],
}

#: Per-stage dispatch (#780): block file -> the input whose omission turns
#: the block's `plan` job on (the standalone-mode discriminator). Each is
#: the fact the composed chain always supplies, so the chain never pays for
#: a re-derivation.
PLAN_DISCRIMINATOR = {
    "wf-build.yml": "matrix",
    "wf-sign-mac.yml": "sign-matrix",
    "wf-publish.yml": "stages",
}

#: A crafted plan matrix entry (the preflight MatrixEntry hand-off shape) on
#: a runner label act maps — the smokes' synthetic fan input.
_ENTRY = (
    '[{"artifact":"demo","platform":"linux-x86_64",'
    '"target":"x86_64-unknown-linux-gnu","runner":"ubuntu-latest",'
    '"sign":false,"bundle":true,'
    '"ext_archive":".tar.gz","ext_bin":"","package_arch":"amd64"}]'
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
    # The liveness facts ride VERBATIM (issue #745): the chain never
    # translates a skipped result nor computes `matrix != '[]'` itself —
    # the verb derives the empty-matrix verdict from the plan facts.
    assert publish["with"]["matrix"] == "${{ needs.prepare.outputs.matrix }}"
    assert publish["with"]["stages"] == "${{ needs.prepare.outputs.stages }}"
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
    assert publish["needs"] == ["plan", "assert"]
    # `!cancelled()` overrides the default skip so publish still RUNS when the
    # assert job legitimately skips (all-signed plan / no bundle stage); the
    # result gate below then admits skipped-or-success and blocks failure.
    assert "!cancelled()" in publish["if"]
    assert "needs.assert.result != 'failure'" in publish["if"]


def test_publish_block_feeds_results_to_the_verb_not_yaml():
    # Scar #3 lives in the verb (ADR-0009/0040): the block hands the three
    # stage results AND the plan's liveness facts (issue #745 — matrix and
    # stages, verbatim) to `shipit release publish`, and its own `if:` never
    # re-derives the gate from them.
    doc = _load("wf-publish.yml")
    publish = doc["jobs"]["publish"]
    script = _runs(publish["steps"])
    for flag in (
        "--build-result",
        "--bundle-result",
        "--sign-result",
        "--matrix",
        "--stages",
    ):
        assert flag in script
    assert "inputs.build-result" not in publish["if"]
    assert "inputs.sign-result" not in publish["if"]
    assert "inputs.matrix" not in publish["if"]
    # The facts reach the verb untranslated: env passthrough only. Since
    # #780 each coalesces input-or-plan-output (`||` routes between the two
    # sources of the SAME fact — the standalone plan job re-derived it via
    # the same planner); whichever source supplied it rides verbatim.
    step = next(s for s in publish["steps"] if "release publish" in s.get("run", ""))
    assert step["env"]["MATRIX"] == "${{ inputs.matrix || needs.plan.outputs.matrix }}"
    assert step["env"]["STAGES"] == "${{ inputs.stages || needs.plan.outputs.stages }}"
    # Direct wf-publish@v1 callers predate the matrix fact. Omitting it must
    # still compile and must omit the CLI flag, preserving the verb's strict
    # success-only default instead of passing an invalid empty JSON string.
    matrix = doc["on"]["workflow_call"]["inputs"]["matrix"]
    assert matrix["required"] is False
    assert 'if [[ -n "$MATRIX" ]]' in script
    assert 'args+=(--matrix "$MATRIX")' in script


def test_publish_block_declares_feeds_and_forwards_every_endpoint_token():
    # ADR-0040 routing: the publish block passes each endpoint's token to the
    # verb, which validates the plan-required subset. The token set IS
    # secretreq.ENDPOINT_SECRETS (gh-release declares none — ambient token), so
    # a new endpoint (notify-downstreams #792) cannot land its adapter without
    # wiring its token here, in the block AND the composed chain's forward.
    from shipit.release import secretreq

    endpoint_tokens = {
        name for names in secretreq.ENDPOINT_SECRETS.values() for name in names
    }
    assert "DOWNSTREAM_DISPATCH_TOKEN" in endpoint_tokens  # the #792 addition

    declared = _load("wf-publish.yml")["on"]["workflow_call"]["secrets"]
    assert endpoint_tokens <= set(declared)
    assert all(not declared[name].get("required", False) for name in endpoint_tokens)
    # The publish step reads each as a same-named env var …
    publish = _load("wf-publish.yml")["jobs"]["publish"]
    step = next(s for s in publish["steps"] if "release publish" in s.get("run", ""))
    for name in sorted(endpoint_tokens):
        assert step["env"][name] == f"${{{{ secrets.{name} }}}}"
    # … and the composed chain forwards each to the publish job.
    forwarded = _load(COMPOSED)["jobs"]["publish"]["secrets"]
    for name in sorted(endpoint_tokens):
        assert forwarded[name] == f"${{{{ secrets.{name} }}}}"


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


def test_build_block_bundles_only_its_matrix_entry_artifact():
    # The per-entry narrowing contract (TOL02-WS07's lex rc finding): the
    # matrix is one artifact × platform per entry, and wf-publish's assert
    # job inspects `bundle-<artifact>-<platform>` PER ARTIFACT — a
    # whole-map `release bundle` would put every artifact's binary in every
    # entry's tree and fail assert-bundle on any multi-artifact repo. The
    # bundle step must pass its entry's artifact through to the verb.
    steps = _steps("wf-build.yml", "build")
    bundle = next(s for s in steps if "release bundle" in s.get("run", ""))
    assert '--artifact "$ARTIFACT"' in bundle["run"]
    assert bundle["env"]["ARTIFACT"] == "${{ matrix.artifact }}"


def test_build_block_gates_bundle_on_the_per_entry_flag_not_the_stage():
    # The mixed-map fix (codex, round 1): the bundle stage is a plan-WIDE
    # flag, but the fan includes every build-bearing artifact whether or not
    # it bundles. Gating bundle/upload on the plan-wide stage would send a
    # build-only artifact's leg through a passthrough that stages nothing,
    # then trip the upload's `if-no-files-found: error`. Both steps gate on
    # the per-entry `matrix.bundle` decision instead.
    steps = _steps("wf-build.yml", "build")
    bundle = next(s for s in steps if "release bundle" in s.get("run", ""))
    upload = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert bundle["if"] == "matrix.bundle"
    assert upload["if"] == "matrix.bundle"


def test_sign_chain_declares_and_forwards_both_notary_trios():
    # #746: the Apple-ID trio is a first-class CI notary path beside the ASC
    # trio. Every hop of the chain must ACCEPT either path — wf-sign-mac
    # (the consumer) and wf-prepare (preflight's presence env) declare the
    # full mac signing surface as optional secrets, and wf-release forwards
    # it verbatim to both. All optional: WHICH trio must be complete is the
    # plan's/verb's decision, never YAML's (ADR-0040).
    from shipit.release import secretreq

    mac_names = {
        *secretreq.SIGN_MAC_CERT_SECRETS,
        *secretreq.NOTARY_SECRETS.names(),
    }

    for block in ("wf-sign-mac.yml", "wf-prepare.yml", "wf-release.yml"):
        declared = _load(block)["on"]["workflow_call"]["secrets"]
        assert mac_names <= set(declared), block
        assert all(
            not spec.get("required", False)
            for name, spec in declared.items()
            if name in mac_names
        ), block

    # wf-sign-mac's sign step reads every name as a same-named env var …
    sign_step = next(
        s
        for s in _steps("wf-sign-mac.yml", "sign")
        if "release sign" in s.get("run", "")
    )
    for name in sorted(mac_names):
        assert sign_step["env"][name] == f"${{{{ secrets.{name} }}}}"
    # … wf-prepare injects them for preflight's presence validation …
    plan_step = next(
        s for s in _steps("wf-prepare.yml", "prepare") if s.get("id") == "plan"
    )
    for name in sorted(mac_names):
        assert plan_step["env"][name] == f"${{{{ secrets.{name} }}}}"
    # … and the composed chain forwards them to prepare and sign.
    jobs = _load(COMPOSED)["jobs"]
    for job_id in ("prepare", "sign"):
        forwarded = jobs[job_id]["secrets"]
        for name in sorted(mac_names):
            assert forwarded[name] == f"${{{{ secrets.{name} }}}}", job_id


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


#: Fleet-standard majors for the first-party actions (issue #761). Every
#: entry is the action's current node24 major — checkout v6 is the fleet
#: standard; upload v6 / download v7 were each family's node24 flip, and
#: the pins ride the current major above that (upload v7, download v8).
#: Exact single pins (not minimums) so a bump is one sweep, never a drift.
FIRST_PARTY_ACTION_PINS = {
    "actions/checkout": "actions/checkout@v6",
    "actions/upload-artifact": "actions/upload-artifact@v7",
    "actions/download-artifact": "actions/download-artifact@v8",
}


def test_first_party_action_pins_are_node24_and_lockstep_everywhere():
    # Issue #761: the wf-* blocks are the fleet's single source of action
    # pins — a node20-deprecated major here re-warns on EVERY @v1 consumer
    # run, undoing cutovers consumers already made. Sweep ALL of shipit's
    # workflows (blocks, ci.yml, advance-major.yml) against the pin table.
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        for job_id, job in _load(path.name)["jobs"].items():
            for step in job.get("steps", []):
                uses = step.get("uses") or ""
                action = uses.split("@", 1)[0]
                if action in FIRST_PARTY_ACTION_PINS:
                    assert uses == FIRST_PARTY_ACTION_PINS[action], (
                        path.name,
                        job_id,
                        uses,
                    )


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
# Per-stage dispatch drift guards (TOL02-WS09 #780, ADR-0054)
# --------------------------------------------------------------------------


def test_standalone_contract_is_tag_only_on_every_stage_block():
    # The aligned stage-input contract (#780): prepare standalone-dispatches
    # on `version` (it CREATES the tag); every downstream stage block on
    # `tag` alone — all plan facts and results optional, re-derived by the
    # block's plan job. A new REQUIRED input here breaks one-line dispatch
    # callers fleet-wide; this pin turns that into an explicit decision.
    for name in PLAN_DISCRIMINATOR:
        inputs = _load(name)["on"]["workflow_call"]["inputs"]
        required = {k for k, spec in inputs.items() if spec.get("required", False)}
        assert required == {"tag"}, name


def test_plan_jobs_gate_on_the_omitted_fact_and_rederive_plan_only():
    # The plan job runs ONLY when the caller omitted the facts (the
    # composed chain always supplies them — no re-derivation tax there),
    # re-derives via the ONE planner (`release preflight --plan-only`,
    # pinned launcher, pipefail — same masking hazard as wf-prepare), and
    # runs in a deliberately secret-free environment: --plan-only exists so
    # it can (presence was the source run's preflight's job; each stage's
    # verb still validates its own names).
    for name, fact in PLAN_DISCRIMINATOR.items():
        job = _load(name)["jobs"]["plan"]
        assert job["if"] == f"inputs.{fact} == ''", name
        script = _runs(job["steps"])
        assert "release preflight" in script, name
        assert "--plan-only" in script, name
        assert "pixi run --locked ./bin/shipit" in script, name
        assert "set -euo pipefail" in script, name
        assert "secrets." not in json.dumps(job), name


def test_fan_jobs_coalesce_input_or_plan_and_override_the_needs_skip():
    # The fan jobs ride `input || plan-output` (two sources of the SAME
    # fact, never a re-derivation) and MUST carry `!cancelled()` + explicit
    # plan-result checks: the plan job SKIPS on every fact-supplied run and
    # default needs-semantics would skip the fan with it, while a
    # failed/cancelled re-derivation must still block.
    fans = {
        "wf-build.yml": ("build", "matrix"),
        "wf-sign-mac.yml": ("sign", "sign-matrix"),
        "wf-publish.yml": ("assert", "unsigned-matrix"),
    }
    for name, (job_id, fact) in fans.items():
        job = _load(name)["jobs"][job_id]
        needs = job["needs"]
        assert needs == "plan" or needs == ["plan"], name
        cond = job["if"]
        assert "!cancelled()" in cond, name
        assert "needs.plan.result != 'failure'" in cond, name
        assert "needs.plan.result != 'cancelled'" in cond, name
        coalesced = f"inputs.{fact} || needs.plan.outputs.{fact}"
        assert coalesced in cond, name
        assert coalesced in job["strategy"]["matrix"]["include"], name


def test_publish_plan_job_derives_the_standalone_claims_from_liveness():
    # The standalone stage-result claims (#780): live stage → success,
    # plan-proven non-live → skipped, derived from the SAME plan JSON the
    # facts come from — never from a real result (none exists on this
    # path). They are enforced, not trusted: the source-run downloads fail
    # loudly on a missing artifact, then the verb's scar-#3 gate runs
    # unchanged.
    script = _runs(_load("wf-publish.yml")["jobs"]["plan"]["steps"])
    assert "build-result=" in script and ".matrix == []" in script
    assert "bundle-result=" in script and 'index("bundle")' in script
    assert "sign-result=" in script and "select(.sign)" in script


def test_cross_run_downloads_pair_run_id_with_token_and_gate_both_ways():
    # download-artifact with `run-id` rides the REST API: it needs a token,
    # and it must NEVER be the composed chain's path (whose callers do not
    # grant actions:read) — so every download step is split: same-run (no
    # run-id/github-token keys, gated `inputs.run-id == ''`) vs cross-run
    # (both keys, gated `inputs.run-id != ''`).
    for name in ("wf-sign-mac.yml", "wf-publish.yml"):
        for job_id, job in _load(name)["jobs"].items():
            for step in job.get("steps", []):
                if "download-artifact" not in step.get("uses", ""):
                    continue
                cond = step.get("if", "")
                if "run-id" in step["with"]:
                    assert step["with"]["run-id"] == "${{ inputs.run-id }}", (
                        name,
                        job_id,
                    )
                    assert step["with"]["github-token"] == "${{ github.token }}", (
                        name,
                        job_id,
                    )
                    assert "inputs.run-id != ''" in cond, (name, job_id)
                else:
                    assert "github-token" not in step["with"], (name, job_id)
                    assert "inputs.run-id == ''" in cond, (name, job_id)


def test_artifact_downloading_blocks_declare_no_permissions_key():
    # The downgrade-only rule: a called workflow's `permissions:` can only
    # STRIP the caller's grant, so a key on wf-sign-mac/wf-publish would
    # strip the `actions: read` a standalone dispatch caller grants for the
    # cross-run downloads (and could never elevate anything in return —
    # gh-release always depended on the caller granting contents:write).
    # These two inherit the caller's token verbatim; the non-downloading
    # blocks keep their least-privilege declarations.
    for name in ("wf-sign-mac.yml", "wf-publish.yml"):
        assert "permissions" not in _load(name), name
    assert _load("wf-build.yml")["permissions"] == {"contents": "read"}
    assert _load("wf-prepare.yml")["permissions"] == {"contents": "write"}


def test_publish_enumerates_the_signed_claim_per_entry_before_overlay():
    # The standalone sign-result=success CLAIM is derived from plan liveness,
    # but the wildcard signed-* overlay passes on ANY match — a source run
    # that signed some legs then failed would publish a MIXED tree under a
    # success claim. A verify step enumerates the expected
    # signed-<artifact>-<platform> set from the plan matrix and fails on any
    # the source run is missing, BEFORE the cp overlay. Standalone only
    # (composed callers grant no actions:read, and scar #3 already blocks a
    # real partial sign failure — its sign-result is FAILURE, never success).
    steps = _load("wf-publish.yml")["jobs"]["publish"]["steps"]
    verify = next(
        s
        for s in steps
        if s.get("name") == "Verify the source run signed every claimed entry"
    )
    cond = verify["if"]
    assert "sign-result || needs.plan.outputs.sign-result) == 'success'" in cond
    assert "inputs.run-id != ''" in cond
    script = verify["run"]
    assert "select(.sign)" in script  # the expected set is the sign projection
    assert "actions/runs/${RUN_ID}/artifacts" in script  # against the source run
    assert "exit 1" in script  # a missing entry is fatal
    # Strictly ORDERED before the overlay: a partial set is refused, never
    # applied.
    order = [s.get("name", "") for s in steps]
    assert order.index(
        "Verify the source run signed every claimed entry"
    ) < order.index("Apply the signed overlay")


def test_standalone_sign_run_carries_base_artifacts_as_a_publish_source():
    # A standalone sign run must be a COMPLETE publish source: publish names
    # ONE run-id and downloads release-notes, bundle-*, signed-* from it. The
    # sign legs produce only signed-* (and drop the bundles they consumed,
    # never fetch release-notes), so carry-bundles (per bundle-bearing leg)
    # and carry-notes re-upload the base families from the SOURCE run under
    # their original names. Both standalone-only (run-id != ''), skipped
    # no-ops on the composed chain (one shared run — nothing to carry).
    doc = _load("wf-sign-mac.yml")
    # The plan feeds the FULL bundle projection to the carry fan (publish
    # stages every bundle-* and asserts each unsigned leg by name).
    assert "bundle-matrix" in doc["jobs"]["plan"]["outputs"]
    assert "select(.bundle)" in _runs(doc["jobs"]["plan"]["steps"])

    carry_bundles = doc["jobs"]["carry-bundles"]
    assert "inputs.run-id != ''" in carry_bundles["if"]
    assert (
        carry_bundles["strategy"]["matrix"]["include"]
        == "${{ fromJson(needs.plan.outputs.bundle-matrix) }}"
    )
    up = next(
        s for s in carry_bundles["steps"] if "upload-artifact" in s.get("uses", "")
    )
    assert up["with"]["name"] == "bundle-${{ matrix.artifact }}-${{ matrix.platform }}"

    carry_notes = doc["jobs"]["carry-notes"]
    assert "inputs.run-id != ''" in carry_notes["if"]
    up = next(s for s in carry_notes["steps"] if "upload-artifact" in s.get("uses", ""))
    assert up["with"]["name"] == "release-notes"


# --------------------------------------------------------------------------
# The dogfood dispatch caller (#774) — the WS09 blessed stage-choice shape
# --------------------------------------------------------------------------

#: shipit's own release caller — the blessed per-stage dispatch surface
#: (workflows.lex §8, ADR-0054), dogfooded verbatim on the publisher repo.
DISPATCH_CALLER = "shipit-release.yml"


def test_dispatch_caller_is_the_blessed_stage_choice_shape():
    # The #774 cutover caller IS the shape every consumer inherits: one
    # workflow_dispatch trigger (never push/PR — a release block must not
    # run on shipit's own CI), the full five-way stage choice defaulting to
    # the composed chain, and one routing-only job per stage — a single
    # remote `uses:` line gated on its stage, no steps, no stage-to-stage
    # output wiring (exactly what ADR-0040 forbids consumer-side and WS06
    # proved unwireable). The refs are the FULL remote @v1 form (the
    # remote-ref scar, #823): this caller models the correct-by-construction
    # consumer shape even though `./` would happen to resolve here.
    doc = _load(DISPATCH_CALLER)
    assert checks.workflow_triggers(doc) == ["workflow_dispatch"]
    stage = doc["on"]["workflow_dispatch"]["inputs"]["stage"]
    assert stage["options"] == ["full", "prepare", "build", "sign", "publish"]
    assert stage["default"] == "full"

    jobs = doc["jobs"]
    stage_for_job = {
        "release": "full",
        "prepare": "prepare",
        "build": "build",
        "sign": "sign",
        "publish": "publish",
    }
    assert list(jobs) == list(stage_for_job)
    for job_id, job in jobs.items():
        assert job["if"] == f"inputs.stage == '{stage_for_job[job_id]}'", job_id
        assert "steps" not in job, job_id  # routing only, never logic
        assert job["uses"].startswith("arthur-debert/shipit/.github/workflows/wf-"), (
            job_id
        )
        assert job["uses"].endswith(".yml@v1"), job_id


def test_dispatch_caller_forwards_the_stage_input_contract_verbatim():
    # The aligned standalone contract (#780): `full`/`prepare` dispatch on
    # `version`; `build`/`sign`/`publish` on `tag` alone (ADR-0041 — the
    # version is read off the tag), plus `run-id` on the artifact-consuming
    # stages. Secrets: RELEASE_TOKEN only, to the stages that push (shipit's
    # plan is gh-release-only — GITHUB_TOKEN — with no sign stage, so no
    # endpoint or Apple names are forwarded anywhere).
    jobs = _load(DISPATCH_CALLER)["jobs"]
    withs = {job_id: set(job.get("with", {})) for job_id, job in jobs.items()}
    assert withs["release"] == {"version", "unsigned"}
    assert withs["prepare"] == {"version", "unsigned"}
    assert withs["build"] == {"tag"}
    assert withs["sign"] == {"tag", "run-id"}
    assert withs["publish"] == {"tag", "run-id", "unsigned"}
    for job_id, job in jobs.items():
        secrets = job.get("secrets", {})
        expected = {"RELEASE_TOKEN"} if job_id in ("release", "prepare") else set()
        assert set(secrets) == expected, job_id


def test_dispatch_caller_grants_cross_run_download_permissions():
    # workflows.lex §8: the standalone dispatch caller's token must carry
    # `actions: read` beside the stage's own needs — `run-id` flips
    # download-artifact onto the REST API, and the downloading blocks
    # deliberately declare no `permissions:` key (a called workflow can
    # only DOWNGRADE this grant). `contents: write` covers prepare's push
    # and publish's gh-release.
    doc = _load(DISPATCH_CALLER)
    assert doc["permissions"] == {"contents": "write", "actions": "read"}


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
    # codesign/notarytool) is the printed untestable hole. Scoped to the
    # `sign` job: the standalone-only `carry-bundles` fan rides a plan OUTPUT
    # matrix (`needs.plan.outputs.bundle-matrix`) that only exists at runtime,
    # so an unscoped walk fatals evaluating it against the empty expression —
    # the same workflow_call-fidelity hole the other fan smokes scope around.
    "wf-sign-mac.yml": {
        "inputs": ("tag=v1.2.3", f"sign-matrix={_ENTRY}"),
        "job": "sign",
    },
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
    # Per-stage dispatch (#780): each stage block parses and walks its plan
    # job against the STANDALONE contract too — tag only (+ run-id where
    # artifacts flow in), every plan fact omitted, so the plan job's `if`
    # flips ON. Scoped to the plan job: the fan legs ride plan OUTPUTS that
    # only exist at runtime (act dry-run evaluates the matrix expression
    # even for an if-gated-off job and fatals on the empty string) — the
    # same printed workflow_call-fidelity hole the composed chain's smoke
    # scopes around.
    "wf-build.yml (standalone)": {
        "file": "wf-build.yml",
        "inputs": ("tag=v1.2.3",),
        "job": "plan",
    },
    "wf-sign-mac.yml (standalone)": {
        "file": "wf-sign-mac.yml",
        "inputs": ("tag=v1.2.3", "run-id=1"),
        "job": "plan",
    },
    "wf-publish.yml (standalone)": {
        "file": "wf-publish.yml",
        "inputs": ("tag=v1.2.3", "run-id=1"),
        "job": "plan",
    },
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
        f".github/workflows/{spec.get('file', name)}",
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


@pytest.mark.skipif(shutil.which("act") is None, reason="act not on PATH")
@pytest.mark.skipif(not _docker_daemon_up(), reason="docker daemon unavailable")
def test_dispatch_caller_smokes_green_under_act_dry_run(monkeypatch, capsys):
    # The #774 dogfood caller against a crafted workflow_dispatch: parse,
    # trigger match, the stage gate, and the remote @v1 ref resolved against
    # THIS checkout. Scoped to the `prepare` stage dispatch — its block
    # nests nothing further, so the walk avoids the nested runtime-output
    # plumbing the composed chain's smoke also scopes around (the printed
    # workflow_call-fidelity hole).
    root = _WORKFLOWS.parents[1]
    monkeypatch.chdir(root)
    rc = wf.run(
        ".github/workflows/shipit-release.yml",
        event=wf.EVENT_WORKFLOW_DISPATCH,
        inputs=("version=1.0.0", "stage=prepare"),
        job="prepare",
        dry_run=True,
        local_repositories=(f"arthur-debert/shipit@v1={root}",),
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WF TEST: OK" in out
