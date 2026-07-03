"""Reviewer configuration is config, not code — loaded ONCE into a Roster.

Proves the `[reviewers]` config is data-driven and that `load_roster` is the
ONE boundary read (CLI01-WS04): a shipped default ({copilot: rerun=False} —
review-once), a per-repo `.shipit.toml` override (a TABLE only — the
list/array form is rejected loud), per-reviewer `rerun` / `window` /
`model` / `instructions` / `timeout` options carried on RosterEntry values,
and unknown / non-requestable names failing LOUD at load. The engine-side
proof (a DIFFERENT set drives a DIFFERENT verdict) lives in
test_prstate_state.py::test_required_set_is_data_driven_*; the pure
Roster/RosterEntry construction-is-validation proofs live in
test_prstate_roster.py.

Ported from release-core, re-shaped for the Roster (CLI01-WS04): the three
parallel dict resolvers (required / rerun / window / run-options) are gone —
every assertion here reads settings off ONE loaded Roster value.
"""

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
    # A bad `.shipit.toml` is a user-renderable engine failure: the `pr` verbs
    # catch `(ExecError, PrStateError)` and render a clean `error: …` line, so
    # the config error MUST be a PrStateError (not a bare RuntimeError that would
    # escape the catch as an unhandled traceback).
    from shipit.prstate.errors import PrStateError

    assert issubclass(RequiredReviewersConfigError, PrStateError)


def test_shipit_own_repo_requires_copilot_codex_agy():
    # Task A / FLU01: shipit DOGFOODS its local reviewers — its own
    # `.shipit.toml` `[reviewers]` table holds Ready on copilot + codex + agy
    # (reversing the earlier "own PRs not held by a local reviewer" choice).
    # Reading the real repo config keeps this assertion honest about the
    # shipped policy.
    repo_root = Path(__file__).resolve().parent.parent
    roster = load_roster(str(repo_root))
    assert roster.required_names == ("copilot", "codex", "agy")


def test_default_is_copilot_only_review_once(tmp_path):
    # CodeRabbit is a phos-org pilot: the App is only installed there, so
    # requiring it by default would park every other repo at REVIEWS_PENDING.
    # rerun defaults False — review once (re-run is opt-in for everyone).
    assert DEFAULT_REVIEWERS == {"copilot": False}
    roster = load_roster(str(tmp_path))  # no .shipit.toml anywhere up tmp_path
    assert roster == default_roster()
    assert roster.required_names == ("copilot",)
    assert roster.entry("copilot").rerun is False


def test_scaffold_body_renders_from_the_default_map_and_round_trips(tmp_path):
    # The install scaffold is rendered FAITHFULLY from DEFAULT_REVIEWERS (keys AND
    # values), so the seeded `.shipit.toml` and the engine default cannot diverge:
    # LOADING the scaffold back must yield exactly the default Roster.
    body = reviewers_config.default_reviewers_scaffold_body()
    assert body.startswith("[reviewers]\n")
    assert load_roster(_write(tmp_path, body)) == default_roster()


def test_scaffold_body_renders_each_reviewers_rerun_flag(tmp_path, monkeypatch):
    # The renderer must honour the map VALUES, not just its keys: a reviewer with
    # rerun=True renders `{ rerun = true }`, one with rerun=False the empty `{}`.
    # (Guards against the renderer silently dropping a future rerun default.)
    monkeypatch.setattr(
        reviewers_config, "DEFAULT_REVIEWERS", {"copilot": True, "codex": False}
    )
    body = reviewers_config.default_reviewers_scaffold_body()
    assert "copilot = { rerun = true }" in body
    assert "codex = {}" in body
    # And it round-trips: loading the rendered body yields the (patched) flags.
    assert tomllib.loads(body)  # syntactically valid TOML
    roster = load_roster(_write(tmp_path, body))
    assert roster.entry("copilot").rerun is True
    assert roster.entry("codex").rerun is False


def test_empty_table_falls_back_to_default(tmp_path):
    # `[reviewers]` with nothing under it is "unset", never "disable all review
    # holds" — removing review enforcement is not a config the loop offers.
    assert load_roster(_write(tmp_path, "[reviewers]\n")) == default_roster()


def test_override_swaps_the_set_with_a_one_line_change(tmp_path):
    # A pilot repo opts into CodeRabbit (or any other set) — only config changed.
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


def test_rerun_defaults_false_when_options_absent(tmp_path):
    # `copilot = {}` with an empty options table means defaults — rerun=False.
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\ncodex = {}\n"))
    assert roster.entry("copilot").rerun is False
    assert roster.entry("codex").rerun is False


# --- the reserved table-level `round_cap` policy key -------------------------


