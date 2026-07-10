"""The declarative experiment Cell (ADR-0049, RVW03-WS07): parse + validation.

A Cell file states one review experiment; these tests pin the fairness
contract at LOAD — mandatory baseline/axis (control names itself and declares
`axis = "control"`; a treatment names its control and one real axis), loud
unknown keys (a silently-ignored knob would run a different experiment than
the reviewed file declares), the rejected-not-wireable `reasoning` field, the
per-dimension override rules, the sweep vocabulary — plus the pure lab
mechanics: the full idempotency key, the informed-sweep instruction
composition, and the machine-checkable fair-pair rule.
"""

from __future__ import annotations

import pytest

from shipit.review.cell import (
    CellError,
    check_fair_pair,
    compose_informed_instructions,
    key_tuple,
    load_cell,
    parse_cell,
    record_matches_key,
    resolve_cell_path,
    run_key,
)
from shipit.review.groundtruth import parse_fixture


def _fair_fixture():
    """A fixture pinning the pins the fair-pair tests name (`core-440`,
    `app-391`) — `check_fair_pair` compares EFFECTIVE pin sets against it, so
    `prs = []` (every pin) and an explicit full list read as one denominator."""
    return parse_fixture(
        {
            "version": 1,
            "prs": [
                {
                    "id": "core-440",
                    "repo": "acme/core",
                    "pr": 1,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                {
                    "id": "app-391",
                    "repo": "acme/app",
                    "pr": 2,
                    "base_sha": "c" * 40,
                    "head_sha": "d" * 40,
                },
            ],
        }
    )


def _cell_data(**overrides):
    """A minimal VALID control-cell dict; tests mutate from here."""
    data = {
        "schema": 1,
        "id": "control",
        "baseline": "control",
        "axis": "control",
        "fixture": {"version": 1, "prs": ["core-440"]},
        "pipeline": {"shape": "fanout"},
        "invocation": {"backend": "codex", "model": "pro", "timeout": "600s"},
        "sweeps": {"count": 2, "mode": "blind", "replicates": 1},
    }
    data.update(overrides)
    return data


def _treatment_data(**overrides):
    data = _cell_data(id="treatment", axis="sweep mode: informed vs blind")
    data["baseline"] = "control"
    data.update(overrides)
    return data


# --- the mandatory fairness declaration ------------------------------------------


def test_parse_cell_accepts_a_valid_control_and_treatment():
    control = parse_cell(_cell_data())
    assert control.is_control
    assert control.sweeps == 2 and control.replicates == 1
    treatment = parse_cell(_treatment_data())
    assert not treatment.is_control
    assert treatment.baseline == "control"


@pytest.mark.parametrize("missing", ["baseline", "axis", "id"])
def test_baseline_axis_and_id_are_mandatory(missing):
    data = _cell_data()
    del data[missing]
    with pytest.raises(CellError, match=missing):
        parse_cell(data)


def test_control_must_declare_axis_control():
    with pytest.raises(CellError, match="CONTROL"):
        parse_cell(_cell_data(axis="something"))


def test_treatment_must_declare_a_real_axis():
    # A treatment hiding behind axis = "control" is an undeclared comparison —
    # the exact unfairness ADR-0049 makes fail at cell review.
    with pytest.raises(CellError, match="ONE changed axis"):
        parse_cell(_treatment_data(axis="control"))


def test_fixture_version_pin_is_mandatory():
    data = _cell_data()
    del data["fixture"]
    with pytest.raises(CellError, match=r"\[fixture\]"):
        parse_cell(data)
    with pytest.raises(CellError, match="positive integer"):
        parse_cell(_cell_data(fixture={"version": 0}))


def test_pipeline_shape_is_explicit_and_vocabulary_checked():
    data = _cell_data()
    del data["pipeline"]
    with pytest.raises(CellError, match=r"\[pipeline\]"):
        parse_cell(data)
    with pytest.raises(CellError, match="shape"):
        parse_cell(_cell_data(pipeline={"shape": "quadratic"}))


# --- no silently-ignored knobs ----------------------------------------------------


def test_unknown_top_level_key_is_loud():
    with pytest.raises(CellError, match="unknown key"):
        parse_cell(_cell_data(sweepz={"count": 3}))


def test_unknown_pipeline_key_is_loud():
    with pytest.raises(CellError, match="unknown key"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "nit_capp": 3}))


def test_invocation_reasoning_is_rejected_as_not_wireable():
    # The backends carry a reasoning knob (#685/#691), but the lab runner does
    # not thread a level into the replay driver yet: accepting the field would
    # stamp arms with a level that never reached a run — the RVW02 failure.
    data = _cell_data()
    data["invocation"]["reasoning"] = "high"
    with pytest.raises(CellError, match="reasoning.*not wireable"):
        parse_cell(data)


