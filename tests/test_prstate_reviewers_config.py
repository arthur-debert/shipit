from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from shipit.prstate import reviewers_config
from shipit.prstate.reviewers import required_adapters
from shipit.prstate.reviewers_config import (
    DEFAULT_REVIEWERS,
    RequiredReviewersConfigError,
    default_roster,
    load_roster,
)


def _write(tmp_path, body: str) -> str:
    (tmp_path / ".shipit.toml").write_text(body)
    return str(tmp_path)


def test_config_error_is_a_prstate_error():
    from shipit.prstate.errors import PrStateError

    assert issubclass(RequiredReviewersConfigError, PrStateError)


def test_shipit_own_repo_requires_copilot_codex_agy():
    repo_root = Path(__file__).resolve().parent.parent
    roster = load_roster(str(repo_root))
    assert roster.required_names == ("copilot", "codex", "agy")


def test_default_is_copilot_only_review_once(tmp_path):
    assert DEFAULT_REVIEWERS == {"copilot": False}
    roster = load_roster(str(tmp_path))
    assert roster == default_roster()
    assert roster.required_names == ("copilot",)
    assert roster.entry("copilot").rerun is False


def test_scaffold_body_renders_from_the_default_map_and_round_trips(tmp_path):
    body = reviewers_config.default_reviewers_scaffold_body()
    assert body.startswith("[reviewers]\n")
    assert load_roster(_write(tmp_path, body)) == default_roster()


def test_scaffold_body_renders_each_reviewers_rerun_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reviewers_config, "DEFAULT_REVIEWERS", {"copilot": True, "codex": False}
    )
    body = reviewers_config.default_reviewers_scaffold_body()
    assert "copilot = { rerun = true }" in body
    assert "codex = { rerun = false }" in body
    assert tomllib.loads(body)
    roster = load_roster(_write(tmp_path, body))
    assert roster.entry("copilot").rerun is True
    assert roster.entry("codex").rerun is False


def test_empty_table_falls_back_to_default(tmp_path):
    assert load_roster(_write(tmp_path, "[reviewers]\n")) == default_roster()


def test_override_swaps_the_set_with_a_one_line_change(tmp_path):
    root = _write(
        tmp_path,
        "[reviewers]\ncopilot = { rerun = false }\ncoderabbit = { rerun = false }\n",
    )
    roster = load_roster(root)
    assert roster.required_names == ("copilot", "coderabbit")


def test_rerun_flags_are_per_reviewer(tmp_path):
    root = _write(
        tmp_path,
        "[reviewers]\ncopilot = { rerun = true }\ncodex = { rerun = false }\n",
    )
    roster = load_roster(root)
    assert roster.entry("copilot").rerun is True
    assert roster.entry("codex").rerun is False


