"""The required-reviewer SET + per-reviewer rerun policy is config, not code.

Proves the `[reviewers]` config is data-driven: a shipped default
({copilot: rerun=False} — review-once), a per-repo `.shipit.toml` override (map
or list shorthand), per-reviewer `rerun` flags, the reserved `model`/
`instructions` fields, and unknown / non-requestable names failing LOUD. The
engine-side proof (a DIFFERENT set drives a DIFFERENT verdict) lives in
test_prstate_state.py::test_required_set_is_data_driven_*.

Ported from release-core: the pure-seam tests are unchanged (re-pointed to
`shipit.prstate`); the loader tests are re-pointed from `.release-sync.yaml`
(yq) to an in-process `tomllib` read of `[reviewers]` in `.shipit.toml`.
"""

from __future__ import annotations

import pytest
from shipit.prstate import reviewers_config
from shipit.prstate.reviewers_config import (
    DEFAULT_REVIEWERS,
    RequiredReviewersConfigError,
    resolve_required_names,
    resolve_reviewers,
    reviewer_rerun,
)


def test_default_is_copilot_only_review_once():
    # CodeRabbit is a phos-org pilot: the App is only installed there, so
    # requiring it by default would park every other repo at REVIEWS_PENDING.
    # rerun defaults False — review once (re-run is opt-in for everyone).
    assert DEFAULT_REVIEWERS == {"copilot": False}
    assert resolve_reviewers(None) == {"copilot": False}
    assert resolve_required_names(None) == ("copilot",)
    assert reviewer_rerun(None) == {"copilot": False}


def test_empty_override_falls_back_to_default():
    # `reviewers = {}` is "unset", never "disable all review gating".
    assert resolve_reviewers({}) == {"copilot": False}


def test_override_swaps_the_set_with_a_one_line_change():
    # A pilot repo opts into CodeRabbit (or any other set) — only config changed.
    parsed = reviewers_config._parse_override_value(
        {"copilot": {"rerun": False}, "coderabbit": {"rerun": False}}
    )
    assert resolve_required_names(parsed) == ("copilot", "coderabbit")
    assert resolve_reviewers(parsed) == {"copilot": False, "coderabbit": False}


def test_rerun_flags_are_per_reviewer():
    parsed = reviewers_config._parse_override_value(
        {"copilot": {"rerun": True}, "codex": {"rerun": False}}
    )
    assert reviewer_rerun(parsed) == {"copilot": True, "codex": False}


def test_rerun_defaults_false_when_options_absent():
    # `copilot = {}` with an empty/null options value means defaults — rerun=False.
    parsed = reviewers_config._parse_override_value({"copilot": None, "codex": {}})
    assert parsed == {"copilot": False, "codex": False}
    assert reviewer_rerun(parsed) == {"copilot": False, "codex": False}


# --- reserved fields (model / instructions) ---------------------------------


def test_reserved_model_and_instructions_are_accepted_but_not_consumed():
    # PRD: `model` / `instructions` are parsed + validated NOW but RESERVED for
    # the deferred local-agent step — they don't affect this epic's behaviour
    # (only the rerun flag is consumed). A valid (string) value must not error.
    parsed = reviewers_config._parse_override_value(
        {"codex": {"rerun": True, "model": "pro", "instructions": "docs/review.md"}}
    )
    assert parsed == {"codex": True}  # only rerun is consumed


def test_reserved_field_must_be_a_string():
    with pytest.raises(RequiredReviewersConfigError, match="must be a string"):
        reviewers_config._parse_override_value({"codex": {"model": 3}})
    with pytest.raises(RequiredReviewersConfigError, match="must be a string"):
        reviewers_config._parse_override_value({"codex": {"instructions": True}})


# --- list shorthand ---------------------------------------------------------


def test_list_shorthand_means_all_required_rerun_false():
    parsed = reviewers_config._parse_override_value(["copilot", "codex", "agy"])
    assert parsed == {"copilot": False, "codex": False, "agy": False}
    assert resolve_required_names(parsed) == ("copilot", "codex", "agy")


def test_list_shorthand_rejects_non_string_entries():
    with pytest.raises(RequiredReviewersConfigError, match="list shorthand"):
        reviewers_config._parse_override_value(["copilot", 3])


def test_list_shorthand_rejects_duplicates():
    # A repeated reviewer in the list shorthand is always a typo, not two gates —
    # it must fail loud, not silently dedup (release#852).
    with pytest.raises(RequiredReviewersConfigError, match="duplicate"):
        reviewers_config._parse_override_value(["copilot", "copilot"])


# --- reviewer-name key normalization (release#852) --------------------------


def test_map_keys_are_canonicalized_to_adapter_names():
    # A `Copilot` key must key the rerun map by the canonical adapter name
    # (`copilot`, lowercase) — the same name the adapters read off the context
    # (`ctx.reviewer_rerun.get(adapter.name, ...)`). Without this, a `rerun: true`
    # keyed `Copilot` is never applied and head-strict silently degrades to
    # review-once.
    parsed = reviewers_config._parse_override_value({"Copilot": {"rerun": True}})
    assert parsed == {"copilot": True}
    assert resolve_required_names(parsed) == ("copilot",)
    assert reviewer_rerun(parsed)["copilot"] is True


