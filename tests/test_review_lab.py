from __future__ import annotations

import json
import subprocess
from importlib import resources

import click
import pytest
from click.testing import CliRunner

from shipit import cli
from shipit.review import fanout, producer, replay
from shipit.review.cell import CellError, parse_cell
from shipit.review.curve import convergence_curve, render_curve_report
from shipit.review.groundtruth import parse_fixture
from shipit.review.labrun import plan_points, resolve_pins, run_cell
from shipit.spawn.launch import LaunchResult
from shipit.verbs.lab import lab_group
from shipit.verbs.lab import report as report_verb
from shipit.verbs.lab import run as run_verb

_REVIEW = json.dumps(
    {
        "summary": {"status": "COMMENT", "overall_feedback": "ok"},
        "comments": [
            {
                "file": "f.txt",
                "line": 2,
                "text": "row padding is missed when the staging buffer wraps",
                "severity": "major",
                "category": "correctness",
                "confidence": 0.9,
                "evidence": "e",
                "fix": "",
            }
        ],
    }
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "second")
    return repo


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    captured: dict = {"launches": []}

    def _launch(cmd, *, cwd, env, timeout=None):
        captured["launches"].append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return LaunchResult(returncode=0, stdout=_REVIEW, stderr="")

    captured["launch"] = _launch
    return captured


def _fixture_for(view):
    return parse_fixture(
        {
            "schema": 1,
            "version": 1,
            "prs": [
                {
                    "id": "widget-1",
                    "repo": "acme/widget",
                    "pr": 7,
                    "base_sha": str(view.base_sha),
                    "head_sha": str(view.head_sha),
                }
            ],
            "labels": [
                {
                    "id": "widget-G1",
                    "pr": "widget-1",
                    "file": "f.txt",
                    "lines": [1, 3],
                    "severity": "major",
                    "verdict": "real",
                    "confirmed": True,
                    "claim": "staging buffer row padding missed",
                    "provenance": {"kind": "fix-commit", "ref": "abc1234"},
                }
            ],
        }
    )


def _control_cell(**overrides):
    data = {
        "schema": 1,
        "id": "ctl",
        "baseline": "ctl",
        "axis": "control",
        "fixture": {"version": 1, "prs": ["widget-1"]},
        "pipeline": {"shape": "single"},
        "invocation": {"backend": "codex", "model": "pro", "timeout": "600s"},
        "sweeps": {"count": 2, "mode": "blind", "replicates": 1},
    }
    data.update(overrides)
    return parse_cell(data)


def _read_records(paths):
    records = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def _store_records(base_dir):
    return _read_records(sorted(base_dir.rglob("*.jsonl")))


