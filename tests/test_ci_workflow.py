from pathlib import Path

from shipit import checks

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return checks._load_yaml_text((_WORKFLOWS / name).read_text(encoding="utf-8"))


def test_ci_caller_has_superseded_run_concurrency_group():
    doc = _load("ci.yml")
    concurrency = doc.get("concurrency")
    assert isinstance(concurrency, dict), "ci.yml lost its concurrency group"
    assert concurrency.get("cancel-in-progress") is True
    group = concurrency.get("group", "")
    assert "github.event_name" in group
    assert "github.event.pull_request.number" in group
    assert "github.ref" in group


def test_ci_caller_check_job_skips_on_cancelled_block():
    doc = _load("ci.yml")
    check = doc["jobs"]["check"]
    assert check["needs"] == "checks"
    condition = check["if"]
    assert "always()" in condition
    assert "needs.checks.result != 'cancelled'" in condition
    steps_script = "".join(step.get("run", "") for step in check["steps"])
    assert 'test "$RESULT" = "success"' in steps_script


def test_wf_checks_block_is_call_only_so_concurrency_stays_caller_side():
    doc = _load("wf-checks.yml")
    assert checks.workflow_triggers(doc) == ["workflow_call"]


def test_wf_checks_run_job_uses_planner_emitted_provisioning_fields():
    doc = _load("wf-checks.yml")
    steps = doc["jobs"]["run"]["steps"]
    setup = next(
        step for step in steps if step.get("uses") == "prefix-dev/setup-pixi@v0.9.6"
    )
    assert setup["with"]["environments"] == "${{ matrix.envs || 'default' }}"
    assert setup["with"]["cache"] is True
    assert setup["with"]["cache-write"] is True
    assert setup["with"]["cache-key"] == "pixi-${{ matrix.envset || 'default' }}-"

    rust_path = next(
        step
        for step in steps
        if step.get("name") == "Expose pixi rust on the runner PATH"
    )
    assert rust_path["if"] == "matrix.caches.rust"
    assert rust_path["env"]["PIXI_ENVS"] == "${{ matrix.envs || 'default' }}"
    assert "IFS=',' read -ra envs" in rust_path["run"]
    assert ".pixi/envs/$env_name/bin" in rust_path["run"]

    rust_cache = next(step for step in steps if step.get("name") == "rust-cache")
    assert rust_cache["if"] == "matrix.caches.rust"
    assert rust_cache["uses"] == "Swatinem/rust-cache@v2"
    assert rust_cache["with"]["workspaces"] == "${{ matrix.rust_workspaces || '' }}"


def test_wf_checks_declares_the_optional_lane_token_secret_seam():
    doc = _load("wf-checks.yml")
    call = doc["on"]["workflow_call"]
    assert call["secrets"]["lane_token"]["required"] is False


def test_wf_checks_run_step_gates_the_lane_token_on_the_planner_allowlist():
    doc = _load("wf-checks.yml")
    run_step = next(
        step for step in doc["jobs"]["run"]["steps"] if step.get("name") == "Run lane"
    )
    lane_token = run_step["env"]["LANE_TOKEN"]
    assert "contains(matrix.secrets, 'lane_token')" in lane_token
    assert "secrets.lane_token" in lane_token
