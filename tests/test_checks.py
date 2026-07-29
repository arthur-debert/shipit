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
    assert checks.job_display_name("build", {"name": "${{ matrix.os }}"}) == "build"
    assert checks.job_display_name("build", {}) == "build"


def test_called_job_included_resolves_inputs_if():
    bats_job = {"if": "inputs.bats"}
    assert checks._called_job_included(bats_job, {"bats": True})
    assert checks._called_job_included(bats_job, {"bats": "true"})
    assert not checks._called_job_included(bats_job, {})
    assert not checks._called_job_included(bats_job, {"bats": False})
    assert checks._called_job_included({"if": "github.event_name == 'push'"}, {})
    assert checks._called_job_included({}, {})


def test_on_key_is_not_parsed_as_bool():
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
    assert doc["c"] == "on"
    assert doc["d"] == "yes"


def test_job_contexts_plain_job():
    assert checks._job_contexts(
        "build", {"name": "Build"}, toplevel=None, cache={}
    ) == (
        ["Build"],
        [],
    )


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


_RELEASE_CALLER = """\
on:
  workflow_dispatch:
jobs:
  release:
    uses: arthur-debert/shipit/.github/workflows/wf-release.yml@v1
  prepare:
    uses: arthur-debert/shipit/.github/workflows/wf-prepare.yml@v1
  local:
    uses: ./.github/workflows/wf-local.yml
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
"""


def _caller_path(tmp_path):
    return str(tmp_path / ".github" / "workflows" / checks.RELEASE_CALLER_WORKFLOW)


def test_workflow_pin_refs_enumerates_cross_repo_vn_pins(tmp_path):
    _write_workflow(tmp_path, checks.RELEASE_CALLER_WORKFLOW, _RELEASE_CALLER)
    pins = checks.workflow_pin_refs(_caller_path(tmp_path))
    assert pins == [("arthur-debert/shipit", "v1")]


def test_workflow_pin_refs_dedupes_across_jobs_and_keeps_distinct_refs(tmp_path):
    _write_workflow(
        tmp_path,
        checks.RELEASE_CALLER_WORKFLOW,
        "on: workflow_dispatch\njobs:\n"
        "  x:\n    uses: o/r/.github/workflows/wf.yml@v1\n"
        "  y:\n    uses: o/r/.github/workflows/wf.yml@v1\n"
        "  z:\n    uses: o/r/.github/workflows/wf.yml@v2\n",
    )
    pins = checks.workflow_pin_refs(_caller_path(tmp_path))
    assert pins == [("o/r", "v1"), ("o/r", "v2")]


def test_workflow_pin_refs_scoped_to_caller_ignores_other_workflows(tmp_path):
    _write_workflow(
        tmp_path,
        checks.RELEASE_CALLER_WORKFLOW,
        "on: workflow_dispatch\njobs:\n"
        "  release:\n    uses: o/r/.github/workflows/wf-release.yml@v1\n",
    )
    _write_workflow(
        tmp_path,
        "experimental.yml",
        "on: workflow_dispatch\njobs:\n"
        "  x:\n    uses: other/repo/.github/workflows/stale.yml@v9\n",
    )
    pins = checks.workflow_pin_refs(_caller_path(tmp_path))
    assert pins == [("o/r", "v1")]


def test_workflow_pin_refs_filters_to_the_vn_shape(tmp_path):
    _write_workflow(
        tmp_path,
        checks.RELEASE_CALLER_WORKFLOW,
        "on: workflow_dispatch\njobs:\n"
        "  a:\n    uses: o/r/.github/workflows/wf.yml@v1\n"
        "  b:\n    uses: o/r/.github/workflows/wf.yml@main\n"
        "  c:\n    uses: o/r/.github/workflows/wf.yml@v1.2.3\n"
        "  d:\n    uses: o/r/.github/workflows/wf.yml@0123456789abcdef\n",
    )
    pins = checks.workflow_pin_refs(_caller_path(tmp_path))
    assert pins == [("o/r", "v1")]


def test_workflow_pin_refs_skips_unparseable_and_absent_caller(tmp_path):
    assert checks.workflow_pin_refs(_caller_path(tmp_path)) == []
    _write_workflow(tmp_path, checks.RELEASE_CALLER_WORKFLOW, "on: [unclosed\n")
    assert checks.workflow_pin_refs(_caller_path(tmp_path)) == []


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
    cache = {
        uses: {
            "jobs": {
                "build": {},
                "bats": {"if": "inputs.bats"},
                "e2e": {"if": "inputs.e2e"},
            }
        }
    }
    ctxs, dropped = checks._job_contexts("call", job, toplevel=None, cache=cache)
    assert ctxs == ["call / build", "call / bats"]
    assert dropped == []


