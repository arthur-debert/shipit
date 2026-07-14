"""Unit tests for `shipit wf verify-canary` — the standing sign e2e (#899).

Three layers:

* PURE CORES — the per-mode version derivation, the stage-input threading
  table (workflows.lex §8: each artifact-consuming relay stage names its
  predecessor's run — sign the build run, publish the SIGN run; build rides
  the tag alone, its `notes` job re-deriving release-notes there), new-run
  discovery, and the proof / teardown renderings — fixture-tested with no
  boundary anywhere near them. Plus the relay-availability drift guard:
  the threading table modelled against the block files' actual artifact
  uploads/downloads (the #902 review scar — sign named a build run that
  did not carry `release-notes`).

* THE gh ADAPTER SURFACE — the three adapter reads/writes the dispatcher
  rides (:func:`shipit.gh.workflow_run` / :func:`shipit.gh.run_list_dispatched`
  / :func:`shipit.gh.run_verdict`) pinned through a captured ``gh._run``
  (the test_gh_adapter.py pattern): argv shape asserted, no subprocess.

* THE VERB FLOW — a SCRIPTED GitHub faked at the adapter boundary (ADR-0028:
  the package is unit-testable by patching the one gh module): dispatch
  order, run-id threading, the relay's stop-on-red, both-mode version
  splitting, and the dispatch-timeout verdict, with no network involved.

There is deliberately NO live smoke here: the verb's whole point is real
GitHub runs on the canary (real macOS runner, real notarization minutes), so
its acceptance run is the operator invocation the workflows.lex §9 runbook
prescribes, never part of `pixi run test`.
"""

from __future__ import annotations

import itertools
import json
import re

import pytest
from test_ci_workflow import _load

from shipit import gh
from shipit.verbs import wf, wf_canary

# --------------------------------------------------------------------------
# Pure cores — versions and input threading
# --------------------------------------------------------------------------


def test_tag_is_v_prefixed():
    assert wf_canary.tag_for("1.2.3-rc") == "v1.2.3-rc"


def test_single_modes_use_the_version_verbatim():
    assert wf_canary.mode_versions("1.2.3", "full") == {"full": "1.2.3"}
    assert wf_canary.mode_versions("1.2.3", "staged") == {"staged": "1.2.3"}


def test_both_mode_derives_two_distinct_semver_versions():
    versions = wf_canary.mode_versions("1.2.3", "both")
    assert versions == {"full": "1.2.3-full", "staged": "1.2.3-staged"}


def test_both_mode_extends_an_existing_prerelease_with_a_dot_identifier():
    versions = wf_canary.mode_versions("1.2.3-canary-rc", "both")
    assert versions == {
        "full": "1.2.3-canary-rc.full",
        "staged": "1.2.3-canary-rc.staged",
    }


def test_full_and_prepare_dispatch_on_version():
    for stage in ("full", "prepare"):
        inputs = wf_canary.stage_inputs(stage, version="1.2.3")
        assert inputs == {"stage": stage, "version": "1.2.3"}


def test_each_relay_stage_names_its_predecessors_run():
    # workflows.lex §8: build dispatches on the tag alone (its `notes` job
    # re-derives release-notes there, #898); sign names the BUILD run,
    # publish the SIGN run.
    run_ids = {"prepare": 1, "build": 2, "sign": 3}
    build = wf_canary.stage_inputs("build", version="1.2.3", run_ids=run_ids)
    assert build == {"stage": "build", "tag": "v1.2.3"}
    sign = wf_canary.stage_inputs("sign", version="1.2.3", run_ids=run_ids)
    assert sign == {"stage": "sign", "tag": "v1.2.3", "run-id": "2"}
    publish = wf_canary.stage_inputs("publish", version="1.2.3", run_ids=run_ids)
    assert publish == {"stage": "publish", "tag": "v1.2.3", "run-id": "3"}


def test_a_missing_source_run_is_a_loud_caller_bug():
    with pytest.raises(KeyError):
        wf_canary.stage_inputs("sign", version="1.2.3", run_ids={})


