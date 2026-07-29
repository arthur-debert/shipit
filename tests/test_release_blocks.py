from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from test_ci_workflow import _WORKFLOWS, _load

from shipit import checks
from shipit.verbs import wf

STAGE_BLOCKS = ("wf-prepare.yml", "wf-build.yml", "wf-sign-mac.yml", "wf-publish.yml")
COMPOSED = "wf-release.yml"

STABLE_JOBS = {
    "wf-prepare.yml": ["prepare"],
    "wf-build.yml": ["plan", "notes", "build"],
    "wf-sign-mac.yml": ["plan", "sign", "carry-bundles", "carry-notes"],
    "wf-publish.yml": ["plan", "assert", "publish"],
    "wf-release.yml": ["prepare", "build", "sign", "publish"],
}

PLAN_DISCRIMINATOR = {
    "wf-build.yml": "matrix",
    "wf-sign-mac.yml": "sign-matrix",
    "wf-publish.yml": "stages",
}

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


def test_blocks_are_workflow_call_only():
    for name in (*STAGE_BLOCKS, COMPOSED):
        doc = _load(name)
        assert checks.workflow_triggers(doc) == ["workflow_call"], name


def test_stable_job_names_hold_for_adp02():
    for name, jobs in STABLE_JOBS.items():
        assert list(_load(name)["jobs"]) == jobs, name


def test_composed_chain_refs_are_remote_never_local():
    jobs = _load(COMPOSED)["jobs"]
    for job_id, job in jobs.items():
        ref = job.get("uses", "")
        assert ref.startswith("arthur-debert/shipit/.github/workflows/wf-"), (
            job_id,
            ref,
        )
        assert ref.endswith("@v1"), (job_id, ref)


def test_composed_chain_carries_zero_logic():
    jobs = _load(COMPOSED)["jobs"]
    publish = jobs["publish"]
    assert "needs.build.result ==" not in publish.get("if", "")
    assert "needs.sign.result ==" not in publish.get("if", "")
    assert "!cancelled()" in publish["if"]
    assert "needs.prepare.result == 'success'" in publish["if"]
    assert publish["with"]["build-result"] == "${{ needs.build.result }}"
    assert publish["with"]["bundle-result"] == "${{ needs.build.result }}"
    assert publish["with"]["sign-result"] == "${{ needs.sign.result }}"
    assert publish["with"]["matrix"] == "${{ needs.prepare.outputs.matrix }}"
    assert publish["with"]["stages"] == "${{ needs.prepare.outputs.stages }}"
    assert jobs["sign"]["if"] == "needs.prepare.outputs.sign-matrix != '[]'"


def test_assert_bundle_runs_at_sign_entry_before_any_secret():
    steps = _steps("wf-sign-mac.yml", "sign")
    order = [i for i, s in enumerate(steps) if "assert-bundle" in s.get("run", "")]
    sign = [i for i, s in enumerate(steps) if "release sign" in s.get("run", "")]
    assert order and sign and order[0] < sign[0]


def test_assert_bundle_guards_publishes_unsigned_path():
    doc = _load("wf-publish.yml")
    assert_job = doc["jobs"]["assert"]
    assert "unsigned-matrix" in assert_job["strategy"]["matrix"]["include"]
    assert "assert-bundle" in _runs(assert_job["steps"])
    publish = doc["jobs"]["publish"]
    assert publish["needs"] == ["plan", "assert"]
    assert "!cancelled()" in publish["if"]
    assert "needs.assert.result != 'failure'" in publish["if"]


def test_publish_block_feeds_results_to_the_verb_not_yaml():
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
    step = next(s for s in publish["steps"] if "release publish" in s.get("run", ""))
    assert step["env"]["MATRIX"] == "${{ inputs.matrix || needs.plan.outputs.matrix }}"
    assert step["env"]["STAGES"] == "${{ inputs.stages || needs.plan.outputs.stages }}"
    matrix = doc["on"]["workflow_call"]["inputs"]["matrix"]
    assert matrix["required"] is False
    assert 'if [[ -n "$MATRIX" ]]' in script
    assert 'args+=(--matrix "$MATRIX")' in script


