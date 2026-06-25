"""Unit tests for required-check discovery — the pure helpers and nesting logic."""

from shipit import checks


def test_workflow_triggers_forms():
    assert checks.workflow_triggers({"on": "pull_request"}) == ["pull_request"]
    assert checks.workflow_triggers({"on": ["push", "pull_request"]}) == [
        "push",
        "pull_request",
    ]
    assert set(checks.workflow_triggers({"on": {"push": {}, "pull_request": {}}})) == {
        "push",
        "pull_request",
    }
    assert checks.workflow_triggers("nonsense") == []


def test_is_pr_workflow():
    assert checks.is_pr_workflow({"on": "pull_request"})
    assert checks.is_pr_workflow({"on": {"pull_request": {}}})
    assert not checks.is_pr_workflow({"on": "push"})


def test_path_filtered_only_when_filter_present():
    assert not checks.pr_trigger_is_path_filtered({"on": "pull_request"})
    assert not checks.pr_trigger_is_path_filtered({"on": {"pull_request": {}}})
    assert checks.pr_trigger_is_path_filtered(
        {"on": {"pull_request": {"paths": ["src/**"]}}}
    )
    assert checks.pr_trigger_is_path_filtered(
        {"on": {"pull_request": {"paths-ignore": ["docs/**"]}}}
    )


def test_checks_json_drops_empties_and_wraps():
    assert checks.checks_json(["a", "", "b"]) == [
        {"context": "a"},
        {"context": "b"},
    ]


def test_job_display_name_prefers_static_name():
    assert checks.job_display_name("build", {"name": "Build"}) == "Build"
    # An expression name can't be resolved statically → fall back to job id.
    assert checks.job_display_name("build", {"name": "${{ matrix.os }}"}) == "build"
    assert checks.job_display_name("build", {}) == "build"


def test_called_job_included_resolves_inputs_if():
    # `if: inputs.bats` gated job is included only when the caller passes bats=true.
    bats_job = {"if": "inputs.bats"}
    assert checks._called_job_included(bats_job, {"bats": True})
    assert checks._called_job_included(bats_job, {"bats": "true"})
    assert not checks._called_job_included(bats_job, {})
    assert not checks._called_job_included(bats_job, {"bats": False})
    # A non-inputs `if:` is included (a skipped job still reports a check run).
    assert checks._called_job_included({"if": "github.event_name == 'push'"}, {})
    assert checks._called_job_included({}, {})


def test_on_key_is_not_parsed_as_bool():
    # The YAML 1.1 gotcha: `on:` must stay the string key, not become True.
    doc = checks._load_yaml_text(
        "on:\n  push:\n    branches: ['**']\n  pull_request:\n"
        "jobs:\n  ci:\n    name: CI\n"
    )
    assert "on" in doc
    assert True not in doc
    assert checks.is_pr_workflow(doc)
    assert not checks.pr_trigger_is_path_filtered(doc)


def test_loader_keeps_true_false_as_bool():
    doc = checks._load_yaml_text("a: true\nb: false\nc: on\nd: yes\n")
    assert doc["a"] is True
    assert doc["b"] is False
    # on/off/yes/no stay strings (only true/false are bools).
    assert doc["c"] == "on"
    assert doc["d"] == "yes"


def test_job_contexts_plain_job():
    assert checks._job_contexts(
        "build", {"name": "Build"}, toplevel=None, cache={}
    ) == ["Build"]


def test_job_contexts_reusable_nesting_and_gating():
    uses = "owner/repo/.github/workflows/ci.yml@v1"
    job = {"uses": uses, "with": {"bats": True}}
    # Pre-seed the cache so no boundary call happens.
    cache = {
        uses: {
            "jobs": {
                "build": {},
                "bats": {"if": "inputs.bats"},
                "e2e": {"if": "inputs.e2e"},
            }
        }
    }
    ctxs = checks._job_contexts("call", job, toplevel=None, cache=cache)
    # e2e is excluded (inputs.e2e not enabled); the bare caller name is never used.
    assert ctxs == ["call / build", "call / bats"]
