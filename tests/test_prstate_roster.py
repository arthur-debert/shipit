"""Roster / RosterEntry — construction IS validation (CLI01-WS04).

The Roster is reviewer configuration as ONE frozen value: a
RosterEntry/Roster that constructs is well-formed, so everything downstream
(the engine, the adapters, the request path) reads settings off it without
re-checking. Config-SHAPE errors (unknown reviewer, wrong-typed option) are
the loader's job and are proven in test_prstate_reviewers_config.py; here we
prove the values defend their own invariants and expose the deep, total read
surface (`entry` never returns None).
"""

from __future__ import annotations

import dataclasses

import pytest
from shipit.prstate.errors import PrStateError
from shipit.prstate.reviewers import required_adapters
from shipit.prstate.roster import Roster, RosterEntry


# --- RosterEntry: construction is validation ---------------------------------


def test_entry_defaults_are_the_shipped_defaults():
    entry = RosterEntry(name="copilot")
    assert entry.required is False
    assert entry.rerun is False
    assert entry.window_seconds is None
    assert (entry.model, entry.instructions, entry.timeout) == (None, None, None)


def test_entry_is_frozen():
    entry = RosterEntry(name="copilot")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.rerun = True  # type: ignore[misc]


def test_entry_name_must_be_canonical_lowercase():
    with pytest.raises(ValueError, match="lowercase"):
        RosterEntry(name="Copilot")


def test_entry_name_must_be_non_empty():
    with pytest.raises(ValueError, match="non-empty"):
        RosterEntry(name="")


def test_entry_flags_must_be_bools():
    with pytest.raises(ValueError, match="rerun"):
        RosterEntry(name="copilot", rerun="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="required"):
        RosterEntry(name="copilot", required=1)  # type: ignore[arg-type]


def test_entry_window_must_be_positive_seconds():
    assert RosterEntry(name="copilot", window_seconds=600).window_seconds == 600
    with pytest.raises(ValueError, match="window_seconds"):
        RosterEntry(name="copilot", window_seconds=0)
    with pytest.raises(ValueError, match="window_seconds"):
        RosterEntry(name="copilot", window_seconds=-5)
    with pytest.raises(ValueError, match="window_seconds"):
        # bool is an int subclass — `window_seconds=True` is never "1 second".
        RosterEntry(name="copilot", window_seconds=True)


def test_entry_timeout_must_be_canonical_duration():
    assert RosterEntry(name="codex", timeout="600s").timeout == "600s"
    for bad in ("soon", "600", "0s", 600):
        with pytest.raises(ValueError, match="timeout"):
            RosterEntry(name="codex", timeout=bad)  # type: ignore[arg-type]


def test_entry_run_strings_must_be_non_empty():
    with pytest.raises(ValueError, match="model"):
        RosterEntry(name="codex", model="")
    with pytest.raises(ValueError, match="instructions"):
        RosterEntry(name="codex", instructions="")


# --- Roster: one value, total reads ------------------------------------------


def test_empty_roster_is_the_honest_fixture_default():
    roster = Roster()
    assert roster.entries == ()
    assert roster.required_names == ()
    # entry() is TOTAL: an unconfigured reviewer reads the all-defaults entry.
    assert roster.entry("copilot") == RosterEntry(name="copilot")


def test_roster_entry_matches_canonical_lowercase():
    roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    # A caller holding a differently-cased name still reads the same entry —
    # the same normalization the loader applies to config keys.
    assert roster.entry("Copilot").rerun is True


def test_roster_required_preserves_config_order():
    roster = Roster(
        (
            RosterEntry(name="coderabbit", required=True),
            RosterEntry(name="gemini"),  # configured but not required
            RosterEntry(name="copilot", required=True),
        )
    )
    assert roster.required_names == ("coderabbit", "copilot")
    assert [e.name for e in roster.required] == ["coderabbit", "copilot"]


def test_roster_rejects_duplicate_entries():
    with pytest.raises(ValueError, match="duplicate"):
        Roster((RosterEntry(name="copilot"), RosterEntry(name="copilot")))


def test_roster_entries_must_be_a_tuple_of_entries():
    with pytest.raises(ValueError, match="tuple of RosterEntry"):
        Roster([RosterEntry(name="copilot")])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tuple of RosterEntry"):
        Roster(({"name": "copilot"},))  # type: ignore[arg-type]


def test_roster_round_cap_defaults_to_none_meaning_shipped_default():
    # None → the engine's shipped default (breakers.ROUND_CAP); the roster only
    # carries an override, so the breaker rule keeps owning its own constant.
    assert Roster().round_cap is None
    assert Roster(round_cap=3).round_cap == 3


def test_roster_round_cap_must_be_a_positive_int():
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError, match="round_cap"):
            Roster(round_cap=bad)  # type: ignore[arg-type]


def test_roster_poll_interval_defaults_to_none_meaning_shipped_default():
    # None → the waiter's shipped default (wait.POLL_INTERVAL_SECONDS, 60s);
    # the roster only carries an override (ADR-0034), same convention as
    # round_cap.
    assert Roster().poll_interval is None
    assert Roster(poll_interval=30).poll_interval == 30


def test_roster_poll_interval_must_be_a_positive_int():
    for bad in (0, -1, True, "60s"):
        with pytest.raises(ValueError, match="poll_interval"):
            Roster(poll_interval=bad)  # type: ignore[arg-type]


# --- required_adapters: the roster→registry mapping --------------------------


def test_required_adapters_maps_names_in_order():
    roster = Roster(
        (
            RosterEntry(name="coderabbit", required=True),
            RosterEntry(name="copilot", required=True),
        )
    )
    assert [a.name for a in required_adapters(roster)] == ["coderabbit", "copilot"]


def test_required_adapters_fails_loud_on_an_unmapped_name():
    # Unreachable through `load_roster` (it validates names against the
    # registry) — a hand-built roster with a name no adapter claims must fail
    # loud rather than leak a None into the engine.
    roster = Roster((RosterEntry(name="gpt5", required=True),))
    with pytest.raises(PrStateError, match="gpt5"):
        required_adapters(roster)