def test_rerun_defaults_true_when_options_absent(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\ncodex = {}\n"))
    assert roster.entry("copilot").rerun is True
    assert roster.entry("codex").rerun is True


def test_rerun_false_is_an_explicit_opt_out(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = { rerun = false }\n"))
    assert roster.entry("copilot").rerun is False


def test_round_cap_defaults_to_unset(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\n"))
    assert roster.round_cap is None
    assert default_roster().round_cap is None


def test_round_cap_is_table_level_policy_not_a_reviewer_entry(tmp_path):
    root = _write(tmp_path, "[reviewers]\nround_cap = 3\ncopilot = {}\n")
    roster = load_roster(root)
    assert roster.round_cap == 3
    assert roster.required_names == ("copilot",)


def test_round_cap_applies_even_with_no_reviewer_entries(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\nround_cap = 2\n"))
    assert roster.required_names == default_roster().required_names
    assert roster.round_cap == 2


def test_round_cap_case_variant_key_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="round_cap"):
        load_roster(_write(tmp_path, "[reviewers]\nRound_Cap = 3\n"))
    with pytest.raises(RequiredReviewersConfigError, match="round_cap"):
        load_roster(_write(tmp_path, "[reviewers]\nROUND_CAP = 3\ncopilot = {}\n"))


def test_round_cap_rejects_non_int_and_non_positive_values(tmp_path):
    for bad in ("0", "-1", "true", '"6"', "2.5"):
        with pytest.raises(RequiredReviewersConfigError, match="round_cap"):
            load_roster(_write(tmp_path, f"[reviewers]\nround_cap = {bad}\n"))


def test_poll_interval_defaults_to_unset(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\n"))
    assert roster.poll_interval is None
    assert default_roster().poll_interval is None


def test_poll_interval_is_table_level_policy_not_a_reviewer_entry(tmp_path):
    root = _write(tmp_path, "[reviewers]\npoll_interval = 30\ncopilot = {}\n")
    roster = load_roster(root)
    assert roster.poll_interval == 30
    assert roster.required_names == ("copilot",)


def test_poll_interval_accepts_the_duration_shape(tmp_path):
    roster = load_roster(_write(tmp_path, '[reviewers]\npoll_interval = "90s"\n'))
    assert roster.poll_interval == 90


def test_poll_interval_applies_even_with_no_reviewer_entries(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\npoll_interval = 15\n"))
    assert roster.required_names == default_roster().required_names
    assert roster.poll_interval == 15


def test_poll_interval_case_variant_key_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="poll_interval"):
        load_roster(_write(tmp_path, "[reviewers]\nPoll_Interval = 30\n"))


def test_poll_interval_rejects_bad_values_loud(tmp_path):
    for bad in ("0", "-5", "true", '"soon"', "2.5"):
        with pytest.raises(RequiredReviewersConfigError, match="poll_interval"):
            load_roster(_write(tmp_path, f"[reviewers]\npoll_interval = {bad}\n"))


def test_run_options_are_carried_on_the_entry(tmp_path):
    root = _write(
        tmp_path,
        "[reviewers]\n"
        'codex = { rerun = true, model = "pro", instructions = "docs/review.md" }\n',
    )
    entry = load_roster(root).entry("codex")
    assert entry.rerun is True
    assert entry.model == "pro"
    assert entry.instructions == str(tmp_path / "docs" / "review.md")


def test_run_option_must_be_a_string(tmp_path):
    with pytest.raises(
        RequiredReviewersConfigError, match="must be a non-empty string"
    ):
        load_roster(_write(tmp_path, "[reviewers]\ncodex = { model = 3 }\n"))
    with pytest.raises(
        RequiredReviewersConfigError, match="must be a non-empty string"
    ):
        load_roster(_write(tmp_path, "[reviewers]\ncodex = { instructions = true }\n"))


def test_empty_run_option_string_rejected_at_load(tmp_path):
    for opt in ("model", "instructions"):
        with pytest.raises(
            RequiredReviewersConfigError, match="must be a non-empty string"
        ):
            load_roster(_write(tmp_path, f'[reviewers]\ncodex = {{ {opt} = "" }}\n'))
        with pytest.raises(
            RequiredReviewersConfigError, match="must be a non-empty string"
        ):
            load_roster(_write(tmp_path, f'[reviewers]\ncodex = {{ {opt} = "   " }}\n'))


def test_instructions_anchored_to_config_dir_not_cwd(tmp_path, monkeypatch):
    _write(tmp_path, '[reviewers]\ncodex = { instructions = "docs/rev.md" }\n')
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert load_roster().entry("codex").instructions == str(
        tmp_path / "docs" / "rev.md"
    )


def test_parse_roster_relative_config_path_keeps_instructions_absolute(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    roster = reviewers_config.parse_roster(
        {"reviewers": {"codex": {"instructions": "review.md"}}},
        config_path="config/custom-policy.toml",
    )
    assert roster.entry("codex").instructions == str(tmp_path / "config" / "review.md")


def test_absolute_instructions_kept(tmp_path):
    root = _write(tmp_path, '[reviewers]\ncodex = { instructions = "/abs/rev.md" }\n')
    assert load_roster(root).entry("codex").instructions == "/abs/rev.md"


def test_unconfigured_reviewer_reads_all_defaults(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\n"))
    entry = roster.entry("codex")
    assert entry.required is False
    assert entry.rerun is True
    assert (entry.model, entry.instructions, entry.timeout) == (None, None, None)


def test_timeout_reads_and_normalizes(tmp_path):
    root = _write(
        tmp_path,
        '[reviewers]\nagy = { timeout = "900s" }\ncodex = { timeout = 1200 }\n',
    )
    roster = load_roster(root)
    assert roster.entry("agy").timeout == "900s"
    assert roster.entry("codex").timeout == "1200s"


def test_timeout_omitted_when_unset(tmp_path):
    root = _write(tmp_path, '[reviewers]\nagy = { model = "pro" }\n')
    assert load_roster(root).entry("agy").timeout is None


def test_timeout_validated_loud_on_bad_input(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="timeout"):
        load_roster(_write(tmp_path, '[reviewers]\nagy = { timeout = "soon" }\n'))
    with pytest.raises(RequiredReviewersConfigError, match="positive"):
        load_roster(_write(tmp_path, "[reviewers]\nagy = { timeout = 0 }\n"))
    with pytest.raises(RequiredReviewersConfigError, match="timeout"):
        load_roster(_write(tmp_path, "[reviewers]\nagy = { timeout = true }\n"))


def test_window_reads_and_normalizes_to_seconds(tmp_path):
    root = _write(
        tmp_path,
        '[reviewers]\ncopilot = { window = "1800s" }\ncodex = { window = 600 }\n',
    )
    roster = load_roster(root)
    assert roster.entry("copilot").window_seconds == 1800
    assert roster.entry("codex").window_seconds == 600


def test_window_absent_is_none(tmp_path):
    root = _write(
        tmp_path,
        '[reviewers]\ncopilot = { rerun = false }\ncodex = { window = "300s" }\n',
    )
    roster = load_roster(root)
    assert roster.entry("copilot").window_seconds is None
    assert roster.entry("codex").window_seconds == 300


def test_window_validated_loud_on_bad_input(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="window"):
        load_roster(_write(tmp_path, '[reviewers]\ncopilot = { window = "soon" }\n'))
    with pytest.raises(RequiredReviewersConfigError, match="positive"):
        load_roster(_write(tmp_path, "[reviewers]\ncopilot = { window = 0 }\n"))
    with pytest.raises(RequiredReviewersConfigError, match="window"):
        load_roster(_write(tmp_path, "[reviewers]\ncopilot = { window = true }\n"))


def test_list_array_form_is_rejected_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="TABLE"):
        load_roster(_write(tmp_path, 'reviewers = ["copilot", "codex"]\n'))


def test_wrong_typed_reviewers_value_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="must be a TABLE"):
        load_roster(_write(tmp_path, 'reviewers = "copilot"\n'))


def test_map_keys_are_canonicalized_to_adapter_names(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\nCopilot = { rerun = true }\n"))
    assert roster.required_names == ("copilot",)
    assert roster.entry("copilot").rerun is True


def test_window_key_canonicalized_too(tmp_path):
    roster = load_roster(
        _write(tmp_path, '[reviewers]\nCopilot = { window = "120s" }\n')
    )
    assert roster.entry("copilot").window_seconds == 120


def test_map_keys_colliding_after_canonicalization_fail_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="duplicate"):
        load_roster(_write(tmp_path, "[reviewers]\nCopilot = {}\ncopilot = {}\n"))


def test_local_backends_are_requestable_and_can_be_required(tmp_path):
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\nagy = {}\n"))
    assert roster.required_names == ("copilot", "agy")


def test_unknown_reviewer_name_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="gpt5"):
        load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\ngpt5 = {}\n"))


def test_non_requestable_reviewer_cannot_be_required(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="non-requestable"):
        load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\ngemini = {}\n"))


def test_unknown_per_reviewer_option_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="unknown option"):
        load_roster(_write(tmp_path, "[reviewers]\ncopilot = { reroll = true }\n"))


def test_non_bool_rerun_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="must be a boolean"):
        load_roster(_write(tmp_path, '[reviewers]\ncopilot = { rerun = "yes" }\n'))


def test_malformed_toml_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="malformed"):
        load_roster(_write(tmp_path, "[reviewers\ncopilot = {}\n"))


