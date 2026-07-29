from __future__ import annotations

import pytest

from shipit.review.cell import (
    CellError,
    check_baseline_lineage,
    check_fair_pair,
    compose_informed_instructions,
    instructions_variant_text,
    key_tuple,
    load_baseline_lineage,
    load_cell,
    parse_cell,
    record_matches_key,
    resolve_cell_path,
    run_key,
)
from shipit.review.groundtruth import parse_fixture


def _fair_fixture():
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


def test_unknown_top_level_key_is_loud():
    with pytest.raises(CellError, match="unknown key"):
        parse_cell(_cell_data(sweepz={"count": 3}))


def test_unknown_pipeline_key_is_loud():
    with pytest.raises(CellError, match="unknown key"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "nit_capp": 3}))


def test_invocation_reasoning_is_rejected_as_not_wireable():
    data = _cell_data()
    data["invocation"]["reasoning"] = "high"
    with pytest.raises(CellError, match="reasoning.*not wireable"):
        parse_cell(data)


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
    data["invocation"]["dimensions"] = {"security-robustness": {"model": "o3"}}
    with pytest.raises(CellError, match="outside this cell's pass set"):
        parse_cell(data)


def test_omitted_dimensions_pass_set_is_the_shipped_default_not_the_registry():
    data = _cell_data()
    data["invocation"]["dimensions"] = {"sev-low": {"model": "o3"}}
    with pytest.raises(CellError, match="outside this cell's pass set"):
        parse_cell(data)
    data = _cell_data(pipeline={"shape": "fanout", "dimensions": ["sev-low"]})
    data["invocation"]["dimensions"] = {"sev-low": {"model": "o3"}}
    cell = parse_cell(data)
    assert cell.dimension_invocations == {"sev-low": {"model": "o3"}}


def test_explicit_empty_dimensions_list_is_rejected_loud():
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


def test_calibrated_dedup_requires_the_calibrator_table():
    with pytest.raises(CellError, match=r"\[pipeline.calibrator\]"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "dedup": "calibrated"}))


def test_calibrator_table_without_calibrated_dedup_is_loud():
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


def test_semantic_dedup_parses_for_the_fanout_shape():
    cell = parse_cell(_cell_data(pipeline={"shape": "fanout", "dedup": "semantic"}))
    assert cell.dedup == "semantic"
    assert cell.calibrator is None


def test_semantic_dedup_rejects_the_single_shape():
    with pytest.raises(CellError, match="only to the fan-out shape"):
        parse_cell(_cell_data(pipeline={"shape": "single", "dedup": "semantic"}))


def test_calibrator_table_with_semantic_dedup_is_loud():
    with pytest.raises(CellError, match="dedup is 'semantic'"):
        parse_cell(
            _cell_data(
                pipeline={
                    "shape": "fanout",
                    "dedup": "semantic",
                    "calibrator": {"backend": "claude"},
                }
            )
        )


def test_dedup_vocabulary_is_closed():
    with pytest.raises(CellError, match="mechanical, semantic, calibrated"):
        parse_cell(_cell_data(pipeline={"shape": "fanout", "dedup": "psychic"}))


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
    with pytest.raises(CellError, match="exceeds the max"):
        parse_cell(_cell_data(sweeps={"count": 1_000_000_000}))
    with pytest.raises(CellError, match="exceeds the max"):
        parse_cell(_cell_data(sweeps={"count": 1, "replicates": 2000}))


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "~/.ssh/id_rsa", "../../.aws/credentials", "sub/../../x"],
)
def test_instructions_path_rejects_absolute_home_and_traversal(bad_path):
    with pytest.raises(CellError, match="repo-relative"):
        parse_cell(_cell_data(instructions={"path": bad_path}))


def test_instructions_path_accepts_a_repo_relative_file():
    cell = parse_cell(_cell_data(instructions={"path": "lab/instructions/strict.txt"}))
    assert cell.instructions_path == "lab/instructions/strict.txt"