# --- dimensions + per-dimension Invocation overrides ------------------------------


def test_unknown_dimension_name_is_loud():
    with pytest.raises(CellError, match="unknown dimension 'vibes'"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "dimensions": ["vibes"]}))


def test_dimensions_apply_only_to_the_fanout_shape():
    with pytest.raises(CellError, match="only to the fan-out"):
        parse_cell(
            _cell_data(pipeline={"shape": "single", "dimensions": ["correctness"]})
        )


def test_per_dimension_override_parses_and_validates_membership():
    data = _cell_data(
        pipeline={"shape": "fanout", "dimensions": ["correctness", "test-quality"]}
    )
    data["invocation"]["dimensions"] = {"test-quality": {"model": "o3"}}
    cell = parse_cell(data)
    assert cell.dimension_invocations == {"test-quality": {"model": "o3"}}
    # An override on a pass the cell never runs is a reviewed lie — loud.
    data["invocation"]["dimensions"] = {"security-robustness": {"model": "o3"}}
    with pytest.raises(CellError, match="outside this cell's pass set"):
        parse_cell(data)


def test_omitted_dimensions_pass_set_is_the_shipped_default_not_the_registry():
    # ADR-0051: an omitted `pipeline.dimensions` runs ONLY the shipped default
    # four, never the experiment-only severity tiers. A per-dimension override
    # naming a tier is therefore outside this cell's pass set — this pins the
    # default-vs-registry distinction at the parse boundary, which would regress
    # silently if `effective_dimensions` fell back to the whole registry.
    data = _cell_data()  # pipeline.dimensions omitted -> shipped default four
    data["invocation"]["dimensions"] = {"sev-low": {"model": "o3"}}
    with pytest.raises(CellError, match="outside this cell's pass set"):
        parse_cell(data)
    # The same override is accepted once the cell lists the tier explicitly.
    data = _cell_data(pipeline={"shape": "fanout", "dimensions": ["sev-low"]})
    data["invocation"]["dimensions"] = {"sev-low": {"model": "o3"}}
    cell = parse_cell(data)
    assert cell.dimension_invocations == {"sev-low": {"model": "o3"}}


def test_explicit_empty_dimensions_list_is_rejected_loud():
    # An explicit `dimensions = []` is a config mistake, not a synonym for the
    # default set — reject it loud rather than silently running the shipped
    # default (the Roster `dimensions` option has the same non-empty posture).
    with pytest.raises(CellError, match="empty list"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "dimensions": []}))


def test_per_dimension_override_rejects_backend_and_unknown_fields():
    data = _cell_data()
    data["invocation"]["dimensions"] = {"correctness": {"backend": "agy"}}
    with pytest.raises(CellError, match="'backend' is not supported"):
        parse_cell(data)
    data["invocation"]["dimensions"] = {"correctness": {"modle": "x"}}
    with pytest.raises(CellError, match="unknown key"):
        parse_cell(data)
    data["invocation"]["dimensions"] = {"correctness": {}}
    with pytest.raises(CellError, match="is empty"):
        parse_cell(data)


def test_per_dimension_override_requires_the_fanout_shape():
    data = _cell_data(pipeline={"shape": "single"})
    data["invocation"]["dimensions"] = {"correctness": {"model": "o3"}}
    with pytest.raises(CellError, match="only to the fan-out shape"):
        parse_cell(data)


# --- dedup / calibrator ------------------------------------------------------------


def test_calibrated_dedup_requires_the_calibrator_table():
    with pytest.raises(CellError, match=r"\[pipeline.calibrator\]"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "dedup": "calibrated"}))


def test_calibrator_table_without_calibrated_dedup_is_loud():
    # A half-declared judge is an unlabeled arm.
    with pytest.raises(CellError, match="dedup is 'mechanical'"):
        parse_cell(
            _cell_data(
                pipeline={
                    "shape": "fanout",
                    "calibrator": {"backend": "claude"},
                }
            )
        )


def test_calibrator_config_is_constructed_so_a_bad_field_fails_loud():
    with pytest.raises(CellError, match="calibrator"):
        parse_cell(
            _cell_data(
                pipeline={
                    "shape": "fanout",
                    "dedup": "calibrated",
                    "calibrator": {"backend": "not-a-backend"},
                }
            )
        )
    cell = parse_cell(
        _cell_data(
            pipeline={
                "shape": "fanout",
                "dedup": "calibrated",
                "calibrator": {"backend": "claude", "reasoning": "high"},
            }
        )
    )
    assert cell.calibrator is not None and cell.calibrator.backend == "claude"


