"""The fleet verification sweep (TOL01-WS07) — unit tests for the pure
applicability/report-assembly helpers and the orchestrator over injected
boundaries.

Per the PRD's testing decisions the WS itself is EVIDENCE-verified — the
per-tool × per-repo report is the artifact — so these tests pin only the
pure helpers (portfolio parse, applicability derivation, cell status, report
shape) and the orchestrator's mechanics through fake create/exec/remove
seams (prior art: the tool-verb recorder tests, ADR-0028).
"""

import json
import sys
from pathlib import Path

import pytest

from shipit import config, execrun, fleetsweep
from shipit.tree.create import Tree
from shipit.verbs import fleet as fleet_verb

# --------------------------------------------------------------------------
# load_portfolio — [project.portfolio] as typed entries
# --------------------------------------------------------------------------

_PORTFOLIO = {
    "project": {
        "portfolio": {
            "phos": [
                {
                    "repo": "phos-editor/app",
                    "path": "phos/phos-app",
                    "expect_verify_fail": "needs sibling checkouts",
                },
                {"repo": "phos-editor/core", "path": "phos/phos-core"},
            ],
            "lex": [{"repo": "lex-fmt/lex", "path": "lex-fmt/lex"}],
        }
    }
}


def test_load_portfolio_declaration_order_and_fields():
    entries = fleetsweep.load_portfolio(_PORTFOLIO)
    assert [e.repo for e in entries] == [
        "phos-editor/app",
        "phos-editor/core",
        "lex-fmt/lex",
    ]
    assert entries[0].stack == "phos"
    assert entries[0].path == "phos/phos-app"
    assert entries[0].expect_verify_fail == "needs sibling checkouts"
    assert entries[1].expect_verify_fail is None
    assert entries[2].stack == "lex"


def test_load_portfolio_missing_table_is_a_config_error():
    with pytest.raises(config.ConfigError, match=r"no \[project.portfolio\]"):
        fleetsweep.load_portfolio({"project": {}})
    with pytest.raises(config.ConfigError):
        fleetsweep.load_portfolio({})


def test_load_portfolio_reads_the_custom_alias():
    cfg = {"custom": {"portfolio": {"s": [{"repo": "a/b", "path": "b"}]}}}
    assert fleetsweep.load_portfolio(cfg)[0].repo == "a/b"


@pytest.mark.parametrize(
    "entry",
    [
        "not-a-table",
        {"path": "x"},  # no repo
        {"repo": "not-a-slug", "path": "x"},  # malformed slug
        {"repo": "a/b"},  # no path
        {"repo": "a/b", "path": "x", "expect_verify_fail": ""},  # empty reason
    ],
)
def test_load_portfolio_malformed_entry_names_itself(entry):
    cfg = {"project": {"portfolio": {"stack": [entry]}}}
    with pytest.raises(config.ConfigError, match=r"\[project.portfolio\].stack\[0\]"):
        fleetsweep.load_portfolio(cfg)


def test_load_portfolio_tolerates_unknown_keys():
    # [project] is the un-policed consumer namespace: only the fields the
    # sweep consumes are validated, extra keys pass through untouched.
    cfg = {"project": {"portfolio": {"s": [{"repo": "a/b", "path": "b", "x": 1}]}}}
    assert fleetsweep.load_portfolio(cfg)[0].repo == "a/b"


# --------------------------------------------------------------------------
# Applicability — derived from the repo's own declarations
# --------------------------------------------------------------------------


def _plan_map(plans):
    return {p.tool: p for p in plans}


def test_derive_plans_lint_and_test_apply_everywhere():
    plans = _plan_map(
        fleetsweep.derive_plans(
            legs_declared=False, e2e_declared=False, changelog_dir=False
        )
    )
    assert plans["lint"].applicable and plans["test"].applicable


def test_derive_plans_not_applicable_cells_carry_reasons():
    plans = _plan_map(
        fleetsweep.derive_plans(
            legs_declared=False, e2e_declared=False, changelog_dir=False
        )
    )
    assert not plans["build"].applicable
    assert "[toolchains]" in plans["build"].reason
    assert not plans["e2e"].applicable
    assert "no e2e harness declared" in plans["e2e"].reason
    assert not plans["changelog"].applicable
    assert "CHANGELOG" in plans["changelog"].reason