def test_instructions_field_errors_point_at_the_instructions_table():
    with pytest.raises(CellError, match=r"\[instructions\]"):
        parse_cell(_cell_data(instructions={"path": "  "}))


@pytest.mark.parametrize("bad", ["../evil", "sub/x", "a\\b", ".", ".."])
def test_id_and_baseline_must_be_bare_cell_names(bad):
    with pytest.raises(CellError, match="bare cell name"):
        parse_cell(_cell_data(id=bad))
    with pytest.raises(CellError, match="bare cell name"):
        parse_cell(_treatment_data(baseline=bad))


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


def test_run_key_and_record_matching_use_the_full_key():
    cell = parse_cell(_cell_data())
    key = run_key(
        cell, pr_id="core-440", variant_hash="sha256:aa", replicate=1, sweep=2
    )
    record = {"round.cell": dict(key)}
    assert record_matches_key(record, key)
    decorated = {"round.cell": {**key, "label": "other", "axis": "x"}}
    assert record_matches_key(decorated, key)
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
    cell = parse_cell(_cell_data())
    key = run_key(
        cell, pr_id="core-440", variant_hash="sha256:aa", replicate=1, sweep=1
    )
    assert isinstance(key_tuple(key), tuple)
    assert key_tuple({**key, "id": []}) is None
    assert key_tuple({**key, "pr": {"nested": 1}}) is None


def test_instructions_variant_text_single_shape_is_the_base_text_verbatim():
    cell = parse_cell(_cell_data(pipeline={"shape": "single"}))
    assert instructions_variant_text(cell, "base text") == "base text"


def test_instructions_variant_text_fanout_folds_the_dimension_set():
    base = "base text"
    default = parse_cell(_cell_data())
    tiers = parse_cell(
        _cell_data(
            pipeline={
                "shape": "fanout",
                "dimensions": ["sev-critical-high", "sev-medium", "sev-low"],
            }
        )
    )
    assert instructions_variant_text(default, base) != base
    assert instructions_variant_text(tiers, base) != instructions_variant_text(
        default, base
    )


def test_instructions_variant_text_fanout_folds_dimension_overrides():
    plain = parse_cell(
        _cell_data(pipeline={"shape": "fanout", "dimensions": ["correctness"]})
    )
    overridden = parse_cell(
        _cell_data(
            pipeline={"shape": "fanout", "dimensions": ["correctness"]},
            invocation={
                "backend": "codex",
                "dimensions": {"correctness": {"model": "o3"}},
            },
        )
    )
    assert instructions_variant_text(plain, "x") != instructions_variant_text(
        overridden, "x"
    )


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
    assert "- src/a.py:12 (major): row padding is missed" in composed
    assert "- src/b.py (minor): leaky handle" in composed
    assert "what they MISSED" in composed


def test_compose_informed_instructions_neutralizes_control_chars_and_caps():
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


def test_check_fair_pair_passes_a_fair_pair():
    check_fair_pair(
        parse_cell(_treatment_data()), parse_cell(_cell_data()), _fair_fixture()
    )


def test_check_fair_pair_treats_empty_prs_as_every_fixture_pin():
    control = parse_cell(_cell_data(fixture={"version": 1}))
    treatment = parse_cell(
        _treatment_data(fixture={"version": 1, "prs": ["core-440", "app-391"]})
    )
    check_fair_pair(treatment, control, _fair_fixture())


def test_check_fair_pair_rejects_wrong_baseline():
    other = parse_cell(_cell_data(id="other-control", baseline="other-control"))
    with pytest.raises(CellError, match="declares baseline"):
        check_fair_pair(parse_cell(_treatment_data()), other, _fair_fixture())