def test_lab_long_form_help_command(capsys):
    rc = cli.main(["lab", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab` runs measured experiments" in out
    assert "shipit lab run CELL" in out


def test_lab_long_form_help_uses_command_named_resources():
    files = resources.files("shipit.verbs.lab")
    assert files.joinpath("lab_help.txt").is_file()
    assert files.joinpath("lab_run_help.txt").is_file()
    assert files.joinpath("lab_report_help.txt").is_file()
    assert not files.joinpath("help.txt").is_file()
    assert not files.joinpath("run_help.txt").is_file()
    assert not files.joinpath("report_help.txt").is_file()
    assert "help" in lab_group.commands
    assert run_verb.cmd.help_resource == "lab_run_help.txt"
    assert report_verb.cmd.help_resource == "lab_report_help.txt"


def test_lab_run_long_form_help_leaf(capsys):
    rc = cli.main(["lab", "run", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab run CELL` executes" in out
    assert "--checkout" in out


def test_lab_run_long_form_help_leaf_allows_trailing_options(capsys):
    rc = cli.main(["lab", "run", "help", "--force"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab run CELL` executes" in out
    assert "2 executed, 0 reused" not in out


def test_lab_run_long_form_help_leaf_allows_leading_options(capsys):
    rc = cli.main(["lab", "run", "--force", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab run CELL` executes" in out
    assert "2 executed, 0 reused" not in out


def test_lab_run_click_help_stays_terse():
    from shipit.verbs.lab import run as run_verb

    result = CliRunner().invoke(run_verb.cmd, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--checkout" in result.output
    assert "`shipit lab run CELL` executes" not in result.output


def test_lab_report_long_form_help_leaf(capsys):
    rc = cli.main(["lab", "report", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab report CELL` renders" in out
    assert "deterministic and token-free" in out


def test_lab_report_long_form_help_leaf_allows_trailing_options(capsys):
    rc = cli.main(["lab", "report", "help", "--fixture", "fixture.toml"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab report CELL` renders" in out
    assert "deterministic and token-free" in out


def test_lab_report_long_form_help_leaf_allows_leading_options(capsys):
    rc = cli.main(["lab", "report", "--fixture", "fixture.toml", "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "`shipit lab report CELL` renders" in out
    assert "deterministic and token-free" in out


def test_lab_report_click_help_stays_terse():
    from shipit.verbs.lab import report as report_verb

    result = CliRunner().invoke(report_verb.cmd, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--fixture" in result.output
    assert "`shipit lab report CELL` renders" not in result.output


def test_long_form_help_missing_resource_is_a_click_error():
    from shipit.verbs import _help

    with pytest.raises(click.ClickException) as excinfo:
        _help.load_help_text("shipit.verbs.lab", "missing_help.txt")

    assert (
        "bundled help resource shipit.verbs.lab:missing_help.txt is unavailable"
        in str(excinfo.value)
    )


def test_long_form_help_missing_package_is_a_click_error():
    from shipit.verbs import _help

    with pytest.raises(click.ClickException) as excinfo:
        _help.load_help_text("shipit.verbs.missing", "help.txt")

    assert "bundled help resource shipit.verbs.missing:help.txt is unavailable" in str(
        excinfo.value
    )


def test_long_form_help_directory_resource_is_a_click_error():
    from shipit.verbs import _help

    with pytest.raises(click.ClickException) as excinfo:
        _help.load_help_text("shipit.verbs.lab", ".")

    assert "bundled help resource shipit.verbs.lab:. is unavailable" in str(
        excinfo.value
    )


def test_helpable_command_missing_positional_returns_none():
    from shipit.verbs.lab import run as run_verb

    ctx = click.Context(run_verb.cmd)
    assert run_verb.cmd._first_positional_arg(ctx, []) is None


def test_lab_run_cell_still_reaches_the_run_callback(monkeypatch):
    from shipit.verbs.lab import run as run_verb

    seen = {}

    def fake_run(cell_ref, *, checkouts, prs, force, fixture_path, cells_dir):
        seen.update(
            cell_ref=cell_ref,
            checkouts=checkouts,
            prs=prs,
            force=force,
            fixture_path=fixture_path,
            cells_dir=cells_dir,
        )
        return 0

    monkeypatch.setattr(run_verb, "run", fake_run)
    result = CliRunner().invoke(
        run_verb.cmd,
        [
            "ctl",
            "--checkout",
            "/repo",
            "--pr",
            "widget-1",
            "--force",
            "--fixture",
            "fixture.toml",
            "--cells-dir",
            "cells",
        ],
    )
    assert result.exit_code == 0
    assert seen == {
        "cell_ref": "ctl",
        "checkouts": ("/repo",),
        "prs": ("widget-1",),
        "force": True,
        "fixture_path": "fixture.toml",
        "cells_dir": "cells",
    }


def test_lab_report_cell_still_reaches_the_report_callback(monkeypatch):
    from shipit.verbs.lab import report as report_verb

    seen = {}

    def fake_run(cell_ref, *, fixture_path, cells_dir):
        seen.update(
            cell_ref=cell_ref,
            fixture_path=fixture_path,
            cells_dir=cells_dir,
        )
        return 0

    monkeypatch.setattr(report_verb, "run", fake_run)
    result = CliRunner().invoke(
        report_verb.cmd,
        ["treat", "--fixture", "fixture.toml", "--cells-dir", "cells"],
    )
    assert result.exit_code == 0
    assert seen == {
        "cell_ref": "treat",
        "fixture_path": "fixture.toml",
        "cells_dir": "cells",
    }


def test_run_cell_executes_the_plan_and_tags_every_record(
    checkout, launcher, tmp_path, capsys
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = _control_cell()
    state = tmp_path / "state"
    summary = run_cell(
        cell,
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=state,
        launcher=launcher["launch"],
    )
    assert len(summary.executed) == 2 and not summary.reused
    records = _store_records(state)
    assert len(records) == 2
    sweeps = sorted(r["round.cell"]["sweep"] for r in records)
    assert sweeps == [1, 2]
    for record in records:
        tag = record["round.cell"]
        assert tag["id"] == "ctl"
        assert tag["pr"] == "widget-1"
        assert tag["fixture_version"] == 1
        assert tag["replicate"] == 1
        assert tag["variant"].startswith("sha256:")
        assert record["round.pr"] is None
    out = capsys.readouterr().out
    assert "2 executed, 0 reused" in out


def test_run_cell_is_idempotent_by_key_and_force_reruns(
    checkout, launcher, tmp_path, capsys
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = _control_cell()
    fixture = _fixture_for(view)
    state = tmp_path / "state"
    kwargs = dict(checkouts=[str(checkout)], base_dir=state)
    run_cell(cell, fixture, launcher=launcher["launch"], **kwargs)
    first_launches = len(launcher["launches"])
    again = run_cell(cell, fixture, launcher=launcher["launch"], **kwargs)
    assert not again.executed and len(again.reused) == 2
    assert len(launcher["launches"]) == first_launches
    assert "banked — reused" in capsys.readouterr().out
    forced = run_cell(cell, fixture, launcher=launcher["launch"], force=True, **kwargs)
    assert len(forced.executed) == 2
    assert len(_store_records(state)) == 4


def test_extending_the_sweep_count_pays_only_for_the_new_points(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    fixture = _fixture_for(view)
    state = tmp_path / "state"
    kwargs = dict(
        checkouts=[str(checkout)], base_dir=state, launcher=launcher["launch"]
    )
    run_cell(_control_cell(sweeps={"count": 1}), fixture, **kwargs)
    summary = run_cell(_control_cell(sweeps={"count": 2}), fixture, **kwargs)
    assert len(summary.reused) == 1 and len(summary.executed) == 1
    assert [k["sweep"] for k in summary.executed] == [2]


def _fanout_cell(dims, cell_id="ctl", **overrides):
    data = {
        "schema": 1,
        "id": cell_id,
        "baseline": cell_id,
        "axis": "control",
        "fixture": {"version": 1, "prs": ["widget-1"]},
        "pipeline": {"shape": "fanout", "dimensions": list(dims)},
        "invocation": {"backend": "codex", "model": "pro", "timeout": "600s"},
        "sweeps": {"count": 1, "mode": "blind", "replicates": 1},
    }
    data.update(overrides)
    return parse_cell(data)


def test_changing_the_dimension_set_re_keys_banked_fanout_points(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    fixture = _fixture_for(view)
    state = tmp_path / "state"
    kwargs = dict(
        checkouts=[str(checkout)], base_dir=state, launcher=launcher["launch"]
    )
    first = run_cell(_fanout_cell(["correctness"]), fixture, **kwargs)
    assert len(first.executed) == 1
    swapped = run_cell(_fanout_cell(["sev-critical-high"]), fixture, **kwargs)
    assert len(swapped.executed) == 1 and not swapped.reused
    again = run_cell(_fanout_cell(["sev-critical-high"]), fixture, **kwargs)
    assert not again.executed and len(again.reused) == 1
    variants = {record["round.cell"]["variant"] for record in _store_records(state)}
    assert len(variants) == 2


def test_per_dimension_invocation_overrides_re_key_banked_fanout_points(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    fixture = _fixture_for(view)
    state = tmp_path / "state"
    kwargs = dict(
        checkouts=[str(checkout)], base_dir=state, launcher=launcher["launch"]
    )
    inv = {"backend": "codex", "model": "pro", "timeout": "600s"}
    plain = _fanout_cell(["correctness"], invocation=inv)
    overridden = _fanout_cell(
        ["correctness"],
        invocation={**inv, "dimensions": {"correctness": {"model": "o3"}}},
    )
    first = run_cell(plain, fixture, **kwargs)
    assert len(first.executed) == 1
    swapped = run_cell(overridden, fixture, **kwargs)
    assert len(swapped.executed) == 1 and not swapped.reused
    again = run_cell(overridden, fixture, **kwargs)
    assert not again.executed and len(again.reused) == 1
    variants = {record["round.cell"]["variant"] for record in _store_records(state)}
    assert len(variants) == 2


def test_informed_sweeps_compose_prior_findings_at_the_runner_layer(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = _control_cell(sweeps={"count": 2, "mode": "informed"})
    run_cell(
        cell,
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=tmp_path / "state",
        launcher=launcher["launch"],
    )
    sweep1_prompt = launcher["launches"][0]["cmd"][-1]
    sweep2_prompt = launcher["launches"][1]["cmd"][-1]
    assert "already banked by prior sweeps" not in sweep1_prompt
    assert "already banked by prior sweeps" in sweep2_prompt
    assert "row padding is missed when the staging buffer wraps" in sweep2_prompt
    assert "- f.txt:2 (major):" in sweep2_prompt


def test_blind_sweeps_never_compose_priors(checkout, launcher, tmp_path):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    run_cell(
        _control_cell(),
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=tmp_path / "state",
        launcher=launcher["launch"],
    )
    for launch in launcher["launches"]:
        assert "already banked by prior sweeps" not in launch["cmd"][-1]


def test_every_point_launches_the_up_front_bytes_not_a_re_read_of_the_original(
    checkout, tmp_path, monkeypatch
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.chdir(checkout)
    instr = checkout / "lab" / "instructions"
    instr.mkdir(parents=True)
    (instr / "base.txt").write_text("ORIGINAL-BYTES-MARKER\n", encoding="utf-8")

    launches: list = []

    def _mutating_launch(cmd, *, cwd, env, timeout=None):
        launches.append({"cmd": cmd})
        (instr / "base.txt").write_text("MUTATED-BYTES-MARKER\n", encoding="utf-8")
        return LaunchResult(returncode=0, stdout=_REVIEW, stderr="")

    run_cell(
        _control_cell(instructions={"path": "lab/instructions/base.txt"}),
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=tmp_path / "state",
        launcher=_mutating_launch,
    )
    assert len(launches) == 2
    for launch in launches:
        prompt = launch["cmd"][-1]
        assert "ORIGINAL-BYTES-MARKER" in prompt
        assert "MUTATED-BYTES-MARKER" not in prompt


def test_missing_checkout_is_a_loud_preflight_refusal(
    checkout, launcher, tmp_path, monkeypatch
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CellError, match="acme/widget"):
        run_cell(
            _control_cell(),
            _fixture_for(view),
            base_dir=tmp_path / "state",
            launcher=launcher["launch"],
        )
    assert not launcher["launches"]


def test_unfetched_pin_sha_refuses_before_any_launch(checkout, launcher, tmp_path):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    fixture = parse_fixture(
        {
            "schema": 1,
            "version": 1,
            "prs": [
                {
                    "id": "widget-1",
                    "repo": "acme/widget",
                    "pr": 7,
                    "base_sha": str(view.base_sha),
                    "head_sha": str(view.head_sha),
                },
                {
                    "id": "widget-2",
                    "repo": "acme/widget",
                    "pr": 8,
                    "base_sha": "0" * 40,
                    "head_sha": "1" * 40,
                },
            ],
        }
    )
    cell = _control_cell(fixture={"version": 1, "prs": ["widget-1", "widget-2"]})
    with pytest.raises(CellError, match="does not resolve"):
        run_cell(
            cell,
            fixture,
            checkouts=[str(checkout)],
            base_dir=tmp_path / "state",
            launcher=launcher["launch"],
        )
    assert not launcher["launches"]


def test_fixture_version_drift_refuses_to_run(checkout, launcher, tmp_path):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = _control_cell(fixture={"version": 2, "prs": ["widget-1"]})
    with pytest.raises(CellError, match="never compare"):
        run_cell(
            cell,
            _fixture_for(view),
            checkouts=[str(checkout)],
            base_dir=tmp_path / "state",
            launcher=launcher["launch"],
        )


def test_resolve_pins_validates_subset_membership(checkout):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    fixture = _fixture_for(view)
    cell = _control_cell()
    assert [p.id for p in resolve_pins(cell, fixture)] == ["widget-1"]
    with pytest.raises(CellError, match="does not have"):
        resolve_pins(_control_cell(fixture={"version": 1, "prs": ["ghost"]}), fixture)
    with pytest.raises(CellError, match="outside cell"):
        resolve_pins(cell, fixture, subset=["ghost"])


def test_plan_points_orders_sweeps_innermost():
    cell = _control_cell(sweeps={"count": 2, "replicates": 2})
    fixture = parse_fixture(
        {
            "version": 1,
            "prs": [
                {
                    "id": "widget-1",
                    "repo": "acme/widget",
                    "pr": 7,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                }
            ],
        }
    )
    points = plan_points(cell, resolve_pins(cell, fixture), variant_hash="sha256:x")
    assert [(p.replicate, p.sweep) for p in points] == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    ]


def test_plan_points_refuses_a_runaway_total_before_building_the_tuple():
    cell = _control_cell(sweeps={"count": 100, "replicates": 1000})
    fixture = parse_fixture(
        {
            "version": 1,
            "prs": [
                {
                    "id": "widget-1",
                    "repo": "acme/widget",
                    "pr": 7,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                }
            ],
        }
    )
    with pytest.raises(CellError, match="exceeds the max"):
        plan_points(cell, resolve_pins(cell, fixture), variant_hash="sha256:x")


def test_safe_instructions_path_refuses_a_symlink_escaping_the_repo(
    tmp_path, monkeypatch
):
    from shipit.review.labrun import safe_instructions_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "lab" / "instructions").mkdir(parents=True)
    secret = tmp_path.parent / "escaped-secret.txt"
    secret.write_text("s3cret", encoding="utf-8")
    (tmp_path / "lab" / "instructions" / "evil.txt").symlink_to(secret)
    with pytest.raises(CellError, match="outside the working directory"):
        safe_instructions_path("lab/instructions/evil.txt")
    (tmp_path / "lab" / "instructions" / "ok.txt").write_text("hi", encoding="utf-8")
    assert safe_instructions_path("lab/instructions/ok.txt").endswith("ok.txt")
    assert safe_instructions_path(None) is None


def test_fanout_cell_applies_per_dimension_invocation_overrides(
    checkout, launcher, tmp_path
):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = parse_cell(
        {
            "schema": 1,
            "id": "ctl",
            "baseline": "ctl",
            "axis": "control",
            "fixture": {"version": 1, "prs": ["widget-1"]},
            "pipeline": {
                "shape": "fanout",
                "dimensions": ["correctness", "test-quality"],
            },
            "invocation": {
                "backend": "codex",
                "model": "pro",
                "dimensions": {"test-quality": {"model": "o3", "timeout": "120s"}},
            },
            "sweeps": {"count": 1},
        }
    )
    state = tmp_path / "state"
    run_cell(
        cell,
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=state,
        launcher=launcher["launch"],
    )
    [record] = _store_records(state)
    models = {run["dimension"]: run["model"] for run in record["round.runs"]}
    assert models == {"correctness": "pro", "test-quality": "o3"}
    assert record["round.cell"]["id"] == "ctl"
    launch_timeouts = {launch["timeout"] for launch in launcher["launches"]}
    assert None not in launch_timeouts and len(launch_timeouts) == 2


def test_semantic_dedup_cell_collapses_the_reworded_duplicate_in_the_record(
    checkout, tmp_path, monkeypatch
):
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def _review(text):
        return json.dumps(
            {
                "summary": {"status": "COMMENT", "overall_feedback": "ok"},
                "comments": [
                    {
                        "file": "f.txt",
                        "line": 2,
                        "text": text,
                        "severity": "major",
                        "category": "correctness",
                        "confidence": 0.9,
                        "evidence": "e",
                        "fix": "",
                    }
                ],
            }
        )

    by_pass = {
        True: _review(
            "GPU readback failure zero-fills the comparison buffer, so the "
            "compare silently passes"
        ),
        False: _review(
            "when GPU readback fails the comparison buffer stays zero-filled "
            "and the compare silently reports a pass"
        ),
    }

    def _launch(cmd, *, cwd, env, timeout=None):
        return LaunchResult(
            returncode=0, stdout=by_pass["Correctness" in cmd[-1]], stderr=""
        )

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = parse_cell(
        {
            "schema": 1,
            "id": "semdedup",
            "baseline": "semdedup",
            "axis": "control",
            "fixture": {"version": 1, "prs": ["widget-1"]},
            "pipeline": {
                "shape": "fanout",
                "dimensions": ["correctness", "test-quality"],
                "dedup": "semantic",
            },
            "invocation": {"backend": "codex"},
            "sweeps": {"count": 1},
        }
    )
    state = tmp_path / "state"
    run_cell(
        cell,
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=state,
        launcher=_launch,
    )
    [record] = _store_records(state)
    findings = record["round.findings"]
    assert len(findings) == 2
    posted = [
        f for f in findings if f["disposition"] == "post" and f["duplicate_of"] is None
    ]
    assert len(posted) == 1
    [duplicate] = [f for f in findings if f["duplicate_of"] is not None]
    assert duplicate["duplicate_of"] == 0
    assert record["round.cell"]["id"] == "semdedup"


def test_fanout_rejects_overrides_outside_the_pass_set(checkout):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    with pytest.raises(ValueError, match="outside this round's pass set"):
        fanout.run_fanout_review(
            object(),
            view,
            dimensions=["correctness"],
            invocation_overrides={"test-quality": {"model": "o3"}},
        )


def test_fanout_rejects_overrides_in_an_incremental_round():
    with pytest.raises(ValueError, match="incremental"):
        fanout.run_fanout_review(
            object(),
            object(),
            incremental=True,
            invocation_overrides={"correctness": {"model": "o3"}},
        )


def _tagged_record(
    *,
    sweep,
    findings,
    base,
    head,
    tokens=None,
    duration_ms=60_000,
    replicate=1,
    variant="sha256:base",
):
    return {
        "round.repo": "acme/widget",
        "round.pr": None,
        "round.range": {"base": base, "head": head},
        "round.findings": [
            {
                "file": file,
                "line": line,
                "severity": "major",
                "text": text,
                "disposition": "post",
                "duplicate_of": None,
            }
            for file, line, text in findings
        ],
        "round.cell": {
            "id": "treat",
            "fixture_version": 1,
            "pr": "widget-1",
            "variant": variant,
            "replicate": replicate,
            "sweep": sweep,
        },
        "round.usage": {"duration_ms": duration_ms, "total_tokens": tokens},
    }


def _curve_fixture():
    return parse_fixture(
        {
            "version": 1,
            "prs": [
                {
                    "id": "widget-1",
                    "repo": "acme/widget",
                    "pr": 7,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                }
            ],
            "labels": [
                {
                    "id": "widget-G1",
                    "pr": "widget-1",
                    "file": "f.txt",
                    "lines": [1, 3],
                    "severity": "major",
                    "verdict": "real",
                    "confirmed": True,
                    "claim": "staging buffer row padding missed",
                    "provenance": {"kind": "fix-commit", "ref": "abc1234"},
                },
                {
                    "id": "widget-N1",
                    "pr": "widget-1",
                    "file": "g.txt",
                    "lines": [5, 5],
                    "severity": "major",
                    "verdict": "not-real",
                    "confirmed": True,
                    "claim": "unused import of Backend",
                    "provenance": {"kind": "adjudication", "ref": "issue-1"},
                },
            ],
        }
    )


def _treatment_cell(sweeps=3):
    return parse_cell(
        {
            "schema": 1,
            "id": "treat",
            "baseline": "ctl",
            "axis": "sweep mode: informed vs blind",
            "fixture": {"version": 1, "prs": ["widget-1"]},
            "pipeline": {"shape": "single"},
            "sweeps": {"count": sweeps, "mode": "informed"},
        }
    )


def test_convergence_curve_reports_cumulative_points_and_missing_sweeps():
    fixture = _curve_fixture()
    records = [
        _tagged_record(
            sweep=1,
            findings=[("h.txt", 1, "unrelated observation entirely")],
            base="a" * 40,
            head="b" * 40,
            tokens=1_000_000,
        ),
        _tagged_record(
            sweep=2,
            findings=[
                ("f.txt", 2, "the staging buffer misses row padding here"),
                ("g.txt", 5, "unused import of Backend"),
            ],
            base="a" * 40,
            head="b" * 40,
        ),
    ]
    curve = convergence_curve(
        _treatment_cell(), fixture, records, variant_hash="sha256:base"
    )
    assert [p.sweep for p in curve.points] == [1, 2, 3]
    p1, p2, p3 = curve.points
    assert (p1.recalled, p1.positives) == (0, 1)
    assert p1.tokens == 1_000_000 and p1.tokens_complete
    assert p1.unadjudicated == 1
    assert (p2.recalled, p2.positives) == (1, 1)
    assert p2.false_positives == 1 and p2.precision == 0.5
    assert p2.tokens == 1_000_000 and not p2.tokens_complete
    assert p2.duration_ms == 120_000
    assert not p2.missing
    assert p3.missing and (p3.recalled, p3.positives) == (1, 1)
    assert p1.underpowered


def test_convergence_curve_latency_only_and_last_record_wins():
    fixture = _curve_fixture()
    stale = _tagged_record(
        sweep=1,
        findings=[("h.txt", 1, "unrelated observation entirely")],
        base="a" * 40,
        head="b" * 40,
    )
    rerun = _tagged_record(
        sweep=1,
        findings=[("f.txt", 2, "the staging buffer misses row padding here")],
        base="a" * 40,
        head="b" * 40,
    )
    curve = convergence_curve(
        _treatment_cell(sweeps=1), fixture, [stale, rerun], variant_hash="sha256:base"
    )
    [point] = curve.points
    assert point.records == 1 and point.recalled == 1
    assert point.tokens is None and not point.tokens_complete


def test_convergence_curve_ignores_other_cells_and_other_fixture_versions():
    fixture = _curve_fixture()
    foreign = _tagged_record(
        sweep=1,
        findings=[("f.txt", 2, "the staging buffer misses row padding here")],
        base="a" * 40,
        head="b" * 40,
    )
    foreign["round.cell"]["id"] = "someone-else"
    drifted = _tagged_record(
        sweep=1,
        findings=[("f.txt", 2, "the staging buffer misses row padding here")],
        base="a" * 40,
        head="b" * 40,
    )
    drifted["round.cell"]["fixture_version"] = 9
    curve = convergence_curve(
        _treatment_cell(sweeps=1),
        fixture,
        [foreign, drifted],
        variant_hash="sha256:base",
    )
    [point] = curve.points
    assert point.records == 0 and point.missing


def test_convergence_curve_ignores_a_pin_outside_the_cells_subset():
    fixture = parse_fixture(
        {
            "version": 1,
            "prs": [
                {
                    "id": "widget-1",
                    "repo": "acme/widget",
                    "pr": 7,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                {
                    "id": "widget-2",
                    "repo": "acme/widget",
                    "pr": 8,
                    "base_sha": "c" * 40,
                    "head_sha": "d" * 40,
                },
            ],
            "labels": [
                {
                    "id": "widget-G1",
                    "pr": "widget-1",
                    "file": "f.txt",
                    "lines": [1, 3],
                    "severity": "major",
                    "verdict": "real",
                    "confirmed": True,
                    "claim": "staging buffer row padding missed",
                    "provenance": {"kind": "fix-commit", "ref": "abc1234"},
                }
            ],
        }
    )
    stray = _tagged_record(
        sweep=1,
        findings=[("f.txt", 2, "the staging buffer misses row padding here")],
        base="a" * 40,
        head="b" * 40,
    )
    stray["round.cell"]["pr"] = "widget-2"
    curve = convergence_curve(
        _treatment_cell(sweeps=1), fixture, [stray], variant_hash="sha256:base"
    )
    [point] = curve.points
    assert point.records == 0 and point.missing


def test_convergence_curve_survives_a_corrupt_banked_record():
    fixture = _curve_fixture()
    good = _tagged_record(
        sweep=1,
        findings=[("f.txt", 2, "the staging buffer misses row padding here")],
        base="a" * 40,
        head="b" * 40,
    )
    corrupt = _tagged_record(
        sweep=1, findings=[("x.txt", 1, "noise")], base="a" * 40, head="b" * 40
    )
    corrupt["round.cell"]["id"] = []
    curve = convergence_curve(
        _treatment_cell(sweeps=1),
        fixture,
        [corrupt, good],
        variant_hash="sha256:base",
    )
    [point] = curve.points
    assert point.records == 1 and point.recalled == 1


def test_render_curve_report_carries_the_honesty_markers():
    fixture = _curve_fixture()
    records = [
        _tagged_record(
            sweep=1,
            findings=[("f.txt", 2, "the staging buffer misses row padding here")],
            base="a" * 40,
            head="b" * 40,
        ),
    ]
    cell = _treatment_cell(sweeps=2)
    curve = convergence_curve(cell, fixture, records, variant_hash="sha256:base")
    baseline_cell = parse_cell(
        {
            "schema": 1,
            "id": "ctl",
            "baseline": "ctl",
            "axis": "control",
            "fixture": {"version": 1, "prs": ["widget-1"]},
            "pipeline": {"shape": "single"},
            "sweeps": {"count": 2},
        }
    )
    baseline_curve = convergence_curve(
        baseline_cell, fixture, [], variant_hash="sha256:base"
    )
    text = render_curve_report(curve, baseline_curve)
    assert "convergence curve — cell treat" in text
    assert "EQUAL BUDGET" in text
    assert "[UNDERPOWERED]" in text
    assert "n/a (latency-only)" in text
    assert "[missing] sweep 2" in text
    assert "baseline ctl (control):" in text


def test_render_curve_report_labels_a_treatment_baseline_by_its_axis():
    fixture = _curve_fixture()
    curve = convergence_curve(
        _treatment_cell(sweeps=2), fixture, [], variant_hash="sha256:base"
    )
    treatment_baseline = parse_cell(
        {
            "schema": 1,
            "id": "mid",
            "baseline": "ctl",
            "axis": "dimensions",
            "fixture": {"version": 1, "prs": ["widget-1"]},
            "pipeline": {"shape": "single"},
            "sweeps": {"count": 2},
        }
    )
    baseline_curve = convergence_curve(
        treatment_baseline, fixture, [], variant_hash="sha256:base"
    )
    text = render_curve_report(curve, baseline_curve)
    assert "baseline mid (axis: dimensions):" in text
    assert "(control)" not in text


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cell_toml(cell_id, *, baseline, axis, mode):
    return f"""
schema = 1
id = "{cell_id}"
baseline = "{baseline}"
axis = "{axis}"
[fixture]
version = 1
prs = ["widget-1"]
[pipeline]
shape = "single"
[invocation]
backend = "codex"
[sweeps]
count = 2
mode = "{mode}"
"""


def _fixture_toml(view):
    return f"""
schema = 1
version = 1
[[prs]]
id = "widget-1"
repo = "acme/widget"
pr = 7
base_sha = "{view.base_sha}"
head_sha = "{view.head_sha}"
[[labels]]
id = "widget-G1"
pr = "widget-1"
file = "f.txt"
lines = [1, 3]
severity = "major"
verdict = "real"
confirmed = true
claim = "staging buffer row padding missed"
[labels.provenance]
kind = "fix-commit"
ref = "abc1234"
"""


def test_lab_demo_pair_end_to_end_produces_a_scored_curve(
    checkout, launcher, tmp_path, capsys
):
    from shipit.verbs.lab import report as report_verb
    from shipit.verbs.lab import run as run_verb

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cells = tmp_path / "cells"
    _write(
        cells / "ctl.toml",
        _cell_toml("ctl", baseline="ctl", axis="control", mode="blind"),
    )
    _write(
        cells / "treat.toml",
        _cell_toml(
            "treat",
            baseline="ctl",
            axis="sweep mode: informed vs blind",
            mode="informed",
        ),
    )
    fixture_path = tmp_path / "fixture.toml"
    _write(fixture_path, _fixture_toml(view))
    state = tmp_path / "state"
    for ref in ("ctl", "treat"):
        rc = run_verb.run(
            ref,
            checkouts=(str(checkout),),
            fixture_path=str(fixture_path),
            cells_dir=str(cells),
            base_dir=state,
            launcher=launcher["launch"],
        )
        assert rc == 0
    capsys.readouterr()
    rc = report_verb.run(
        "treat",
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=state,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "convergence curve — cell treat" in out
    assert "baseline ctl (control):" in out
    assert "sweep 1: recall 1/1 (100%)" in out
    assert "sweep 2: recall 1/1 (100%)" in out
    assert "n/a (latency-only)" in out


def test_lab_run_refuses_an_unfair_pair_as_one_clean_error_line(
    checkout, tmp_path, capsys
):
    from shipit.verbs.lab import run as run_verb

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cells = tmp_path / "cells"
    _write(
        cells / "ctl.toml",
        _cell_toml("ctl", baseline="ctl", axis="control", mode="blind"),
    )
    unfair = _cell_toml(
        "treat", baseline="ctl", axis="pr subset", mode="blind"
    ).replace('prs = ["widget-1"]', 'prs = ["widget-2"]')
    _write(cells / "treat.toml", unfair)
    fixture_path = tmp_path / "fixture.toml"
    _write(fixture_path, _fixture_toml(view))
    rc = run_verb.run(
        "treat",
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=tmp_path / "state",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "different PR subsets" in err


def test_lab_run_missing_baseline_file_is_one_clean_error_line(tmp_path, capsys):
    from shipit.verbs.lab import run as run_verb

    cells = tmp_path / "cells"
    _write(
        cells / "treat.toml",
        _cell_toml("treat", baseline="ctl", axis="x", mode="blind"),
    )
    fixture_path = tmp_path / "fixture.toml"
    _write(fixture_path, "schema = 1\nversion = 1\n")
    rc = run_verb.run(
        "treat",
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=tmp_path / "state",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "does not exist" in err
    assert "'ctl'" in err and str(cells) in err


def test_lab_run_refuses_a_cyclic_baseline_chain(tmp_path, capsys):
    from shipit.verbs.lab import run as run_verb

    cells = tmp_path / "cells"
    _write(cells / "a.toml", _cell_toml("a", baseline="b", axis="x", mode="blind"))
    _write(cells / "b.toml", _cell_toml("b", baseline="a", axis="y", mode="blind"))
    fixture_path = tmp_path / "fixture.toml"
    _write(fixture_path, "schema = 1\nversion = 1\n")
    rc = run_verb.run(
        "a",
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=tmp_path / "state",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "cyclic baseline chain" in err
    assert "'a' -> 'b' -> 'a'" in err


def test_lab_report_missing_baseline_file_is_one_clean_error_line(tmp_path, capsys):
    from shipit.verbs.lab import report as report_verb

    cells = tmp_path / "cells"
    _write(
        cells / "treat.toml",
        _cell_toml("treat", baseline="ghost", axis="x", mode="blind"),
    )
    fixture_path = tmp_path / "fixture.toml"
    _write(fixture_path, "schema = 1\nversion = 1\n")
    rc = report_verb.run(
        "treat",
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=tmp_path / "state",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "does not exist" in err
    assert "'ghost'" in err and str(cells) in err


def test_lab_report_unknown_cell_is_one_clean_error_line(tmp_path, capsys):
    from shipit.verbs.lab import report as report_verb

    rc = report_verb.run("ghost", cells_dir=str(tmp_path / "cells"))
    assert rc == 1
    assert capsys.readouterr().err.startswith("error: no cell file")


def test_lab_report_selects_fanout_records_by_the_folded_key(
    checkout, launcher, tmp_path, capsys
):
    from shipit.verbs.lab import report as report_verb
    from shipit.verbs.lab import run as run_verb

    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cells = tmp_path / "cells"
    _write(
        cells / "ctl.toml",
        """
schema = 1
id = "ctl"
baseline = "ctl"
axis = "control"
[fixture]
version = 1
prs = ["widget-1"]
[pipeline]
shape = "fanout"
dimensions = ["correctness"]
[invocation]
backend = "codex"
[sweeps]
count = 1
""",
    )
    fixture_path = tmp_path / "fixture.toml"
    _write(fixture_path, _fixture_toml(view))
    state = tmp_path / "state"
    rc = run_verb.run(
        "ctl",
        checkouts=(str(checkout),),
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=state,
        launcher=launcher["launch"],
    )
    assert rc == 0
    capsys.readouterr()
    rc = report_verb.run(
        "ctl",
        fixture_path=str(fixture_path),
        cells_dir=str(cells),
        base_dir=state,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "sweep 1: recall 1/1 (100%)" in out
    assert "missing" not in out


def test_the_committed_cells_load_and_pair_fairly():
    import dataclasses
    from pathlib import Path

    from shipit.review.cell import load_baseline_lineage, load_cell
    from shipit.review.groundtruth import load_fixture

    cells_dir = Path("lab/cells")
    cells = [load_cell(path) for path in sorted(cells_dir.glob("*.toml"))]
    controls = [c for c in cells if c.is_control]
    treatments = [c for c in cells if not c.is_control]
    assert len(controls) == 1, "exactly one committed control cell expected"
    control = controls[0]
    fixture = load_fixture(Path("lab/fixture.toml"))
    assert fixture.version == control.fixture_version
    assert control.is_control
    by_id = {cell.id: cell for cell in (control, *treatments)}
    for treatment in treatments:
        chain = load_baseline_lineage(treatment, fixture, cells_dir)
        assert chain[-1].id == control.id and chain[-1].is_control
        assert treatment.axis != "control"
    assert by_id["fanout-informed"].baseline == "fanout-baseline"
    assert by_id["fanout-semdedup"].baseline == "fanout-baseline"
    assert by_id["fanout-sevtiers"].baseline == "fanout-baseline"
    assert by_id["sevtiers-informed"].baseline == "fanout-sevtiers"
    assert by_id["singlepass"].baseline == "fanout-baseline"
    assert [
        c.id
        for c in load_baseline_lineage(by_id["sevtiers-informed"], fixture, cells_dir)
    ] == ["sevtiers-informed", "fanout-sevtiers", "fanout-baseline"]
    identity_fields = {"id", "baseline", "axis", "description"}
    allowed_deltas = {
        "fanout-informed": {"sweep_mode"},
        "fanout-semdedup": {"dedup"},
        "fanout-sevtiers": {"dimensions"},
        "sevtiers-informed": {"sweep_mode"},
        "singlepass": {"shape"},
    }
    for treatment in treatments:
        base = by_id[treatment.baseline]
        deltas = {
            f.name
            for f in dataclasses.fields(treatment)
            if getattr(treatment, f.name) != getattr(base, f.name)
        } - identity_fields
        assert deltas == allowed_deltas[treatment.id], (
            f"cell {treatment.id!r} differs from baseline {base.id!r} in "
            f"{sorted(deltas)}; only {sorted(allowed_deltas[treatment.id])} "
            "may differ (the declared axis)"
        )
    for cell in (control, *treatments):
        assert cell.sweeps == 2
        assert cell.replicates == 2
        assert [p.id for p in resolve_pins(cell, fixture)] == [
            "core-440",
            "app-391",
            "lex-820",
        ]
    sev_tiers = ("sev-critical-high", "sev-medium", "sev-low")
    assert by_id["fanout-sevtiers"].dimensions == sev_tiers
    assert by_id["sevtiers-informed"].dimensions == sev_tiers
    assert by_id["fanout-baseline"].dimensions == ()
    assert by_id["fanout-informed"].dimensions == ()
    assert by_id["singlepass"].dimensions == ()
    assert by_id["fanout-baseline"].sweep_mode == "blind"
    assert by_id["fanout-sevtiers"].sweep_mode == "blind"
    assert by_id["fanout-informed"].sweep_mode == "informed"
    assert by_id["sevtiers-informed"].sweep_mode == "informed"
    assert by_id["singlepass"].sweep_mode == "blind"
    assert by_id["singlepass"].shape == "single"
    assert by_id["fanout-baseline"].shape == "fanout"
    assert by_id["fanout-informed"].shape == "fanout"
    assert by_id["fanout-sevtiers"].shape == "fanout"
    assert by_id["sevtiers-informed"].shape == "fanout"