def test_round_cap_defaults_to_unset(tmp_path):
    # Absent → None on the Roster: the engine falls back to its shipped
    # `breakers.ROUND_CAP` default.
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\n"))
    assert roster.round_cap is None
    assert default_roster().round_cap is None


def test_round_cap_is_table_level_policy_not_a_reviewer_entry(tmp_path):
    # `round_cap` rides the `[reviewers]` table but is NOT a reviewer entry:
    # it lands on Roster.round_cap and never reaches the unknown-reviewer
    # validation (which would otherwise reject it loud).
    root = _write(tmp_path, "[reviewers]\nround_cap = 3\ncopilot = {}\n")
    roster = load_roster(root)
    assert roster.round_cap == 3
    assert roster.required_names == ("copilot",)


def test_round_cap_applies_even_with_no_reviewer_entries(tmp_path):
    # Policy without touching the reviewer set: the shipped default required
    # set still applies, WITH the configured cap.
    roster = load_roster(_write(tmp_path, "[reviewers]\nround_cap = 2\n"))
    assert roster.required_names == default_roster().required_names
    assert roster.round_cap == 2


def test_round_cap_rejects_non_int_and_non_positive_values(tmp_path):
    # A bad budget is a loud config error, never a silent default — including
    # `true` (bool is an int subclass; `round_cap = true` is never "1 round").
    for bad in ("0", "-1", "true", '"6"', "2.5"):
        with pytest.raises(RequiredReviewersConfigError, match="round_cap"):
            load_roster(_write(tmp_path, f"[reviewers]\nround_cap = {bad}\n"))


# --- run options (model / instructions / timeout) ride the entry ------------


def test_run_options_are_carried_on_the_entry(tmp_path):
    # `model` / `instructions` / `timeout` land on the reviewer's RosterEntry —
    # the request path reads them off the value, never from a second config read.
    root = _write(
        tmp_path,
        "[reviewers]\n"
        'codex = { rerun = true, model = "pro", instructions = "docs/review.md" }\n',
    )
    entry = load_roster(root).entry("codex")
    assert entry.rerun is True
    assert entry.model == "pro"
    # A relative `instructions` path is anchored to the config's directory (so
    # it opens regardless of cwd), and the entry carries it absolute.
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
    # An empty (or whitespace-only) `model`/`instructions` is a config error at
    # LOAD, not a raw RosterEntry ValueError later. `instructions = ""` is the
    # dangerous one: `Path("").expanduser()` resolves to `.` and joins with the
    # config dir into a NON-empty directory path, so it would slip past
    # RosterEntry's non-empty guard and only blow up as IsADirectoryError on the
    # run path — reject it here, before the path expansion.
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
    # `.shipit.toml` is found by walking UP from cwd; a relative `instructions`
    # path must resolve against the config's dir, not a nested cwd.
    _write(tmp_path, '[reviewers]\ncodex = { instructions = "docs/rev.md" }\n')
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert load_roster().entry("codex").instructions == str(
        tmp_path / "docs" / "rev.md"
    )


def test_absolute_instructions_kept(tmp_path):
    root = _write(tmp_path, '[reviewers]\ncodex = { instructions = "/abs/rev.md" }\n')
    assert load_roster(root).entry("codex").instructions == "/abs/rev.md"


def test_unconfigured_reviewer_reads_all_defaults(tmp_path):
    # Roster.entry is TOTAL: a reviewer outside the table (e.g. a forced
    # `--reviewer codex-local` run) reads the all-defaults entry — not required,
    # review-once, no run options.
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\n"))
    entry = roster.entry("codex")
    assert entry.required is False
    assert entry.rerun is False
    assert (entry.model, entry.instructions, entry.timeout) == (None, None, None)


def test_timeout_reads_and_normalizes(tmp_path):
    # `timeout` is consumed by the run path: a duration string or bare seconds is
    # normalized to the canonical `<N>s` form the backend passes to the agent CLI.
    root = _write(
        tmp_path,
        '[reviewers]\nagy = { timeout = "900s" }\ncodex = { timeout = 1200 }\n',
    )
    roster = load_roster(root)
    assert roster.entry("agy").timeout == "900s"
    assert roster.entry("codex").timeout == "1200s"


def test_timeout_omitted_when_unset(tmp_path):
    # An unset timeout is simply absent (the run path then defaults to 600s).
    root = _write(tmp_path, '[reviewers]\nagy = { model = "pro" }\n')
    assert load_roster(root).entry("agy").timeout is None