# --- sweeps ------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, "2"])
def test_sweep_count_and_replicates_must_be_positive_ints(bad):
    with pytest.raises(CellError, match="positive integer"):
        parse_cell(_cell_data(sweeps={"count": bad}))
    with pytest.raises(CellError, match="positive integer"):
        parse_cell(_cell_data(sweeps={"count": 1, "replicates": bad}))


def test_sweep_mode_vocabulary_is_closed():
    with pytest.raises(CellError, match="informed"):
        parse_cell(_cell_data(sweeps={"count": 1, "mode": "psychic"}))


def test_sweep_count_and_replicates_reject_absurd_values():
    """An unbounded count is an OOM vector — the runner allocates one point per
    pin × replicate × sweep, so a plan above the ceiling is a mistake, refused
    at parse before it can build a billion-tuple."""
    with pytest.raises(CellError, match="exceeds the max"):
        parse_cell(_cell_data(sweeps={"count": 1_000_000_000}))
    with pytest.raises(CellError, match="exceeds the max"):
        parse_cell(_cell_data(sweeps={"count": 1, "replicates": 2000}))


# --- instructions path safety (arbitrary-file-read guard) --------------------------


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "~/.ssh/id_rsa", "../../.aws/credentials", "sub/../../x"],
)
def test_instructions_path_rejects_absolute_home_and_traversal(bad_path):
    """A cell reads instructions from in-repo files ONLY: an absolute, '~', or
    '..' path is a loud refusal at parse — it could otherwise read a local
    secret off disk and hand its contents to the model as prompt text."""
    with pytest.raises(CellError, match="repo-relative"):
        parse_cell(_cell_data(instructions={"path": bad_path}))


def test_instructions_path_accepts_a_repo_relative_file():
    cell = parse_cell(_cell_data(instructions={"path": "lab/instructions/strict.txt"}))
    assert cell.instructions_path == "lab/instructions/strict.txt"


def test_instructions_field_errors_point_at_the_instructions_table():
    """A present-but-empty path/label error names `[instructions]`, not the whole
    cell, so the TOML is quick to fix."""
    with pytest.raises(CellError, match=r"\[instructions\]"):
        parse_cell(_cell_data(instructions={"path": "  "}))


@pytest.mark.parametrize("bad", ["../evil", "sub/x", "a\\b", ".", ".."])
def test_id_and_baseline_must_be_bare_cell_names(bad):
    """`id`/`baseline` each name a file under the cells directory; a
    path-separator or traversal value would escape it when the pair loads, so
    it is refused at parse."""
    with pytest.raises(CellError, match="bare cell name"):
        parse_cell(_cell_data(id=bad))
    with pytest.raises(CellError, match="bare cell name"):
        parse_cell(_treatment_data(baseline=bad))


# --- the file boundary ---------------------------------------------------------------


def _write_cell(path, cell_id, extra=""):
    path.write_text(
        f"""
schema = 1
id = "{cell_id}"
baseline = "{cell_id}"
axis = "control"
[fixture]
version = 1
[pipeline]
shape = "single"
{extra}
""",
        encoding="utf-8",
    )


def test_load_cell_enforces_id_equals_filename_stem(tmp_path):
    # A copy-edited treatment that forgot to change its id must fail loud, not
    # silently impersonate its control.
    path = tmp_path / "other-name.toml"
    _write_cell(path, "control")
    with pytest.raises(CellError, match="filename stem"):
        load_cell(path)


def test_load_cell_missing_file_and_bad_toml_are_loud(tmp_path):
    with pytest.raises(CellError, match="no cell file"):
        load_cell(tmp_path / "absent.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [valid", encoding="utf-8")
    with pytest.raises(CellError, match="not valid TOML"):
        load_cell(bad)


def test_resolve_cell_path_prefers_an_existing_path(tmp_path):
    direct = tmp_path / "x.toml"
    direct.write_text("", encoding="utf-8")
    assert resolve_cell_path(str(direct), tmp_path / "cells") == direct
    assert (
        resolve_cell_path("my-cell", tmp_path / "cells")
        == tmp_path / "cells" / "my-cell.toml"
    )


# --- the idempotency key --------------------------------------------------------------


def test_run_key_and_record_matching_use_the_full_key():
    cell = parse_cell(_cell_data())
    key = run_key(
        cell, pr_id="core-440", variant_hash="sha256:aa", replicate=1, sweep=2
    )
    record = {"round.cell": dict(key)}
    assert record_matches_key(record, key)
    # Non-key decoration (label/axis) does not affect matching…
    decorated = {"round.cell": {**key, "label": "other", "axis": "x"}}
    assert record_matches_key(decorated, key)
    # …but every ADR-0049 key field does.
    for field, other in [
        ("id", "other"),
        ("fixture_version", 2),
        ("pr", "app-391"),
        ("variant", "sha256:bb"),
        ("replicate", 2),
        ("sweep", 1),
    ]:
        assert not record_matches_key({"round.cell": {**key, field: other}}, key)
    assert not record_matches_key({"round.cell": None}, key)
    assert not record_matches_key({}, key)