def test_derive_plans_declarations_switch_each_tool_on():
    plans = _plan_map(
        fleetsweep.derive_plans(
            legs_declared=True, e2e_declared=True, changelog_dir=True
        )
    )
    assert all(p.applicable for p in plans.values())
    assert all(p.reason is None for p in plans.values())


def test_plan_tools_reads_the_tree_declarations(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        '[toolchains]\n"." = "python"\n\n[artifacts.cli]\ne2e = {}\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG").mkdir()
    plans = _plan_map(fleetsweep.plan_tools(tmp_path))
    assert all(p.applicable for p in plans.values())


def test_plan_tools_absent_declarations_are_not_applicable(tmp_path):
    (tmp_path / ".shipit.toml").write_text("[secrets]\n", encoding="utf-8")
    plans = _plan_map(fleetsweep.plan_tools(tmp_path))
    assert plans["lint"].applicable and plans["test"].applicable
    assert not plans["build"].applicable
    assert not plans["e2e"].applicable
    assert not plans["changelog"].applicable


def test_plan_tools_unreadable_config_runs_config_tools_changelog_follows_fs(tmp_path):
    # A missing/malformed .shipit.toml makes the CONFIG-borne facts unprovable,
    # so lint/test/build/e2e all run and fail with their own diagnosis — a red
    # cell, never a silent skip. But CHANGELOG/ presence is a FILESYSTEM fact,
    # provable regardless of the config: changelog stays not-applicable when the
    # dir is absent even in the error fallback, and applies once it exists.
    for label, setup in (
        ("missing config", lambda: None),
        (
            "malformed config",
            lambda: (tmp_path / ".shipit.toml").write_text(
                "not = valid = toml", encoding="utf-8"
            ),
        ),
    ):
        setup()
        plans = _plan_map(fleetsweep.plan_tools(tmp_path))
        assert all(plans[t].applicable for t in ("lint", "test", "build", "e2e")), label
        assert not plans["changelog"].applicable, label
    # The convention on disk flips changelog applicable even with unreadable config.
    (tmp_path / "CHANGELOG").mkdir()
    assert all(p.applicable for p in fleetsweep.plan_tools(tmp_path))


# --------------------------------------------------------------------------
# cell_status — the expected-fail carve-out
# --------------------------------------------------------------------------


def test_cell_status_green_beats_a_declared_expectation():
    assert fleetsweep.cell_status(0, "reason") == (fleetsweep.STATUS_PASS, None)


def test_cell_status_red_without_a_declaration():
    assert fleetsweep.cell_status(1, None) == (fleetsweep.STATUS_FAIL, None)


def test_cell_status_declared_expectation_is_expected_fail_with_reason():
    assert fleetsweep.cell_status(2, "needs siblings") == (
        fleetsweep.STATUS_EXPECTED_FAIL,
        "needs siblings",
    )


# --------------------------------------------------------------------------
# Report shape — red cells reproduce, absent-not-null, ADP02 seed
# --------------------------------------------------------------------------

_ENTRY = fleetsweep.PortfolioEntry(stack="s", repo="a/b", path="b")
_XENTRY = fleetsweep.PortfolioEntry(
    stack="s", repo="c/d", path="d", expect_verify_fail="declared reason"
)


def _report(rows):
    return fleetsweep.SweepReport(
        candidate_build="cafe" * 10,
        generated_at="2026-07-09T00:00:00+00:00",
        tools=fleetsweep.SWEEP_TOOLS,
        repos=tuple(rows),
    )


def test_red_cell_carries_exact_command_and_raw_output():
    cell = fleetsweep.Cell(
        "test",
        fleetsweep.STATUS_FAIL,
        argv=("/t/bin/shipit", "test"),
        cwd="/t",
        rc=1,
        duration_ms=5,
        output="1 failed",
    )
    data = fleetsweep.RepoResult(_ENTRY, (cell,)).to_dict()["cells"]["test"]
    assert data["command"] == "/t/bin/shipit test"
    assert data["argv"] == ["/t/bin/shipit", "test"]
    assert data["cwd"] == "/t"
    assert data["rc"] == 1
    assert data["output"] == "1 failed"


def test_green_and_na_cells_stay_lean():
    green = fleetsweep.Cell(
        "lint", fleetsweep.STATUS_PASS, argv=("x",), cwd="/t", rc=0, duration_ms=1
    ).to_dict()
    assert "output" not in green and "reason" not in green
    na = fleetsweep.Cell(
        "e2e", fleetsweep.STATUS_NOT_APPLICABLE, reason="no e2e harness declared"
    ).to_dict()
    assert na == {"status": "not-applicable", "reason": "no e2e harness declared"}


def test_adoption_ready_and_summaries():
    green = fleetsweep.RepoResult(
        _ENTRY, (fleetsweep.Cell("lint", fleetsweep.STATUS_PASS),)
    )
    red = fleetsweep.RepoResult(
        _ENTRY, (fleetsweep.Cell("test", fleetsweep.STATUS_FAIL),)
    )
    xfail = fleetsweep.RepoResult(
        _XENTRY,
        (
            fleetsweep.Cell(
                "test", fleetsweep.STATUS_EXPECTED_FAIL, reason="declared reason"
            ),
        ),
    )
    assert green.adoption_ready
    assert "adoption-ready" in green.summary()
    assert not red.adoption_ready
    assert "1 red cell(s): test" in red.summary()
    # expected-fail is distinct from red AND from green: not adoption-ready,
    # but not a red cell either.
    assert not xfail.adoption_ready
    assert not xfail.red
    assert "declared reason" in xfail.summary()


def test_report_verdict_and_exit_gate():
    green = fleetsweep.RepoResult(
        _ENTRY, (fleetsweep.Cell("lint", fleetsweep.STATUS_PASS),)
    )
    xfail = fleetsweep.RepoResult(
        _XENTRY,
        (fleetsweep.Cell("test", fleetsweep.STATUS_EXPECTED_FAIL, reason="r"),),
    )
    red = fleetsweep.RepoResult(
        _ENTRY, (fleetsweep.Cell("test", fleetsweep.STATUS_FAIL),)
    )
    # every applicable cell green or declared expected-fail → the gate holds
    assert _report([green, xfail]).verdict() == 0
    assert _report([green, red]).verdict() == 1


def test_report_to_dict_says_it_seeds_adp02():
    report = _report(
        [
            fleetsweep.RepoResult(
                _ENTRY, (fleetsweep.Cell("lint", fleetsweep.STATUS_PASS),)
            )
        ]
    )
    data = report.to_dict()
    assert data["kind"] == "fleet-sweep-report"
    assert "ADP02" in data["consumer"]
    assert data["adoption_ready"] == ["a/b"]
    assert data["candidate_build"] == "cafe" * 10
    assert data["repos"][0]["summary"].startswith("a/b: adoption-ready")


def test_format_sweep_renders_the_matrix():
    rows = [
        fleetsweep.RepoResult(
            _ENTRY,
            (
                fleetsweep.Cell("lint", fleetsweep.STATUS_PASS),
                fleetsweep.Cell("test", fleetsweep.STATUS_FAIL),
                fleetsweep.Cell("build", fleetsweep.STATUS_NOT_APPLICABLE, reason="r"),
                fleetsweep.Cell(
                    "e2e", fleetsweep.STATUS_EXPECTED_FAIL, reason="declared"
                ),
                fleetsweep.Cell("changelog", fleetsweep.STATUS_PASS),
            ),
        )
    ]
    text = fleet_verb.format_sweep(_report(rows))
    header, row = text.splitlines()[0], text.splitlines()[1]
    assert header.split() == ["REPO", "LINT", "TEST", "BUILD", "E2E", "CHANGELOG"]
    assert row.split() == ["a/b", "pass", "FAIL", "n/a", "xfail", "pass"]
    assert "1 red cell(s)" in text


# --------------------------------------------------------------------------
# The orchestrator — over injected create/exec/remove boundaries
# --------------------------------------------------------------------------


class _FakeExec:
    """Records (argv, cwd, env); returns a scripted rc per tool subcommand."""

    def __init__(self, rcs=None):
        self.calls: list[tuple[tuple[str, ...], Path, dict]] = []
        self.rcs = rcs or {}

    def __call__(self, argv, cwd, env):
        self.calls.append((tuple(argv), Path(cwd), dict(env)))
        rc = self.rcs.get(argv[1], 0)
        return execrun.ExecResult(
            argv=tuple(argv), rc=rc, stdout=f"{argv[1]} out\n", stderr="", duration_ms=1
        )


def _tree_factory(tmp_path, toml='[toolchains]\n"." = "python"\n'):
    """A fake create_tree: materializes a Tree dir with a launcher + config."""

    def create(entry):
        root = tmp_path / "trees" / entry.repo.replace("/", "-")
        (root / "bin").mkdir(parents=True)
        launcher = root / "bin" / "shipit"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        (root / ".shipit.toml").write_text(toml, encoding="utf-8")
        return Tree(path=str(root), branch="fleet-sweep-x", base="origin/main")

    return create


def _sweep(entries, tmp_path, *, exec_fake=None, removed=None, **kwargs):
    exec_fake = exec_fake or _FakeExec()
    removed = removed if removed is not None else []
    report = fleetsweep.sweep(
        entries,
        candidate=Path("/cand/shipit"),
        candidate_build="beef" * 10,
        generated_at="now",
        source_root=tmp_path / "src",
        create_tree=kwargs.pop("create_tree", _tree_factory(tmp_path)),
        run_tool=exec_fake,
        remove_tree=removed.append,
        **kwargs,
    )
    return report, exec_fake, removed


def test_sweep_runs_applicable_tools_through_the_tree_launcher(tmp_path):
    report, exec_fake, removed = _sweep([_ENTRY], tmp_path)
    # lint/test/build ran (map declared); e2e + changelog recorded n/a.
    ran = [argv[1:] for argv, _, _ in exec_fake.calls]
    assert ran == [("lint",), ("test",), ("build",)]
    row = report.repos[0]
    by_tool = {c.tool: c for c in row.cells}
    assert by_tool["e2e"].status == fleetsweep.STATUS_NOT_APPLICABLE
    assert by_tool["changelog"].status == fleetsweep.STATUS_NOT_APPLICABLE
    assert row.adoption_ready
    # every executed argv heads the TREE's managed launcher (ADR-0033).
    for argv, cwd, _ in exec_fake.calls:
        assert argv[0] == str(cwd / "bin" / "shipit")
    # the Tree was torn down after its row.
    assert removed == [Path(report.repos[0].cells[0].cwd)]


def test_sweep_sets_the_sanctioned_shipit_exec_override(tmp_path):
    _, exec_fake, _ = _sweep([_ENTRY], tmp_path)
    assert all(env["SHIPIT_EXEC"] == "/cand/shipit" for _, _, env in exec_fake.calls)


def test_run_tool_scrubs_leaked_pixi_env_but_keeps_path(monkeypatch, tmp_path):
    # The swept child must be hermetic: a leaked parent PIXI_* project pointer
    # (present when the sweep runs from shipit's own pixi env) would bind the
    # tool to the COORDINATOR checkout, not its freshly provisioned Tree. The
    # scrub drops it while keeping PATH and the SHIPIT_EXEC override, and the
    # env is passed as the COMPLETE child env (replace_env=True) so no dropped
    # pointer can creep back in via a merge over os.environ.
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/coordinator/pixi.toml")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(fleetsweep.execrun, "run", fake_run)
    fleetsweep._run_tool(
        ["/t/bin/shipit", "lint"], tmp_path, {"SHIPIT_EXEC": "/cand/shipit"}
    )
    assert captured["replace_env"] is True
    env = captured["env"]
    assert "PIXI_PROJECT_MANIFEST" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["SHIPIT_EXEC"] == "/cand/shipit"


def test_sweep_red_cell_carries_command_and_output(tmp_path):
    exec_fake = _FakeExec(rcs={"test": 1})
    report, _, _ = _sweep([_ENTRY], tmp_path, exec_fake=exec_fake)
    cell = {c.tool: c for c in report.repos[0].cells}["test"]
    assert cell.status == fleetsweep.STATUS_FAIL
    assert cell.argv is not None and cell.argv[-1] == "test"
    assert cell.output == "test out\n"
    assert report.verdict() == 1


def test_sweep_declared_expectation_renders_expected_fail(tmp_path):
    exec_fake = _FakeExec(rcs={"lint": 1})
    report, _, _ = _sweep([_XENTRY], tmp_path, exec_fake=exec_fake)
    cell = {c.tool: c for c in report.repos[0].cells}["lint"]
    assert cell.status == fleetsweep.STATUS_EXPECTED_FAIL
    assert cell.reason == "declared reason"
    # expected-fail holds no gate red…
    assert report.verdict() == 0
    # …but the repo is not adoption-ready.
    assert not report.repos[0].adoption_ready


def test_sweep_empty_tool_selection_refuses_loud(tmp_path):
    # An empty selection — an empty tools tuple or only names outside
    # SWEEP_TOOLS — would run nothing yet report 0 red cells (a false green
    # exit gate). The domain function must refuse, not return a trivial pass.
    with pytest.raises(fleetsweep.SweepError, match="no swept tools selected"):
        _sweep([_ENTRY], tmp_path, tools=())
    with pytest.raises(fleetsweep.SweepError, match="no swept tools selected"):
        _sweep([_ENTRY], tmp_path, tools=("bogus",))


def test_sweep_tool_filter_narrows_the_run(tmp_path):
    report, exec_fake, _ = _sweep([_ENTRY], tmp_path, tools=("test",))
    assert [argv[1:] for argv, _, _ in exec_fake.calls] == [("test",)]
    assert [c.tool for c in report.repos[0].cells] == ["test"]


def test_sweep_keep_trees_skips_teardown(tmp_path):
    _, _, removed = _sweep([_ENTRY], tmp_path, keep_trees=True)
    assert removed == []


def test_sweep_tree_create_failure_is_a_red_row_not_a_gap(tmp_path):
    def broken(entry):
        raise fleetsweep.SweepError("source checkout missing")

    report, exec_fake, _ = _sweep([_ENTRY], tmp_path, create_tree=broken)
    assert exec_fake.calls == []
    row = report.repos[0]
    assert row.cells  # the row is present, never silently skipped
    assert all(
        c.status == fleetsweep.STATUS_FAIL
        for c in row.cells
        if c.status != fleetsweep.STATUS_NOT_APPLICABLE
    )
    assert "tree create failed" in row.cells[0].output
    assert report.verdict() == 1


def test_sweep_missing_launcher_is_a_red_cell(tmp_path):
    def bare_tree(entry):
        root = tmp_path / "bare"
        root.mkdir()
        (root / ".shipit.toml").write_text(
            '[toolchains]\n"." = "python"\n', encoding="utf-8"
        )
        return Tree(path=str(root), branch="b", base="origin/main")

    report, exec_fake, _ = _sweep([_ENTRY], tmp_path, create_tree=bare_tree)
    assert exec_fake.calls == []
    cell = {c.tool: c for c in report.repos[0].cells}["lint"]
    assert cell.status == fleetsweep.STATUS_FAIL
    assert "launcher missing" in cell.output


# --------------------------------------------------------------------------
# The verb — portfolio read, selectors, report artifact
# --------------------------------------------------------------------------

_TOML = """
[project.portfolio]
s = [
  { repo = "a/b", path = "b" },
  { repo = "c/d", path = "d", expect_verify_fail = "declared reason" },
]
"""


@pytest.fixture
def portfolio_repo(tmp_path, monkeypatch):
    (tmp_path / ".shipit.toml").write_text(_TOML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _fake_sweep(entries, **kwargs):
    rows = tuple(
        fleetsweep.RepoResult(e, (fleetsweep.Cell("lint", fleetsweep.STATUS_PASS),))
        for e in entries
    )
    return fleetsweep.SweepReport(
        candidate_build=kwargs.get("candidate_build"),
        generated_at=kwargs.get("generated_at", "now"),
        tools=tuple(kwargs.get("tools", fleetsweep.SWEEP_TOOLS)),
        repos=rows,
    )


def test_run_sweep_writes_the_report_artifact(portfolio_repo, capsys):
    rc = fleet_verb.run_sweep(shipit_exec=None, sweep_fn=_fake_sweep)
    assert rc == 0
    artifact = portfolio_repo / fleetsweep.REPORT_PATH
    assert artifact.is_file()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["kind"] == "fleet-sweep-report"
    assert [r["repo"] for r in data["repos"]] == ["a/b", "c/d"]
    assert str(fleetsweep.REPORT_PATH) in capsys.readouterr().out


def test_run_sweep_json_stdout_is_a_single_json_document(portfolio_repo, capsys):
    # Under --json, stdout is the machine surface: the "report written" courtesy
    # note must be suppressed so stdout stays ONE valid JSON document (emit()
    # already wrote it) — a full sweep with --json still persists the artifact.
    rc = fleet_verb.run_sweep(shipit_exec=None, as_json=True, sweep_fn=_fake_sweep)
    assert rc == 0
    out = capsys.readouterr().out
    assert "report written" not in out
    data = json.loads(out)  # parses clean → not corrupted by a trailing line
    assert data["kind"] == "fleet-sweep-report"


def test_run_sweep_filtered_run_never_clobbers_the_evidence(portfolio_repo):
    rc = fleet_verb.run_sweep(repos=("a/b",), sweep_fn=_fake_sweep)
    assert rc == 0
    assert not (portfolio_repo / fleetsweep.REPORT_PATH).exists()


def test_run_sweep_explicit_out_wins(portfolio_repo):
    out = portfolio_repo / "partial.json"
    rc = fleet_verb.run_sweep(repos=("a/b",), out=out, sweep_fn=_fake_sweep)
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["repos"][0]["repo"] == "a/b"


def test_run_sweep_unknown_repo_selector_is_a_clean_refusal(portfolio_repo, capsys):
    rc = fleet_verb.run_sweep(repos=("nope/nope",), sweep_fn=_fake_sweep)
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "nope/nope" in err


def test_run_sweep_repo_selector_matches_case_insensitively(portfolio_repo):
    # Selectors and portfolio slugs are normalized through the canonical parser,
    # so a differently-cased selector still finds its repo (not "unknown").
    out = portfolio_repo / "partial.json"
    rc = fleet_verb.run_sweep(repos=("A/B",), out=out, sweep_fn=_fake_sweep)
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["repos"][0]["repo"] == "a/b"


def test_run_sweep_malformed_repo_selector_is_rejected_as_invalid(
    portfolio_repo, capsys
):
    # A malformed selector is an INVALID slug, distinct from a well-formed slug
    # that is merely absent from the portfolio.
    rc = fleet_verb.run_sweep(repos=("notaslug",), sweep_fn=_fake_sweep)
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "invalid --repo selector" in err


def test_resolve_candidate_explicit_must_be_executable(tmp_path):
    exe = tmp_path / "shipit"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(fleetsweep.SweepError):
        fleetsweep.resolve_candidate(exe)  # exists but not executable
    exe.chmod(0o755)
    assert fleetsweep.resolve_candidate(exe) == exe.resolve()
    with pytest.raises(fleetsweep.SweepError):
        fleetsweep.resolve_candidate(tmp_path / "absent")


def test_resolve_candidate_implicit_resolves_bare_argv0_via_path(tmp_path, monkeypatch):
    # The default candidate is the running build's entrypoint. Launched off PATH,
    # sys.argv[0] is a bare name ("shipit"), not a cwd file — resolve_candidate
    # must find it on PATH rather than refusing the executable running build.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "shipit"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "argv", ["shipit", "fleet", "sweep"])
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.chdir(tmp_path)  # cwd has no bare "shipit" file at its root
    assert fleetsweep.resolve_candidate() == exe.resolve()


def test_fleet_group_is_attached_to_the_cli():
    from shipit.cli import root

    assert "fleet" in root.commands
    assert "sweep" in root.commands["fleet"].commands