def test_check_fair_pair_accepts_a_treatment_baseline():
    chained = parse_cell(_treatment_data())
    check_fair_pair(
        parse_cell(
            _treatment_data(
                id="deeper",
                baseline="treatment",
                axis="pass scoping: severity tiers vs concern dimensions",
            )
        ),
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


def _lineage_resolver(cells):

    def resolve(current):
        parent = cells.get(current.baseline)
        if parent is None:
            raise CellError(
                f"cell {current.id!r} names baseline {current.baseline!r} "
                "which does not exist"
            )
        return parent

    return resolve


def test_check_baseline_lineage_walks_a_chain_to_the_control():
    control = parse_cell(_cell_data())
    mid = parse_cell(_treatment_data())
    deep = parse_cell(
        _treatment_data(
            id="deeper",
            baseline="treatment",
            axis="pass scoping: severity tiers vs concern dimensions",
        )
    )
    cells = {c.id: c for c in (control, mid, deep)}
    chain = check_baseline_lineage(deep, _fair_fixture(), _lineage_resolver(cells))
    assert [c.id for c in chain] == ["deeper", "treatment", "control"]
    assert chain[-1].is_control


def test_check_baseline_lineage_control_is_its_own_one_cell_chain():
    control = parse_cell(_cell_data())

    def never(_current):  # pragma: no cover - the walker must not call it
        raise AssertionError("a control has no baseline hop to resolve")

    assert check_baseline_lineage(control, _fair_fixture(), never) == (control,)


def test_check_baseline_lineage_rejects_a_cycle():
    a = parse_cell(_treatment_data(id="a", baseline="b", axis="x"))
    b = parse_cell(_treatment_data(id="b", baseline="a", axis="y"))
    with pytest.raises(CellError, match="cyclic baseline chain"):
        check_baseline_lineage(a, _fair_fixture(), _lineage_resolver({"a": a, "b": b}))


def test_check_baseline_lineage_rejects_a_missing_ancestor():
    mid = parse_cell(_treatment_data(id="mid", baseline="ghost", axis="x"))
    deep = parse_cell(_treatment_data(id="deep", baseline="mid", axis="y"))
    with pytest.raises(CellError, match="'ghost'"):
        check_baseline_lineage(
            deep, _fair_fixture(), _lineage_resolver({"mid": mid, "deep": deep})
        )


def test_check_baseline_lineage_fair_pair_checks_every_hop():
    control = parse_cell(_cell_data())
    mid = parse_cell(
        _treatment_data(id="mid", fixture={"version": 2, "prs": ["core-440"]})
    )
    deep = parse_cell(
        _treatment_data(
            id="deep",
            baseline="mid",
            axis="y",
            fixture={"version": 2, "prs": ["core-440"]},
        )
    )
    cells = {c.id: c for c in (control, mid, deep)}
    with pytest.raises(CellError, match="different fixture versions"):
        check_baseline_lineage(deep, _fair_fixture(), _lineage_resolver(cells))


def test_load_baseline_lineage_names_the_missing_cell_and_the_dir(tmp_path):
    path = tmp_path / "treat.toml"
    path.write_text(
        """
schema = 1
id = "treat"
baseline = "ghost"
axis = "x"
[fixture]
version = 1
prs = ["core-440"]
[pipeline]
shape = "single"
""",
        encoding="utf-8",
    )
    with pytest.raises(CellError) as exc:
        load_baseline_lineage(load_cell(path), _fair_fixture(), tmp_path)
    message = str(exc.value)
    assert "'ghost'" in message and str(tmp_path) in message
    assert "does not exist" in message


def test_load_baseline_lineage_loads_a_committed_chain(tmp_path):

    def cell_toml(cell_id, baseline, axis):
        return f"""
schema = 1
id = "{cell_id}"
baseline = "{baseline}"
axis = "{axis}"
[fixture]
version = 1
prs = ["core-440"]
[pipeline]
shape = "single"
"""

    for cell_id, baseline, axis in (
        ("ctl", "ctl", "control"),
        ("treat", "ctl", "x"),
        ("compose", "treat", "y"),
    ):
        (tmp_path / f"{cell_id}.toml").write_text(
            cell_toml(cell_id, baseline, axis), encoding="utf-8"
        )
    chain = load_baseline_lineage(
        load_cell(tmp_path / "compose.toml"), _fair_fixture(), tmp_path
    )
    assert [c.id for c in chain] == ["compose", "treat", "ctl"]
