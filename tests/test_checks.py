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
    # The @vN stage pins the release caller dispatches — deduped and sorted;
    # a `./` local ref and a step `actions/checkout@v6` (no .yml path) are not
    # pins and never appear (#917).
    _write_workflow(tmp_path, checks.RELEASE_CALLER_WORKFLOW, _RELEASE_CALLER)
    pins = checks.workflow_pin_refs(_caller_path(tmp_path))
    assert pins == [("arthur-debert/shipit", "v1")]


def test_workflow_pin_refs_dedupes_across_jobs_and_keeps_distinct_refs(tmp_path):
    # Deduped over the caller's jobs; distinct @vN refs are kept distinct.
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
    # The gate is release-specific: an unrelated CI/manual workflow with a
    # stale cross-repo pin is NOT part of the release dispatch and must never
    # block a cut (#917). `workflow_pin_refs` reads ONLY the caller path.
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
    assert pins == [("o/r", "v1")]  # the unrelated @v9 never appears


def test_workflow_pin_refs_filters_to_the_vn_shape(tmp_path):
    # Only floating-major @vN refs are gated: a @main, a SHA, and a @v1.2.3
    # release tag are outside the bootstrap contract (advance-major moves a
    # v-major BRANCH — ADR-0010) and must not draw the phantom "bootstrap the
    # v-major branch" remediation (#917, copilot finding).
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
    # An absent caller file → no pins (a different failure, not this gate's).
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
    ctxs, dropped = checks._job_contexts("call", job, toplevel=None, cache=cache)
    # e2e is excluded (inputs.e2e not enabled); the bare caller name is never used.
    assert ctxs == ["call / build", "call / bats"]
    assert dropped == []


# --------------------------------------------------------------------------
# Static drop-and-refuse — the #1056 phantom `<caller> / run` guard.
# --------------------------------------------------------------------------


def test_job_unpredictable_matrix_and_dynamic_name():
    assert checks.job_unpredictable({"strategy": {"matrix": {"os": ["a", "b"]}}}) == (
        "matrix"
    )
    assert checks.job_unpredictable({"name": "${{ matrix.name }}"}) == "dynamic name"
    # Matrix wins over a dynamic name — both are the same unpredictability.
    assert (
        checks.job_unpredictable(
            {"name": "${{ matrix.name }}", "strategy": {"matrix": {"x": [1]}}}
        )
        == "matrix"
    )
    # A predictable job — static name or no name at all.
    assert checks.job_unpredictable({"name": "Build"}) is None
    assert checks.job_unpredictable({}) is None
    assert checks.job_unpredictable("nonsense") is None


def test_job_contexts_drops_matrix_job_instead_of_guessing_id():
    # A matrix job reports `id (values)`, never the bare id — so it is DROPPED,
    # not resolved to `run` (the phantom that bricked lex — #1056).
    ctxs, dropped = checks._job_contexts(
        "run",
        {"strategy": {"matrix": {"name": ["lint", "test"]}}},
        toplevel=None,
        cache={},
    )
    assert ctxs == []
    assert dropped == [checks.DroppedJob(job="run", reason="matrix")]


def test_job_contexts_drops_nested_matrix_job_caller_prefixed():
    # lex's exact shape: a caller job → reusable wf with a static `plan` and a
    # matrix `run`. `plan` is named `checks / plan`; `run` is dropped, and the
    # drop is surfaced caller-prefixed so the warning names the full path.
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
    """Force the runs-based path empty so discover falls to static (the
    onboarding case) without any network call."""
    monkeypatch.setattr(checks, "checks_from_runs", lambda *a, **k: [])


def test_discover_lex_shape_names_certain_set_never_phantom_run(
    tmp_path, capsys, no_runs
):
    # Mirror lex: a caller workflow with a `checks` job (→ reusable wf-checks
    # with static `plan` + matrix `run`) and a sibling aggregator `check`, plus
    # `Documentation` and `WASM build` workflows. Discovery must yield exactly
    # the certain set and NEVER `checks / run`.
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
    # The drop is warned loudly on stderr.
    err = capsys.readouterr().err
    assert "run" in err and "matrix" in err


def test_discover_refuses_when_a_workflow_has_only_a_matrix_job(tmp_path, no_runs):
    # A PR workflow whose ONLY job is a bare matrix job contributes zero certain
    # contexts — discovery refuses and demands --checks rather than write a rule
    # that omits the workflow's real gate (#1056).
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
    # Plain single-job workflow — a certain context, no refusal.
    _write_workflow(
        tmp_path,
        "ci.yml",
        "on: pull_request\njobs:\n  build:\n    name: Build\n    steps: []\n",
    )
    result = checks.discover("o/r", "main", toplevel=str(tmp_path))
    assert result.refusal is None
    assert result.checks == ("Build",)


def test_discover_no_pr_workflows_is_empty_not_refusal(tmp_path, no_runs):
    # No PR-check workflow at all — an honest empty set, NOT a refusal.
    result = checks.discover("o/r", "main", toplevel=str(tmp_path))
    assert result == checks.Discovery(checks=(), refusal=None)