def test_timeout_validated_loud_on_bad_input(tmp_path):
    # A non-duration string, a non-positive value, and a boolean all fail loud at
    # LOAD time — a bad timeout is a config error, never a silent default.
    with pytest.raises(RequiredReviewersConfigError, match="timeout"):
        load_roster(_write(tmp_path, '[reviewers]\nagy = { timeout = "soon" }\n'))
    with pytest.raises(RequiredReviewersConfigError, match="positive"):
        load_roster(_write(tmp_path, "[reviewers]\nagy = { timeout = 0 }\n"))
    with pytest.raises(RequiredReviewersConfigError, match="timeout"):
        load_roster(_write(tmp_path, "[reviewers]\nagy = { timeout = true }\n"))


# --- OBS04-WS03: the per-reviewer wait `window` option ----------------------


def test_window_reads_and_normalizes_to_seconds(tmp_path):
    # `window` is the OBS04-WS03 wait window: a duration string or bare seconds,
    # resolved to whole SECONDS on the entry. A reviewer without one carries
    # None (the adapter applies the engine's 20m default).
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


# --- table-only: the list/array form is rejected loud ------------------------


def test_list_array_form_is_rejected_loud(tmp_path):
    # The `[reviewers]` config is TABLE-ONLY. A list/array form
    # (`reviewers = ["copilot", "codex"]`) — a ported release shorthand — must
    # fail loud, not be silently accepted (spec #11 / PRD reviewer-policy).
    with pytest.raises(RequiredReviewersConfigError, match="TABLE"):
        load_roster(_write(tmp_path, 'reviewers = ["copilot", "codex"]\n'))


def test_wrong_typed_reviewers_value_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="must be a TABLE"):
        load_roster(_write(tmp_path, 'reviewers = "copilot"\n'))


# --- reviewer-name key normalization (release#852) ---------------------------


def test_map_keys_are_canonicalized_to_adapter_names(tmp_path):
    # A `Copilot` key must key the entry by the canonical adapter name
    # (`copilot`, lowercase) — the same name the adapters read off the context
    # (`ctx.roster.entry(adapter.name)`). Without this, a `rerun: true` keyed
    # `Copilot` is never applied and head-strict silently degrades to
    # review-once.
    roster = load_roster(_write(tmp_path, "[reviewers]\nCopilot = { rerun = true }\n"))
    assert roster.required_names == ("copilot",)
    assert roster.entry("copilot").rerun is True


def test_window_key_canonicalized_too(tmp_path):
    roster = load_roster(
        _write(tmp_path, '[reviewers]\nCopilot = { window = "120s" }\n')
    )
    assert roster.entry("copilot").window_seconds == 120


def test_map_keys_colliding_after_canonicalization_fail_loud(tmp_path):
    # `Copilot` + `copilot` are byte-distinct keys (so TOML's own duplicate-key
    # rejection misses them) but canonicalize to one adapter — a typo, never two
    # reviewers. It must fail loud, not silently clobber.
    with pytest.raises(RequiredReviewersConfigError, match="duplicate"):
        load_roster(_write(tmp_path, "[reviewers]\nCopilot = {}\ncopilot = {}\n"))


# --- validation (loud, at load) ----------------------------------------------


def test_local_backends_are_requestable_and_can_be_required(tmp_path):
    # codex / agy are requestable local backends, so they are valid in the
    # required set (they post a real review the engine can read as done).
    roster = load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\nagy = {}\n"))
    assert roster.required_names == ("copilot", "agy")


def test_unknown_reviewer_name_fails_loud(tmp_path):
    with pytest.raises(RequiredReviewersConfigError, match="gpt5"):
        load_roster(_write(tmp_path, "[reviewers]\ncopilot = {}\ngpt5 = {}\n"))


def test_non_requestable_reviewer_cannot_be_required(tmp_path):
    # Gemini auto-triggers and has no request mechanism, so it can never satisfy
    # a required (holding) reviewer — configuring it required fails loud at load.
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


# --- the loader seam ----------------------------------------------------------


def test_absent_file_is_the_default_roster(tmp_path):
    assert load_roster(str(tmp_path)) == default_roster()


def test_missing_table_is_the_default_roster(tmp_path):
    # A `.shipit.toml` with no `[reviewers]` table → the default applies.
    root = _write(tmp_path, '[secrets]\nGH_PAT = { env = "X" }\n')
    assert load_roster(root) == default_roster()


def test_loader_searches_up_to_the_repo_root(tmp_path, monkeypatch):
    # The loader walks up from cwd for the repo-root `.shipit.toml`, so a call
    # from a nested subdir still finds the config.
    _write(tmp_path, "[reviewers]\ncodex = { rerun = true }\n")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert load_roster().entry("codex").rerun is True


def test_required_adapters_maps_the_roster_in_config_order(tmp_path):
    root = _write(tmp_path, "[reviewers]\ncoderabbit = {}\ncopilot = {}\n")
    adapters = required_adapters(load_roster(root))
    assert [a.name for a in adapters] == ["coderabbit", "copilot"]