def test_publish_block_declares_feeds_and_forwards_every_endpoint_token():
    from shipit.release import secretreq

    endpoint_tokens = {
        name for names in secretreq.ENDPOINT_SECRETS.values() for name in names
    }
    assert "DOWNSTREAM_DISPATCH_TOKEN" in endpoint_tokens

    declared = _load("wf-publish.yml")["on"]["workflow_call"]["secrets"]
    assert endpoint_tokens <= set(declared)
    assert all(not declared[name].get("required", False) for name in endpoint_tokens)
    publish = _load("wf-publish.yml")["jobs"]["publish"]
    step = next(s for s in publish["steps"] if "release publish" in s.get("run", ""))
    for name in sorted(endpoint_tokens):
        assert step["env"][name] == f"${{{{ secrets.{name} }}}}"
    forwarded = _load(COMPOSED)["jobs"]["publish"]["secrets"]
    for name in sorted(endpoint_tokens):
        assert forwarded[name] == f"${{{{ secrets.{name} }}}}"


def test_prepare_pipeline_steps_set_pipefail():
    steps = _steps("wf-prepare.yml", "prepare")
    piped = [s for s in steps if "| jq -c" in s.get("run", "")]
    assert piped, "expected the plan/prepare steps to pipe into jq"
    for step in piped:
        assert "set -euo pipefail" in step["run"], step.get("name")


def test_build_block_never_ships_the_target_tree():
    steps = _steps("wf-build.yml", "build")
    uploads = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    assert len(uploads) == 1
    path = uploads[0]["with"]["path"]
    assert "target" not in path
    assert path.splitlines()[0].strip() == "dist/**"
    assert "!dist/**/*.app/**" in path


def test_build_block_cross_compiles_via_the_matrix_target():
    steps = _steps("wf-build.yml", "build")
    build = next(s for s in steps if "/shipit build" in s.get("run", ""))
    assert '--target "$TARGET"' in build["run"]
    assert build["env"]["TARGET"] == "${{ matrix.target }}"
    bundle = next(s for s in steps if "release bundle" in s.get("run", ""))
    assert bundle["env"]["TARGET"] == "${{ matrix.target }}"


def test_build_block_bundles_only_its_matrix_entry_artifact():
    steps = _steps("wf-build.yml", "build")
    bundle = next(s for s in steps if "release bundle" in s.get("run", ""))
    assert '--artifact "$ARTIFACT"' in bundle["run"]
    assert bundle["env"]["ARTIFACT"] == "${{ matrix.artifact }}"


def test_build_block_gates_bundle_on_the_per_entry_flag_not_the_stage():
    steps = _steps("wf-build.yml", "build")
    bundle = next(s for s in steps if "release bundle" in s.get("run", ""))
    upload = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert bundle["if"] == "matrix.bundle"
    assert upload["if"] == "matrix.bundle"


def test_sign_chain_declares_and_forwards_both_notary_trios():
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

    sign_step = next(
        s
        for s in _steps("wf-sign-mac.yml", "sign")
        if "release sign" in s.get("run", "")
    )
    for name in sorted(mac_names):
        assert sign_step["env"][name] == f"${{{{ secrets.{name} }}}}"
    plan_step = next(
        s for s in _steps("wf-prepare.yml", "prepare") if s.get("id") == "plan"
    )
    for name in sorted(mac_names):
        assert plan_step["env"][name] == f"${{{{ secrets.{name} }}}}"
    jobs = _load(COMPOSED)["jobs"]
    for job_id in ("prepare", "sign"):
        forwarded = jobs[job_id]["secrets"]
        for name in sorted(mac_names):
            assert forwarded[name] == f"${{{{ secrets.{name} }}}}", job_id


def test_endpoint_tokens_are_declared_forwarded_and_mapped_everywhere():
    from shipit.release import secretreq

    endpoint_tokens = {
        name for names in secretreq.ENDPOINT_SECRETS.values() for name in names
    }

    for block in ("wf-prepare.yml", "wf-publish.yml", "wf-release.yml"):
        declared = _load(block)["on"]["workflow_call"]["secrets"]
        assert endpoint_tokens <= set(declared), block
        assert all(
            not spec.get("required", False)
            for name, spec in declared.items()
            if name in endpoint_tokens
        ), block

    plan_step = next(
        s for s in _steps("wf-prepare.yml", "prepare") if s.get("id") == "plan"
    )
    for name in sorted(endpoint_tokens):
        assert plan_step["env"][name] == f"${{{{ secrets.{name} }}}}", name

    publish_step = next(
        s for s in _steps("wf-publish.yml", "publish") if s.get("name") == "Publish"
    )
    for name in sorted(endpoint_tokens):
        assert publish_step["env"][name] == f"${{{{ secrets.{name} }}}}", name

    jobs = _load(COMPOSED)["jobs"]
    for job_id in ("prepare", "publish"):
        forwarded = jobs[job_id]["secrets"]
        for name in sorted(endpoint_tokens):
            assert forwarded[name] == f"${{{{ secrets.{name} }}}}", (job_id, name)