def test_absent_file_is_the_default_roster(tmp_path):
    assert load_roster(str(tmp_path)) == default_roster()


def test_missing_table_is_the_default_roster(tmp_path):
    root = _write(tmp_path, '[secrets]\nGH_PAT = { env = "X" }\n')
    assert load_roster(root) == default_roster()


def test_loader_searches_up_to_the_repo_root(tmp_path, monkeypatch):
    _write(tmp_path, "[reviewers]\ncodex = { rerun = true }\n")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert load_roster().entry("codex").rerun is True


def test_required_adapters_maps_the_roster_in_config_order(tmp_path):
    root = _write(tmp_path, "[reviewers]\ncoderabbit = {}\ncopilot = {}\n")
    adapters = required_adapters(load_roster(root))
    assert [a.name for a in adapters] == ["coderabbit", "copilot"]


def test_dimensions_option_lands_on_the_entry(tmp_path):
    root = _write(
        tmp_path,
        '[reviewers]\ncodex = { dimensions = ["correctness", "test-quality"] }\n',
    )
    entry = load_roster(root).entry("codex")
    assert entry.dimensions == ("correctness", "test-quality")


def test_dimensions_default_is_unset(tmp_path):
    root = _write(tmp_path, "[reviewers]\ncodex = {}\n")
    assert load_roster(root).entry("codex").dimensions is None