def test_relay_stages_are_the_blessed_callers_stage_choices():
    # Drift guard: the relay dispatches ride the SAME closed `stage` choice
    # set the blessed caller offers and `wf test` lints (workflows.lex §8).
    for stage in wf_canary.RELAY_ORDER:
        assert stage in wf.CALLER_STAGES
    assert wf_canary.MODE_FULL in wf.CALLER_STAGES
    assert set(wf_canary.RELAY_SOURCE) == set(wf_canary.RELAY_ORDER)


#: Relay stage → the stage block its caller job routes to (workflows.lex §8).
_RELAY_BLOCKS = {
    "prepare": "wf-prepare.yml",
    "build": "wf-build.yml",
    "sign": "wf-sign-mac.yml",
    "publish": "wf-publish.yml",
}


def _family(name: str) -> str:
    """An artifact name/pattern reduced to its family: ``bundle-${{ … }}``
    and ``bundle-*`` -> ``bundle``, ``release-notes`` -> itself."""
    return re.split(r"\$\{\{|\*", name)[0].rstrip("-")


def _artifact_steps(block: str, action: str) -> list[dict]:
    return [
        step
        for job in _load(block)["jobs"].values()
        for step in job.get("steps", [])
        if f"{action}-artifact" in step.get("uses", "")
    ]


def test_every_relay_source_run_carries_what_its_consumer_downloads():
    # The #902 review scar, drift-pinned against the block files: the relay
    # once had sign name a build run that did not carry `release-notes`
    # (prepare's artifact), so a staged sign dispatch died at its own
    # carry-notes download. Model the relay: a stage's run HOLDS every
    # family its block uploads (its own produce plus the standalone
    # carry-forward / re-derivation jobs — active on the relay path, where
    # every dispatch is standalone); a stage NEEDS every family its block
    # downloads CROSS-RUN
    # (steps naming `run-id`). Every need must be present in the run
    # RELAY_SOURCE names as that stage's source.
    uploads = {
        stage: {_family(s["with"]["name"]) for s in _artifact_steps(block, "upload")}
        for stage, block in _RELAY_BLOCKS.items()
    }
    for stage, block in _RELAY_BLOCKS.items():
        needs = {
            _family(s["with"].get("name") or s["with"]["pattern"])
            for s in _artifact_steps(block, "download")
            if "run-id" in s["with"]
        }
        source = wf_canary.RELAY_SOURCE[stage]
        if source is None:
            assert not needs, (stage, needs)
        else:
            missing = needs - uploads[source]
            assert not missing, (stage, source, missing)
    # The scar itself, stated directly: the build run holds the notes (its
    # `notes` job re-derives them at the tag, #898), so it is a complete
    # source for the sign dispatch that names it.
    assert "release-notes" in uploads["build"]


def test_new_run_is_none_until_a_fresh_run_appears():
    runs = [{"databaseId": 1}, {"databaseId": 2}]
    assert wf_canary.new_run(runs, {1, 2}) is None


def test_new_run_picks_the_newest_unseen_run():
    runs = [{"databaseId": 3}, {"databaseId": 4}, {"databaseId": 1}]
    assert wf_canary.new_run(runs, {1})["databaseId"] == 4


def test_proof_block_cites_every_step_with_tag_and_url():
    steps = [
        wf_canary.ChainStep("full", "full", "1.2.3-full", 7, "https://u/7", "success"),
        wf_canary.ChainStep("staged", "sign", "1.2.3-staged", None, "", "skipped"),
    ]
    block = wf_canary.proof_block(steps)
    assert "CANARY PROOF" in block
    assert "v1.2.3-full" in block and "https://u/7" in block
    assert "staged/sign" in block and "skipped" in block