def test_pixi_pin_is_lockstep_across_all_blocks():
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


FIRST_PARTY_ACTION_PINS = {
    "actions/checkout": "actions/checkout@v6",
    "actions/upload-artifact": "actions/upload-artifact@v7",
    "actions/download-artifact": "actions/download-artifact@v8",
}


def test_first_party_action_pins_are_node24_and_lockstep_everywhere():
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
    doc = _load("advance-major.yml")
    assert checks.workflow_triggers(doc) == ["push"]
    script = _runs(doc["jobs"]["advance"]["steps"])
    assert "git branch -f" in script
    assert "--force-with-lease" in script
    assert "*-*)" in script


def test_standalone_contract_is_tag_only_on_every_stage_block():
    for name in PLAN_DISCRIMINATOR:
        inputs = _load(name)["on"]["workflow_call"]["inputs"]
        required = {k for k, spec in inputs.items() if spec.get("required", False)}
        assert required == {"tag"}, name


def test_plan_jobs_gate_on_the_omitted_fact_and_rederive_plan_only():
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
    script = _runs(_load("wf-publish.yml")["jobs"]["plan"]["steps"])
    assert "build-result=" in script and ".matrix == []" in script
    assert "bundle-result=" in script and 'index("bundle")' in script
    assert "sign-result=" in script and "select(.sign)" in script


def test_cross_run_downloads_pair_run_id_with_token_and_gate_both_ways():
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
    for name in ("wf-sign-mac.yml", "wf-publish.yml"):
        assert "permissions" not in _load(name), name
    assert _load("wf-build.yml")["permissions"] == {"contents": "read"}
    assert _load("wf-prepare.yml")["permissions"] == {"contents": "write"}


def test_publish_enumerates_the_signed_claim_per_entry_before_overlay():
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
    assert "select(.sign)" in script
    assert "actions/runs/${RUN_ID}/artifacts" in script
    assert "exit 1" in script
    order = [s.get("name", "") for s in steps]
    assert order.index(
        "Verify the source run signed every claimed entry"
    ) < order.index("Apply the signed overlay")


def test_standalone_sign_run_carries_base_artifacts_as_a_publish_source():
    doc = _load("wf-sign-mac.yml")
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


def test_standalone_build_run_rederives_notes_as_a_relay_source():
    doc = _load("wf-build.yml")
    notes = doc["jobs"]["notes"]
    assert notes["if"] == "inputs.matrix == ''"
    runs = _runs(notes["steps"])
    assert "release notes" in runs
    assert "--out RELEASE_NOTES.md" in runs
    assert "${TAG#v}" in runs
    up = next(s for s in notes["steps"] if "upload-artifact" in s.get("uses", ""))
    assert up["with"]["name"] == "release-notes"
    assert up["with"]["path"] == "RELEASE_NOTES.md"
    assert up["with"]["if-no-files-found"] == "error"


def test_endpoint_selector_threads_from_input_to_the_verb():
    doc = _load("wf-publish.yml")
    declared = doc["on"]["workflow_call"]["inputs"]["endpoints"]
    assert declared["required"] is False
    assert "default" not in declared

    publish = doc["jobs"]["publish"]
    step = next(s for s in publish["steps"] if "release publish" in s.get("run", ""))
    assert step["env"]["ENDPOINTS"] == "${{ inputs.endpoints }}"
    script = step["run"]
    assert '<<<"${ENDPOINTS//,/ }"' in script
    assert 'args+=(--endpoint "$endpoint")' in script