def test_unknown_dimension_fails_loud_with_the_known_set(tmp_path):
    root = _write(tmp_path, '[reviewers]\ncodex = { dimensions = ["highs-only"] }\n')
    with pytest.raises(RequiredReviewersConfigError, match="known dimensions"):
        load_roster(root)


def test_dimensions_wrong_shape_and_duplicates_fail_loud(tmp_path):
    for bad in (
        'codex = { dimensions = "correctness" }\n',
        "codex = { dimensions = [] }\n",
        "codex = { dimensions = [1] }\n",
        'codex = { dimensions = ["correctness", "correctness"] }\n',
    ):
        root = _write(tmp_path, f"[reviewers]\n{bad}")
        with pytest.raises(RequiredReviewersConfigError, match="dimensions"):
            load_roster(root)


def test_nit_cap_lands_on_the_roster_and_zero_is_legal(tmp_path):
    root = _write(tmp_path, "[reviewers]\nnit_cap = 0\ncodex = {}\n")
    assert load_roster(root).nit_cap == 0
    root = _write(tmp_path, "[reviewers]\nnit_cap = 3\ncodex = {}\n")
    assert load_roster(root).nit_cap == 3


def test_nit_cap_default_is_uncapped(tmp_path):
    root = _write(tmp_path, "[reviewers]\ncodex = {}\n")
    assert load_roster(root).nit_cap is None


def test_nit_cap_rejects_negative_bool_and_non_int(tmp_path):
    for bad in ("nit_cap = -1", "nit_cap = true", 'nit_cap = "3"'):
        root = _write(tmp_path, f"[reviewers]\n{bad}\ncodex = {{}}\n")
        with pytest.raises(RequiredReviewersConfigError, match="nit_cap"):
            load_roster(root)


def test_calibrator_table_builds_a_validated_config(tmp_path):
    root = _write(
        tmp_path,
        "[reviewers]\n"
        'calibrator = { backend = "codex", model = "gpt-5.5", '
        'reasoning = "medium", timeout = 300 }\n'
        "codex = {}\n",
    )
    calibrator = load_roster(root).calibrator
    assert calibrator.backend == "codex"
    assert calibrator.model == "gpt-5.5"
    assert calibrator.reasoning == "medium"
    assert calibrator.timeout == "300s"


def test_calibrator_default_is_unset_meaning_shipped_default(tmp_path):
    root = _write(tmp_path, "[reviewers]\ncodex = {}\n")
    assert load_roster(root).calibrator is None


def test_calibrator_unknown_key_fails_loud(tmp_path):
    root = _write(
        tmp_path, '[reviewers]\ncalibrator = { agent = "claude" }\ncodex = {}\n'
    )
    with pytest.raises(RequiredReviewersConfigError, match="unknown option"):
        load_roster(root)


def test_calibrator_invalid_values_fail_loud_at_load(tmp_path):
    for bad in (
        'calibrator = { backend = "gpt-cli" }',
        'calibrator = { reasoning = "ultra" }',
        "calibrator = { timeout = -5 }",
        "calibrator = [1]",
    ):
        root = _write(tmp_path, f"[reviewers]\n{bad}\ncodex = {{}}\n")
        with pytest.raises(RequiredReviewersConfigError, match="calibrator"):
            load_roster(root)


def test_table_level_policy_applies_with_default_reviewer_entries(tmp_path):
    root = _write(
        tmp_path, '[reviewers]\nnit_cap = 2\ncalibrator = { backend = "claude" }\n'
    )
    roster = load_roster(root)
    assert roster.required_names == tuple(reviewers_config.DEFAULT_REVIEWERS)
    assert roster.nit_cap == 2
    assert roster.calibrator.backend == "claude"


def test_roster_policy_bundles_the_table_level_values(tmp_path):
    root = _write(
        tmp_path, '[reviewers]\nnit_cap = 1\ncalibrator = { backend = "claude" }\n'
    )
    policy = load_roster(root).policy
    assert policy.nit_cap == 1
    assert policy.calibrator.backend == "claude"