def test_job_unpredictable_matrix_and_dynamic_name():
    assert checks.job_unpredictable({"strategy": {"matrix": {"os": ["a", "b"]}}}) == (
        "matrix"
    )
    assert checks.job_unpredictable({"name": "${{ matrix.name }}"}) == "dynamic name"
    assert (
        checks.job_unpredictable(
            {"name": "${{ matrix.name }}", "strategy": {"matrix": {"x": [1]}}}
        )
        == "matrix"
    )
    assert checks.job_unpredictable({"name": "Build"}) is None
    assert checks.job_unpredictable({}) is None
    assert checks.job_unpredictable("nonsense") is None


def test_job_contexts_drops_matrix_job_instead_of_guessing_id():
    ctxs, dropped = checks._job_contexts(
        "run",
        {"strategy": {"matrix": {"name": ["lint", "test"]}}},
        toplevel=None,
        cache={},
    )
    assert ctxs == []
    assert dropped == [checks.DroppedJob(job="run", reason="matrix")]


def test_job_contexts_drops_nested_matrix_job_caller_prefixed():
    uses = "o/r/.github/workflows/wf-checks.yml@v1"
    job = {"uses": uses}
    cache = {
        uses: {
            "jobs": {
                "plan": {},
                "run": {"name": "${{ matrix.name }}", "strategy": {"matrix": {}}},
            }
        }
    }
    ctxs, dropped = checks._job_contexts("checks", job, toplevel=None, cache=cache)
    assert ctxs == ["checks / plan"]
    assert dropped == [checks.DroppedJob(job="checks / run", reason="matrix")]


@pytest.fixture
def no_runs(monkeypatch):
    monkeypatch.setattr(checks, "checks_from_runs", lambda *a, **k: [])


def test_discover_lex_shape_names_certain_set_never_phantom_run(
    tmp_path, capsys, no_runs
):
    reusable = tmp_path / ".github" / "workflows" / "wf-checks.yml"
    reusable.parent.mkdir(parents=True, exist_ok=True)
    reusable.write_text(
        "on: workflow_call\n"
        "jobs:\n"
        "  plan: {}\n"
        "  run:\n"
        "    name: ${{ matrix.name }}\n"
        "    strategy:\n"
        "      matrix:\n"
        "        name: [lint, test]\n",
        encoding="utf-8",
    )
    _write_workflow(
        tmp_path,
        "checks.yml",
        "on: pull_request\n"
        "jobs:\n"
        "  checks:\n"
        "    uses: ./.github/workflows/wf-checks.yml\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: []\n",
    )
    _write_workflow(
        tmp_path,
        "docs.yml",
        "on: pull_request\njobs:\n  Documentation:\n    name: Documentation\n"
        "    steps: []\n",
    )
    _write_workflow(
        tmp_path,
        "wasm.yml",
        'on: pull_request\njobs:\n  wasm:\n    name: "WASM build"\n    steps: []\n',
    )

    result = checks.discover("o/r", "main", toplevel=str(tmp_path))
    assert result.refusal is None
    assert set(result.checks) == {
        "check",
        "checks / plan",
        "Documentation",
        "WASM build",
    }
    assert "checks / run" not in result.checks
    err = capsys.readouterr().err
    assert "run" in err and "matrix" in err


def test_discover_refuses_when_a_workflow_has_only_a_matrix_job(tmp_path, no_runs):
    _write_workflow(
        tmp_path,
        "ci.yml",
        "on: pull_request\n"
        "jobs:\n"
        "  build:\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [ubuntu, macos]\n"
        "    steps: []\n",
    )
    result = checks.discover("o/r", "main", toplevel=str(tmp_path))
    assert result.checks == ()
    assert result.refusal is not None
    assert "--checks" in result.refusal
    assert "ci.yml" in result.refusal
    assert "matrix" in result.refusal


def test_discover_all_certain_writes_without_refusal(tmp_path, no_runs):
    _write_workflow(
        tmp_path,
        "ci.yml",
        "on: pull_request\njobs:\n  build:\n    name: Build\n    steps: []\n",
    )
    result = checks.discover("o/r", "main", toplevel=str(tmp_path))
    assert result.refusal is None
    assert result.checks == ("Build",)


def test_discover_no_pr_workflows_is_empty_not_refusal(tmp_path, no_runs):
    result = checks.discover("o/r", "main", toplevel=str(tmp_path))
    assert result == checks.Discovery(checks=(), refusal=None)