def test_list_shorthand_keys_are_canonicalized():
    parsed = reviewers_config._parse_override_value(["Copilot", "CodeRabbit"])
    assert parsed == {"copilot": False, "coderabbit": False}


def test_map_keys_colliding_after_canonicalization_fail_loud():
    # `Copilot` + `copilot` are byte-distinct keys (so a parser's own
    # duplicate-key rejection misses them) but canonicalize to one adapter — a
    # typo, never two gates. It must fail loud, not silently clobber.
    with pytest.raises(RequiredReviewersConfigError, match="duplicate"):
        reviewers_config._parse_override_value({"Copilot": {}, "copilot": {}})


# --- validation (loud) ------------------------------------------------------


def test_local_backends_are_requestable_and_can_be_required():
    # codex / agy are requestable local backends, so they are valid in the
    # required set (they post a real review the gate can read as done).
    assert resolve_required_names({"codex": False}) == ("codex",)
    assert resolve_required_names({"copilot": False, "agy": True}) == ("copilot", "agy")


def test_unknown_reviewer_name_fails_loud():
    with pytest.raises(RequiredReviewersConfigError, match="gpt5"):
        resolve_reviewers({"copilot": False, "gpt5": False})


def test_non_requestable_reviewer_cannot_be_required():
    # Gemini auto-triggers and has no request mechanism, so it can never satisfy
    # a required gate — configuring it required fails loud at parse time.
    with pytest.raises(RequiredReviewersConfigError, match="non-requestable"):
        resolve_reviewers({"copilot": False, "gemini": False})


def test_unknown_per_reviewer_option_fails_loud():
    with pytest.raises(RequiredReviewersConfigError, match="unknown option"):
        reviewers_config._parse_override_value({"copilot": {"reroll": True}})


def test_non_bool_rerun_fails_loud():
    with pytest.raises(RequiredReviewersConfigError, match="must be a boolean"):
        reviewers_config._parse_override_value({"copilot": {"rerun": "yes"}})


def test_wrong_typed_reviewers_value_fails_loud():
    with pytest.raises(RequiredReviewersConfigError, match="must be a map"):
        reviewers_config._parse_override_value("copilot")


def test_required_reviewers_maps_names_to_adapters_in_order():
    adapters = reviewers_config.required_reviewers(("coderabbit", "copilot"))
    assert [a.name for a in adapters] == ["coderabbit", "copilot"]


def test_required_reviewers_rejects_unknown_name():
    with pytest.raises(RequiredReviewersConfigError):
        reviewers_config.required_reviewers(("nope",))


# --- the override loader (the one filesystem seam: .shipit.toml [reviewers]) -


def test_load_override_absent_file_is_none(tmp_path):
    assert reviewers_config.load_override(str(tmp_path)) is None


def test_load_override_missing_table_is_none(tmp_path):
    # A `.shipit.toml` with no `[reviewers]` table → None (default applies).
    (tmp_path / ".shipit.toml").write_text('[secrets]\nGH_PAT = { env = "X" }\n')
    assert reviewers_config.load_override(str(tmp_path)) is None


def test_load_override_empty_table_is_none(tmp_path):
    # An empty `[reviewers]` table is "unset" → None (default applies), never
    # "disable all gating".
    (tmp_path / ".shipit.toml").write_text("[reviewers]\n")
    assert reviewers_config.load_override(str(tmp_path)) is None


def test_load_override_reads_the_map(tmp_path):
    (tmp_path / ".shipit.toml").write_text(
        "[reviewers]\ncoderabbit = { rerun = true }\n"
    )
    assert reviewers_config.load_override(str(tmp_path)) == {"coderabbit": True}


def test_load_override_reads_the_list_shorthand(tmp_path):
    (tmp_path / ".shipit.toml").write_text('reviewers = ["copilot", "codex"]\n')
    assert reviewers_config.load_override(str(tmp_path)) == {
        "copilot": False,
        "codex": False,
    }


def test_load_override_reads_reserved_fields(tmp_path):
    # The full inline-table shape from the PRD example parses; only rerun is
    # consumed (model/instructions are reserved but validated).
    (tmp_path / ".shipit.toml").write_text(
        '[reviewers]\n'
        'copilot = { rerun = false }\n'
        'codex = { rerun = false, model = "pro", instructions = "docs/review.md" }\n'
    )
    assert reviewers_config.load_override(str(tmp_path)) == {
        "copilot": False,
        "codex": False,
    }


def test_load_override_rejects_a_wrong_typed_value(tmp_path):
    (tmp_path / ".shipit.toml").write_text('reviewers = "copilot"\n')
    with pytest.raises(RequiredReviewersConfigError, match="must be a map"):
        reviewers_config.load_override(str(tmp_path))


def test_load_override_searches_up_to_the_repo_root(tmp_path, monkeypatch):
    # The loader walks up from cwd for the repo-root `.shipit.toml`, so a call
    # from a nested subdir still finds the config.
    (tmp_path / ".shipit.toml").write_text("[reviewers]\ncodex = { rerun = true }\n")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert reviewers_config.load_override() == {"codex": True}