def test_delimiter_only_endpoint_selector_fails_closed():
    doc = _load("wf-publish.yml")
    publish = doc["jobs"]["publish"]
    step = next(s for s in publish["steps"] if "release publish" in s.get("run", ""))
    verb_line = 'pixi run --locked ./bin/shipit release publish "$VERSION" "${args[@]}"'
    script = step["run"]
    assert verb_line in script
    harness = script.replace(verb_line, 'printf "%s\\n" "${args[@]}"')

    def run(selector: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-e", "-c", harness],
            env={"PATH": "/usr/bin:/bin", "ENDPOINTS": selector},
            capture_output=True,
            text=True,
        )

    for selector in (",", ", ,", " ,, "):
        rejected = run(selector)
        assert rejected.returncode != 0, selector
        assert "delimiters" in rejected.stderr, selector
        assert "--endpoint" not in rejected.stdout, selector
    for selector in ("", "   "):
        blank = run(selector)
        assert blank.returncode == 0, repr(selector)
        assert "--endpoint" not in blank.stdout, repr(selector)
    scoped = run("gh-release, conda")
    assert scoped.returncode == 0, scoped.stderr
    tail = scoped.stdout.splitlines()[-4:]
    assert tail == ["--endpoint", "gh-release", "--endpoint", "conda"]


def test_composed_chain_forwards_the_endpoint_selector_to_publish():
    doc = _load(COMPOSED)
    declared = doc["on"]["workflow_call"]["inputs"]["endpoints"]
    assert declared["required"] is False
    jobs = doc["jobs"]
    assert jobs["publish"]["with"]["endpoints"] == "${{ inputs.endpoints }}"
    for job_id in ("prepare", "build", "sign"):
        assert "endpoints" not in jobs[job_id].get("with", {}), job_id


DISPATCH_CALLER = "shipit-release.yml"


def test_dispatch_caller_is_the_blessed_stage_choice_shape():
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
        assert "steps" not in job, job_id
        assert job["uses"].startswith("arthur-debert/shipit/.github/workflows/wf-"), (
            job_id
        )
        assert job["uses"].endswith(".yml@v1"), job_id


def test_dispatch_caller_forwards_the_stage_input_contract_verbatim():
    jobs = _load(DISPATCH_CALLER)["jobs"]
    withs = {job_id: set(job.get("with", {})) for job_id, job in jobs.items()}
    assert withs["release"] == {"version", "unsigned", "endpoints"}
    assert withs["prepare"] == {"version", "unsigned"}
    assert withs["build"] == {"tag"}
    assert withs["sign"] == {"tag", "run-id"}
    assert withs["publish"] == {"tag", "run-id", "unsigned", "endpoints"}
    for job_id, job in jobs.items():
        secrets = job.get("secrets", {})
        expected = {"RELEASE_TOKEN"} if job_id in ("release", "prepare") else set()
        assert set(secrets) == expected, job_id


def test_dispatch_caller_threads_the_endpoint_selector():
    doc = _load(DISPATCH_CALLER)
    declared = doc["on"]["workflow_dispatch"]["inputs"]["endpoints"]
    assert declared["required"] is False
    assert declared["type"] == "string"
    jobs = doc["jobs"]
    assert jobs["release"]["with"]["endpoints"] == "${{ inputs.endpoints }}"
    assert jobs["publish"]["with"]["endpoints"] == "${{ inputs.endpoints }}"


def test_dispatch_caller_grants_cross_run_download_permissions():
    doc = _load(DISPATCH_CALLER)
    assert doc["permissions"] == {"contents": "write", "actions": "read"}


def _declared_secrets(name: str) -> set[str]:
    return set(_load(name)["on"]["workflow_call"].get("secrets") or {})


def test_caller_secret_rule_trims_are_pinned_to_the_blocks():
    assert set(wf.SIGN_BLOCK_SECRETS) == _declared_secrets("wf-sign-mac.yml")
    assert set(wf.PUBLISH_BLOCK_SECRETS) == _declared_secrets("wf-publish.yml")
    assert _declared_secrets("wf-build.yml") == set()
    assert _declared_secrets("wf-prepare.yml") == _declared_secrets("wf-release.yml")
    assert _declared_secrets("wf-prepare.yml") == (
        {"RELEASE_TOKEN"} | set(wf.SIGN_BLOCK_SECRETS) | set(wf.PUBLISH_BLOCK_SECRETS)
    )


def test_dispatch_caller_passes_its_own_secret_lint():
    doc = _load(DISPATCH_CALLER)
    assert wf.stage_caller_jobs(doc) == {
        "release": "full",
        "prepare": "prepare",
        "build": "build",
        "sign": "sign",
        "publish": "publish",
    }
    assert wf.caller_secret_drift(doc) == []


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
    "wf-release.yml": {"inputs": ("version=1.2.3",), "job": "prepare", "local": True},
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