def test_key_tuple_returns_none_for_a_corrupt_non_scalar_key_field():
    """A well-formed key packs to a hashable tuple; a corrupt tag with a
    non-scalar key field returns None, so a reader skips it instead of feeding
    an unhashable value into a set."""
    cell = parse_cell(_cell_data())
    key = run_key(
        cell, pr_id="core-440", variant_hash="sha256:aa", replicate=1, sweep=1
    )
    assert isinstance(key_tuple(key), tuple)
    assert key_tuple({**key, "id": []}) is None
    assert key_tuple({**key, "pr": {"nested": 1}}) is None


# --- informed-sweep composition --------------------------------------------------------


def test_compose_informed_instructions_is_identity_without_priors():
    assert compose_informed_instructions("base text", []) == "base text"


def test_compose_informed_instructions_embeds_prior_findings():
    composed = compose_informed_instructions(
        "base text",
        [
            {
                "file": "src/a.py",
                "line": 12,
                "severity": "major",
                "text": "row padding\nis missed",
            },
            {"file": "src/b.py", "severity": "minor", "text": "leaky handle"},
        ],
    )
    assert composed.startswith("base text")
    assert "already banked by prior sweeps" in composed
    # Location + claim, newlines flattened; a file-scoped finding renders bare.
    assert "- src/a.py:12 (major): row padding is missed" in composed
    assert "- src/b.py (minor): leaky handle" in composed
    assert "what they MISSED" in composed


def test_compose_informed_instructions_neutralizes_control_chars_and_caps():
    """Prior findings are untrusted (their fields trace back to diffs): a
    terminal-escape byte is neutralized before it reaches the prompt, and a
    flood of priors is capped with an explicit note — a poisoned or huge banked
    record can neither steer nor bloat the next sweep's instructions."""
    from shipit.review.cell import MAX_PRIOR_FINDINGS

    composed = compose_informed_instructions(
        "base",
        [{"file": "a.py", "line": 1, "severity": "major", "text": "x\x1b[31mred"}],
    )
    assert "\x1b" not in composed and "x·[31mred" in composed
    many = [
        {"file": f"f{i}.py", "line": i, "severity": "minor", "text": "t"}
        for i in range(MAX_PRIOR_FINDINGS + 5)
    ]
    capped = compose_informed_instructions("base", many)
    assert "5 more banked finding(s) omitted" in capped


# --- the fair-pair rule -----------------------------------------------------------------


def test_check_fair_pair_passes_a_fair_pair():
    check_fair_pair(
        parse_cell(_treatment_data()), parse_cell(_cell_data()), _fair_fixture()
    )


def test_check_fair_pair_treats_empty_prs_as_every_fixture_pin():
    """`prs = []` means "every fixture pin", so a control that omits `prs` and a
    treatment that lists all pins explicitly are ONE denominator — a fair pair,
    not a spurious mismatch. The check compares effective sets, not raw `prs`."""
    control = parse_cell(_cell_data(fixture={"version": 1}))  # prs omitted = all
    treatment = parse_cell(
        _treatment_data(fixture={"version": 1, "prs": ["core-440", "app-391"]})
    )
    check_fair_pair(treatment, control, _fair_fixture())  # no raise


def test_check_fair_pair_rejects_wrong_baseline_and_non_control():
    other = parse_cell(_cell_data(id="other-control", baseline="other-control"))
    with pytest.raises(CellError, match="declares baseline"):
        check_fair_pair(parse_cell(_treatment_data()), other, _fair_fixture())
    # Chained treatments hide axes: the named baseline must itself be a control.
    chained = parse_cell(_treatment_data())
    with pytest.raises(CellError, match="not a control"):
        check_fair_pair(
            parse_cell(_treatment_data(id="deeper", baseline="treatment")),
            chained,
            _fair_fixture(),
        )


def test_check_fair_pair_rejects_differing_denominators():
    control = parse_cell(_cell_data())
    with pytest.raises(CellError, match="fixture versions"):
        check_fair_pair(
            parse_cell(_treatment_data(fixture={"version": 2, "prs": ["core-440"]})),
            control,
            _fair_fixture(),
        )
    with pytest.raises(CellError, match="different PR subsets"):
        check_fair_pair(
            parse_cell(_treatment_data(fixture={"version": 1, "prs": ["app-391"]})),
            control,
            _fair_fixture(),
        )