def test_teardown_block_prints_one_delete_per_rc_tag():
    block = wf_canary.teardown_block(
        "o/r", {"full": "1.2.3-full", "staged": "1.2.3-staged"}
    )
    assert "gh release delete v1.2.3-full -R o/r --yes --cleanup-tag" in block
    assert "gh release delete v1.2.3-staged -R o/r --yes --cleanup-tag" in block


# --------------------------------------------------------------------------
# The gh adapter surface — captured argv, no subprocess (test_gh_adapter.py
# pattern)
# --------------------------------------------------------------------------


def _capture_run(monkeypatch, stdout: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return stdout

    monkeypatch.setattr(gh, "_run", fake_run)
    return calls


def test_workflow_run_encodes_repo_ref_and_ordered_fields(monkeypatch):
    calls = _capture_run(monkeypatch, "")
    gh.workflow_run(
        "caller.yml", repo="o/r", ref="main", fields={"stage": "full", "version": "1"}
    )
    (argv,) = calls
    assert argv[:4] == ["gh", "workflow", "run", "caller.yml"]
    assert argv[argv.index("-R") + 1] == "o/r"
    assert argv[argv.index("--ref") + 1] == "main"
    fields = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
    assert fields == ["stage=full", "version=1"]


def test_run_list_dispatched_scopes_to_the_callers_dispatch_runs(monkeypatch):
    calls = _capture_run(monkeypatch, json.dumps([{"databaseId": 5}]))
    assert gh.run_list_dispatched("o/r", "caller.yml") == [{"databaseId": 5}]
    (argv,) = calls
    assert argv[:3] == ["gh", "run", "list"]
    assert argv[argv.index("--workflow") + 1] == "caller.yml"
    assert argv[argv.index("--event") + 1] == "workflow_dispatch"
    assert "databaseId" in argv[argv.index("--json") + 1]


def test_run_verdict_reads_one_runs_conclusion(monkeypatch):
    calls = _capture_run(
        monkeypatch, json.dumps({"status": "completed", "conclusion": "success"})
    )
    doc = gh.run_verdict("o/r", 42)
    assert doc["conclusion"] == "success"
    (argv,) = calls
    assert argv[:4] == ["gh", "run", "view", "42"]
    assert argv[argv.index("--json") + 1] == "status,conclusion,url"


# --------------------------------------------------------------------------
# The verb flow — a scripted GitHub, faked at the adapter boundary
# --------------------------------------------------------------------------


class _ScriptedGh:
    """A scripted Actions surface behind the three adapter calls.

    Every ``workflow_run`` registers a fresh run (next id, completed, with
    the scripted per-stage ``conclusions``, default green);
    ``run_list_dispatched`` and ``run_verdict`` serve reads off that state.
    ``register`` can be turned off to simulate GitHub never materializing
    the dispatched run (the dispatch-timeout path).
    """

    def __init__(
        self, conclusions: dict[str, str] | None = None, register: bool = True
    ):
        self.dispatched: list[dict[str, str]] = []
        self.runs: list[dict] = []
        self.conclusions = conclusions or {}
        self.register = register
        self._next_id = 100

    def install(self, monkeypatch) -> _ScriptedGh:
        monkeypatch.setattr(gh, "workflow_run", self.workflow_run)
        monkeypatch.setattr(gh, "run_list_dispatched", self.run_list_dispatched)
        monkeypatch.setattr(gh, "run_verdict", self.run_verdict)
        return self

    def workflow_run(self, workflow, *, repo, ref, fields):
        self.dispatched.append(dict(fields))
        if not self.register:
            return
        self._next_id += 1
        self.runs.insert(
            0,
            {
                "databaseId": self._next_id,
                "status": "completed",
                "conclusion": self.conclusions.get(fields["stage"], "success"),
                "url": f"https://gh/run/{self._next_id}",
            },
        )

    def run_list_dispatched(self, repo, workflow, *, limit=20):
        return list(self.runs)

    def run_verdict(self, repo, run_id):
        run = next(r for r in self.runs if r["databaseId"] == run_id)
        return {k: run[k] for k in ("status", "conclusion", "url")}


def _quiet_clock():
    """A no-op sleep + a slow monotonic that never trips a deadline."""
    ticks = itertools.count(step=0.001)
    return (lambda _s: None), (lambda: next(ticks))


def test_full_mode_dispatches_once_and_reports_green(monkeypatch, capsys):
    scripted = _ScriptedGh().install(monkeypatch)
    sleep, monotonic = _quiet_clock()
    rc = wf_canary.run("1.2.3", mode="full", sleep=sleep, monotonic=monotonic)
    assert rc == 0
    assert scripted.dispatched == [{"stage": "full", "version": "1.2.3"}]
    out = capsys.readouterr().out
    assert "CANARY PROOF" in out
    assert "gh release delete v1.2.3" in out
    assert "WF VERIFY-CANARY: OK" in out


def test_staged_mode_threads_run_ids_through_the_relay(monkeypatch, capsys):
    scripted = _ScriptedGh().install(monkeypatch)
    sleep, monotonic = _quiet_clock()
    rc = wf_canary.run("1.2.3", mode="staged", sleep=sleep, monotonic=monotonic)
    assert rc == 0
    assert [d["stage"] for d in scripted.dispatched] == list(wf_canary.RELAY_ORDER)
    # prepare rides the version; build rides the tag alone (its `notes` job
    # re-derives release-notes there, workflows.lex §8 / #898).
    assert scripted.dispatched[0] == {"stage": "prepare", "version": "1.2.3"}
    assert scripted.dispatched[1] == {"stage": "build", "tag": "v1.2.3"}
    # sign names the BUILD run; publish names the SIGN run (workflows.lex §8).
    assert scripted.dispatched[2]["run-id"] == "102"
    assert scripted.dispatched[3]["run-id"] == "103"
    assert "WF VERIFY-CANARY: OK (4 run(s) green)" in capsys.readouterr().out


def test_a_red_relay_stage_skips_the_rest_and_fails(monkeypatch, capsys):
    scripted = _ScriptedGh(conclusions={"sign": "failure"}).install(monkeypatch)
    sleep, monotonic = _quiet_clock()
    rc = wf_canary.run("1.2.3", mode="staged", sleep=sleep, monotonic=monotonic)
    assert rc == 1
    # publish is never dispatched against a failed source run.
    assert [d["stage"] for d in scripted.dispatched] == ["prepare", "build", "sign"]
    out = capsys.readouterr().out
    assert "staged/sign" in out and "failure" in out
    assert "staged/publish" in out and "skipped" in out
    assert "WF VERIFY-CANARY: FAILED" in out


def test_both_mode_runs_each_chain_on_its_own_rc_version(monkeypatch, capsys):
    scripted = _ScriptedGh().install(monkeypatch)
    sleep, monotonic = _quiet_clock()
    rc = wf_canary.run("1.2.3", mode="both", sleep=sleep, monotonic=monotonic)
    assert rc == 0
    assert scripted.dispatched[0] == {"stage": "full", "version": "1.2.3-full"}
    assert {"stage": "prepare", "version": "1.2.3-staged"} in scripted.dispatched
    assert len(scripted.dispatched) == 5
    out = capsys.readouterr().out
    assert "gh release delete v1.2.3-full" in out
    assert "gh release delete v1.2.3-staged" in out


def test_a_run_that_never_registers_is_a_dispatch_timeout_verdict(monkeypatch, capsys):
    _ScriptedGh(register=False).install(monkeypatch)
    ticks = itertools.count(step=wf_canary.DISPATCH_TIMEOUT / 2)
    rc = wf_canary.run(
        "1.2.3",
        mode="full",
        sleep=lambda _s: None,
        monotonic=lambda: next(ticks),
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "dispatch-timeout" in out
    assert "WF VERIFY-CANARY: FAILED" in out


def test_the_verb_is_registered_on_the_wf_group():
    from shipit import cli

    assert "verify-canary" in cli.root.commands["wf"].commands
