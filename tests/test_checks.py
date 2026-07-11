"""Unit tests for required-check discovery — the pure helpers and nesting logic."""

import pytest

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
    # `if: inputs.bats` conditional job is included only when the caller passes bats=true.
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


def test_is_reusable_workflow():
    assert checks.is_reusable_workflow({"on": "workflow_call"})
    assert checks.is_reusable_workflow({"on": {"workflow_call": {"inputs": {}}}})
    assert checks.is_reusable_workflow({"on": ["push", "workflow_call"]})
    assert not checks.is_reusable_workflow({"on": "pull_request"})
    assert not checks.is_reusable_workflow("nonsense")


def _write_workflow(tmp_path, name, text):
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True, exist_ok=True)
    (wfdir / name).write_text(text, encoding="utf-8")


def test_publishes_reusable_workflows_local_scan(tmp_path):
    _write_workflow(tmp_path, "ci.yml", "on:\n  pull_request:\njobs:\n  ci: {}\n")
    assert not checks.publishes_reusable_workflows("o/r", toplevel=str(tmp_path))
    _write_workflow(
        tmp_path, "wf-build.yaml", "on:\n  workflow_call:\njobs:\n  b: {}\n"
    )
    assert checks.publishes_reusable_workflows("o/r", toplevel=str(tmp_path))


def test_publishes_reusable_workflows_local_skips_unparseable(tmp_path):
    _write_workflow(tmp_path, "broken.yml", "on: [unclosed\n")
    assert not checks.publishes_reusable_workflows("o/r", toplevel=str(tmp_path))


def test_publishes_reusable_workflows_local_skips_non_utf8(tmp_path):
    _write_workflow(tmp_path, "broken.yml", "placeholder")
    (tmp_path / ".github" / "workflows" / "broken.yml").write_bytes(b"\xff\xfe")
    assert not checks.publishes_reusable_workflows("o/r", toplevel=str(tmp_path))


def test_publishes_reusable_workflows_local_no_workflows_dir(tmp_path):
    assert not checks.publishes_reusable_workflows("o/r", toplevel=str(tmp_path))


def test_check_discovery_skips_non_utf8_workflows(tmp_path):
    wfdir = tmp_path / ".github" / "workflows"
    wfdir.mkdir(parents=True)
    path = wfdir / "broken.yml"
    path.write_bytes(b"\xff\xfe")

    assert checks.pr_workflow_paths(str(wfdir)) == []
    assert (
        checks.checks_from_workflows(str(tmp_path), [".github/workflows/broken.yml"])
        == []
    )


def test_publishes_reusable_workflows_remote_contents_api(monkeypatch):
    import base64

    body = base64.b64encode(b"on:\n  workflow_call:\njobs:\n  b: {}\n").decode()
    responses = {
        "repos/o/r/contents/.github/workflows": [
            {"name": "notes.md"},
            {"name": "wf-build.yml"},
        ],
        "repos/o/r/contents/.github/workflows/wf-build.yml": {"content": body},
    }
    calls = []

    def rest(path, *, method=None, body=None, paginate=False):
        calls.append(path)
        return responses[path]

    monkeypatch.setattr(checks.gh, "rest", rest)
    assert checks.publishes_reusable_workflows("o/r", toplevel=None)
    assert "repos/o/r/contents/.github/workflows/notes.md" not in calls


def test_publishes_reusable_workflows_remote_404_means_no_publisher(monkeypatch):
    from shipit.execrun import ExecError

    def rest(path, *, method=None, body=None, paginate=False):
        raise ExecError(["gh", "api"], rc=1, stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(checks.gh, "rest", rest)
    assert not checks.publishes_reusable_workflows("o/r", toplevel=None)


@pytest.mark.parametrize("listing", [None, {"name": "wf-build.yml"}])
def test_publishes_reusable_workflows_remote_malformed_listing_raises(
    monkeypatch, listing
):
    monkeypatch.setattr(checks.gh, "rest", lambda *args, **kwargs: listing)
    with pytest.raises(ValueError, match="expected a list"):
        checks.publishes_reusable_workflows("o/r", toplevel=None)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"content": None},
        {"content": "//4="},
    ],
)
def test_publishes_reusable_workflows_remote_skips_unparseable_file(
    monkeypatch, payload
):
    responses = {
        "repos/o/r/contents/.github/workflows": [{"name": "broken.yml"}],
        "repos/o/r/contents/.github/workflows/broken.yml": payload,
    }
    monkeypatch.setattr(checks.gh, "rest", lambda path, **kwargs: responses[path])
    assert not checks.publishes_reusable_workflows("o/r", toplevel=None)


def test_publishes_reusable_workflows_remote_other_failure_raises(monkeypatch):
    """A non-404 remote failure must RAISE — the caller reports "could not
    inspect", never a verified non-publisher."""
    import pytest

    from shipit.execrun import ExecError

    def rest(path, *, method=None, body=None, paginate=False):
        raise ExecError(["gh", "api"], rc=1, stderr="HTTP 403")

    monkeypatch.setattr(checks.gh, "rest", rest)
    with pytest.raises(ExecError):
        checks.publishes_reusable_workflows("o/r", toplevel=None)


@pytest.mark.parametrize("payload", [None, {}, {"content": None}])
def test_fetch_called_workflow_rejects_non_string_content(monkeypatch, payload):
    monkeypatch.setattr(checks.gh, "rest", lambda *args, **kwargs: payload)
    with pytest.raises(ValueError, match="no content for reusable workflow"):
        checks._fetch_called_workflow(
            "owner/repo/.github/workflows/ci.yml@v1", toplevel=None
        )


def test_job_contexts_reusable_nesting_and_conditions():
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
