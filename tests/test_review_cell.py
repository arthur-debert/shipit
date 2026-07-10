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
    load_cell,
    parse_cell,
    record_matches_key,
    resolve_cell_path,
    run_key,
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
    # No replay backend carries a reasoning knob yet: accepting the field would
    # stamp arms with a level that never reached a backend — the RVW02 failure.
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


# --- the fair-pair rule -----------------------------------------------------------------


def test_check_fair_pair_passes_a_fair_pair():
    check_fair_pair(parse_cell(_treatment_data()), parse_cell(_cell_data()))


def test_check_fair_pair_rejects_wrong_baseline_and_non_control():
    other = parse_cell(_cell_data(id="other-control", baseline="other-control"))
    with pytest.raises(CellError, match="declares baseline"):
        check_fair_pair(parse_cell(_treatment_data()), other)
    # Chained treatments hide axes: the named baseline must itself be a control.
    chained = parse_cell(_treatment_data())
    with pytest.raises(CellError, match="not a control"):
        check_fair_pair(
            parse_cell(_treatment_data(id="deeper", baseline="treatment")), chained
        )


def test_check_fair_pair_rejects_differing_denominators():
    control = parse_cell(_cell_data())
    with pytest.raises(CellError, match="fixture versions"):
        check_fair_pair(
            parse_cell(_treatment_data(fixture={"version": 2, "prs": ["core-440"]})),
            control,
        )
    with pytest.raises(CellError, match="different PR subsets"):
        check_fair_pair(
            parse_cell(_treatment_data(fixture={"version": 1, "prs": ["app-391"]})),
            control,
        )
