"""The Review Lab's cell runner + convergence-curve report (ADR-0049, RVW03-WS07).

`lab run` resolves a declarative Cell onto the sanctioned offline replay
driver — foreground, idempotent by the full key (cell, fixture PR, fixture
version, variant, replicate, sweep), banked records reused, never re-paid —
and tags every resulting review-round record with `round.cell`. `lab report`
pools those records through the ONE deterministic scorer into a convergence
curve, compared against the baseline cell at equal budget. These tests pin the
runner's contract (idempotency + --force, informed-sweep composition at the
runner layer, checkout preflight, per-dimension Invocation overrides through
the driver), the pure curve mechanics (cumulative points, missing sweeps,
last-record-wins, latency-only cost), the CLI verbs' user-facing errors, and
the acceptance end-to-end: a control + one-axis treatment pair runs over a
real checkout (model launch faked) and produces a scored curve.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from shipit.review import fanout, producer, replay
from shipit.review.cell import CellError, parse_cell
from shipit.review.curve import convergence_curve, render_curve_report
from shipit.review.groundtruth import parse_fixture
from shipit.review.labrun import plan_points, resolve_pins, run_cell
from shipit.spawn.launch import LaunchResult

# A single-pass review whose finding lexically matches the fixture label below
# (same file, line in range, claim-token overlap above the threshold).
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
    """A real two-commit checkout with an origin remote (the record's repo key)."""
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
    """A launch fake capturing every run's prompt; answers a valid review."""
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    captured: dict = {"launches": []}

    def _launch(cmd, *, cwd, env, timeout=None):
        captured["launches"].append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return LaunchResult(returncode=0, stdout=_REVIEW, stderr="")

    captured["launch"] = _launch
    return captured


def _fixture_for(view):
    """A one-pin fixture over the test checkout's real range, with one
    confirmed real major label the fake review's finding matches (the runner
    tests exercise execution/idempotency, not FP scoring — see `_curve_fixture`
    for the real + not-real pair the curve tests score)."""
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


# --- the runner: execution, tagging, idempotency ----------------------------------


def test_run_cell_executes_the_plan_and_tags_every_record(
    checkout, launcher, tmp_path, capsys
):
    """The sweep plan runs foreground over the replay driver; each record is a
    normal review-round record (round.pr None) tagged with the cell's FULL
    idempotency key on round.cell."""
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
    """ADR-0049's banked-reuse: the second run reuses every point (extending a
    curve pays only for new points); --force is the explicit re-execute."""
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = _control_cell()
    fixture = _fixture_for(view)
    state = tmp_path / "state"
    kwargs = dict(checkouts=[str(checkout)], base_dir=state)
    run_cell(cell, fixture, launcher=launcher["launch"], **kwargs)
    first_launches = len(launcher["launches"])
    again = run_cell(cell, fixture, launcher=launcher["launch"], **kwargs)
    assert not again.executed and len(again.reused) == 2
    assert len(launcher["launches"]) == first_launches  # nothing re-billed
    assert "banked — reused" in capsys.readouterr().out
    forced = run_cell(cell, fixture, launcher=launcher["launch"], force=True, **kwargs)
    assert len(forced.executed) == 2
    assert len(_store_records(state)) == 4  # re-runs append; the report keeps last


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


def test_informed_sweeps_compose_prior_findings_at_the_runner_layer(
    checkout, launcher, tmp_path
):
    """Sweep 2 of an informed cell is primed with sweep 1's POSTED findings in
    its instructions text — runner-layer composition, no driver change: the
    launched prompt itself carries the priors."""
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
        _control_cell(),  # blind K=2
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
    """The idempotency key hashes the instructions read ONCE up front, so every
    point must launch those exact bytes. The replay driver re-reads its
    instructions path at launch; handing it the original file would let an edit
    landing mid-run bill the model bytes the record is not keyed under. run_cell
    materializes the hashed bytes per point, so a rewrite of the original never
    reaches a launch — even a later blind sweep."""
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    monkeypatch.setattr(producer.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.chdir(checkout)
    instr = checkout / "lab" / "instructions"
    instr.mkdir(parents=True)
    (instr / "base.txt").write_text("ORIGINAL-BYTES-MARKER\n", encoding="utf-8")

    # A launcher that rewrites the original instructions file on its first call —
    # an edit/swap landing mid-run between the up-front read and a later launch.
    launches: list = []

    def _mutating_launch(cmd, *, cwd, env, timeout=None):
        launches.append({"cmd": cmd})
        (instr / "base.txt").write_text("MUTATED-BYTES-MARKER\n", encoding="utf-8")
        return LaunchResult(returncode=0, stdout=_REVIEW, stderr="")

    run_cell(
        _control_cell(instructions={"path": "lab/instructions/base.txt"}),  # blind K=2
        _fixture_for(view),
        checkouts=[str(checkout)],
        base_dir=tmp_path / "state",
        launcher=_mutating_launch,
    )
    assert len(launches) == 2  # both points ran
    for launch in launches:
        prompt = launch["cmd"][-1]
        assert "ORIGINAL-BYTES-MARKER" in prompt
        assert "MUTATED-BYTES-MARKER" not in prompt


def test_missing_checkout_is_a_loud_preflight_refusal(
    checkout, launcher, tmp_path, monkeypatch
):
    """A pin with no matching clone refuses BEFORE any model run bills —
    never a silent skip that would shrink the curve's denominator."""
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    monkeypatch.chdir(tmp_path)  # cwd is not a clone of anything
    with pytest.raises(CellError, match="acme/widget"):
        run_cell(
            _control_cell(),
            _fixture_for(view),
            base_dir=tmp_path / "state",
            launcher=launcher["launch"],
        )
    assert not launcher["launches"]


def test_unfetched_pin_sha_refuses_before_any_launch(checkout, launcher, tmp_path):
    """The all-or-nothing preflight resolves EVERY pin's range up front: a
    second pin naming an unfetched SHA refuses before the FIRST pin's point ever
    launches — locking the documented contract that a multi-pin run never bills
    pin 1 and then dies on pin 2's missing commit, leaving a half-run curve."""
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
                    "id": "widget-2",  # same repo/checkout, but SHAs never fetched
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
    assert not launcher["launches"]  # all-or-nothing: nothing billed


def test_fixture_version_drift_refuses_to_run(checkout, launcher, tmp_path):
    view = replay.resolve_range("HEAD~1..HEAD", workdir=str(checkout))
    cell = _control_cell(fixture={"version": 2, "prs": ["widget-1"]})
    with pytest.raises(CellError, match="never compare"):
        run_cell(
            cell,
            _fixture_for(view),  # v1
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
    """Informed sweeps need their priors banked first: per pin, per replicate,
    sweeps run 1..K before the next replicate starts."""
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
    """The per-axis cap still lets the product blow up (1000 × 1000 per pin);
    a total past MAX_PLANNED_POINTS is a config typo, not an experiment, and
    must refuse loudly before enumerating a million points or billing them."""
    # Each axis is individually valid (≤ MAX_SWEEP_COUNT), but 1000 × 100 = 100k
    # points is a runaway total for one pin.
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
    """The parse guard blocks absolute / `~` / `..`, but an IN-repo symlink
    pointing out still resolves to a secret; the read boundary re-checks that the
    resolved real path stays within the repo root, so a symlink escape is a loud
    refusal (the in-repo-files-only promise holds for symlinks too)."""
    from shipit.review.labrun import safe_instructions_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "lab" / "instructions").mkdir(parents=True)
    secret = tmp_path.parent / "escaped-secret.txt"
    secret.write_text("s3cret", encoding="utf-8")
    (tmp_path / "lab" / "instructions" / "evil.txt").symlink_to(secret)
    with pytest.raises(CellError, match="outside the working directory"):
        safe_instructions_path("lab/instructions/evil.txt")
    # An in-repo real file resolves fine; the bundled default (None) passes through.
    (tmp_path / "lab" / "instructions" / "ok.txt").write_text("hi", encoding="utf-8")
    assert safe_instructions_path("lab/instructions/ok.txt").endswith("ok.txt")
    assert safe_instructions_path(None) is None


# --- per-dimension Invocation overrides through the driver --------------------------


def test_fanout_cell_applies_per_dimension_invocation_overrides(
    checkout, launcher, tmp_path
):
    """The experiment-only capability (ADR-0049): the overridden pass launches
    AND records with its own model; every other pass keeps the cell's — read
    off round.runs, so the arm is never mislabeled."""
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
    # The override must reach the DRIVER, not merely the record: the two passes
    # launch with DISTINCT process deadlines (correctness's 600s vs the
    # overridden test-quality's 120s), so the timeout override — which round.runs
    # does NOT stamp — is verified end-to-end at the launch seam, not assumed.
    launch_timeouts = {launch["timeout"] for launch in launcher["launches"]}
    assert None not in launch_timeouts and len(launch_timeouts) == 2


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


# --- the convergence curve (pure) ----------------------------------------------------


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
        # Sweep 1: misses everything relevant, burns 1M tokens.
        _tagged_record(
            sweep=1,
            findings=[("h.txt", 1, "unrelated observation entirely")],
            base="a" * 40,
            head="b" * 40,
            tokens=1_000_000,
        ),
        # Sweep 2: recalls the real label AND hits the banked not-real (an FP);
        # no token count (the WS04 capture not present) — cost goes partial.
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
    assert p1.unadjudicated == 1  # the unmatched emission awaits adjudication
    assert (p2.recalled, p2.positives) == (1, 1)
    assert p2.false_positives == 1 and p2.precision == 0.5
    assert p2.tokens == 1_000_000 and not p2.tokens_complete  # a floor, not truth
    assert p2.duration_ms == 120_000
    assert not p2.missing
    # Sweep 3 is declared but not banked: the point renders as the gap it is,
    # carrying the prior sweeps' cumulative numbers.
    assert p3.missing and (p3.recalled, p3.positives) == (1, 1)
    assert p1.underpowered  # 1 major-or-worse positive < the floor


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
    # A --force re-run supersedes its predecessor: both never score together.
    assert point.records == 1 and point.recalled == 1
    # No record carries a token count: the point is honestly latency-only.
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
    """A record whose cell id + fixture version match but whose PR is a
    DIFFERENT fixture pin (one the cell does not declare, sharing the same repo
    store) must NOT pool into the curve. The report filters by the FULL expected
    key — pin included — so a stray pin never inflates recall or the denominator."""
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
                    "id": "widget-2",  # same repo, NOT in the cell's declared subset
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
    # A well-scoring record, but tagged for widget-2 — the cell declares widget-1.
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
    assert point.records == 0 and point.missing  # the stray pin never pooled


def test_convergence_curve_survives_a_corrupt_banked_record():
    """A malformed stored record whose `round.cell` key holds a non-scalar (a
    hand-edited or corrupt store line) is SKIPPED, not fed as an unhashable
    element into the O(1) key-tuple set — no `TypeError: unhashable type`."""
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
    corrupt["round.cell"]["id"] = []  # unhashable — a corrupt key field
    curve = convergence_curve(
        _treatment_cell(sweeps=1),
        fixture,
        [corrupt, good],
        variant_hash="sha256:base",
    )
    [point] = curve.points
    assert point.records == 1 and point.recalled == 1  # only the good record pooled


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
    assert "[UNDERPOWERED]" in text  # 1 positive < the ADR-0048 floor
    assert "n/a (latency-only)" in text  # no token counts banked yet
    assert "[missing] sweep 2" in text  # declared, not banked
    assert "baseline ctl (control):" in text


# --- the CLI verbs (thin boundary + acceptance end-to-end) ---------------------------


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
    """The acceptance walk: a control + one-axis treatment cell pair runs
    end-to-end against a fixture (model launch faked) and `lab report` renders
    a scored convergence curve with the equal-budget baseline beside it."""
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
    assert "n/a (latency-only)" in out  # CLI backends bank no token totals yet


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
    # A genuinely-unfair pair: the treatment scores a DIFFERENT pin subset than
    # the control (not just a different spelling of the same one — an empty
    # `prs` would resolve to the same single fixture pin and be fair).
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


def test_lab_report_unknown_cell_is_one_clean_error_line(tmp_path, capsys):
    from shipit.verbs.lab import report as report_verb

    rc = report_verb.run("ghost", cells_dir=str(tmp_path / "cells"))
    assert rc == 1
    assert capsys.readouterr().err.startswith("error: no cell file")


# --- the committed demo pair ----------------------------------------------------------


def test_the_committed_demo_cell_pair_loads_and_is_fair():
    """The in-repo example pair (lab/cells/) must load, pass the fair-pair
    check, and resolve every pin against the committed fixture v1."""
    from pathlib import Path

    from shipit.review.cell import check_fair_pair, load_cell
    from shipit.review.groundtruth import load_fixture

    control = load_cell(Path("lab/cells/fanout-baseline.toml"))
    treatment = load_cell(Path("lab/cells/fanout-informed.toml"))
    fixture = load_fixture(Path("lab/fixture.toml"))
    check_fair_pair(treatment, control, fixture)
    assert fixture.version == control.fixture_version
    assert [p.id for p in resolve_pins(treatment, fixture)] == [
        "core-440",
        "app-391",
        "lex-820",
    ]
    assert treatment.axis != "control" and control.is_control
