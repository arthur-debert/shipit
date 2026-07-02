"""Unit tests for the `Sha` value object in ISOLATION (COR02, issue #251).

The commit-identity type in the core-identities family: validated FULL hex,
lowercase-normalized at construction, equality full-vs-full only — a silent
prefix-against-full comparison is impossible (a prefix cannot BE a `Sha`, and a
raw string refuses to compare); prefix matching is the explicit
`matches_prefix()` ask. The retired ad-hoc validity checks live in the
constructor, so rejection is loud, at the boundary.
"""

from __future__ import annotations

import dataclasses

import pytest
from shipit.identity import Sha

SHA1 = "0123456789abcdef0123456789abcdef01234567"  # 40 hex
SHA256 = "a" * 64  # 64 hex


# --- construction ---------------------------------------------------------


def test_accepts_a_full_sha1():
    assert Sha(SHA1).value == SHA1


def test_accepts_a_full_sha256():
    assert Sha(SHA256).value == SHA256


def test_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Sha(SHA1).value = SHA256  # type: ignore[misc]


def test_str_is_the_normalized_value():
    assert str(Sha(SHA1.upper())) == SHA1


# --- normalization --------------------------------------------------------


def test_lowercase_normalizes():
    assert Sha(SHA1.upper()).value == SHA1


def test_strips_whitespace():
    assert Sha(f"  {SHA1}\n").value == SHA1


def test_case_variants_are_one_identity():
    assert Sha(SHA1.upper()) == Sha(SHA1)
    assert hash(Sha(SHA1.upper())) == hash(Sha(SHA1))


# --- rejection ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "deadbeef",  # abbreviated — a prefix cannot BE a Sha
        SHA1[:39],  # one short of full
        SHA1 + "0",  # one past SHA-1, far from SHA-256
        "g" * 40,  # right length, not hex
        "deadbeef deadbeef deadbeef deadbeef dead",  # inner whitespace
        None,  # not a str
        40,  # not a str
    ],
)
def test_rejects_non_full_sha_values(bad):
    with pytest.raises(ValueError):
        Sha(bad)


# --- equality -------------------------------------------------------------


def test_equal_full_shas_compare_equal():
    assert Sha(SHA1) == Sha(SHA1)
    assert Sha(SHA1) != Sha(SHA256)


def test_comparing_against_a_raw_str_refuses_loudly():
    # The staleness-flipping bug: `full_sha == "short-or-case-varying-string"`
    # silently answering False. A raw-string comparison raises instead.
    with pytest.raises(TypeError):
        Sha(SHA1) == SHA1  # noqa: B015 - the comparison itself is the assertion
    with pytest.raises(TypeError):
        Sha(SHA1) != "deadbeef"  # noqa: B015


def test_comparing_against_none_is_false_not_an_error():
    # `None` means honestly-unknown (a review node with no commit) — a routine
    # state, not a type confusion.
    assert Sha(SHA1) != None  # noqa: E711 - the reflected comparison is the point


def test_dict_probe_with_a_raw_str_fails_loud_not_silent_miss():
    # __hash__ matches hash(value) ON PURPOSE: a str probe of a Sha-keyed dict
    # lands in the same bucket and then refuses in __eq__ — never a silent miss.
    d = {Sha(SHA1): "x"}
    with pytest.raises(TypeError):
        d.get(SHA1)


# --- prefix matching ------------------------------------------------------


def test_matches_prefix_is_the_explicit_ask():
    assert Sha(SHA1).matches_prefix(SHA1[:12]) is True
    assert Sha(SHA1).matches_prefix("beef") is False


def test_matches_prefix_normalizes_case():
    assert Sha(SHA1).matches_prefix(SHA1[:8].upper()) is True


def test_matches_prefix_accepts_the_full_sha_itself():
    assert Sha(SHA1).matches_prefix(SHA1) is True


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "abc",  # below git's 4-char abbreviation floor
        "xyz4",  # not hex
        SHA1 + "0",  # longer than the sha it would abbreviate
    ],
)
def test_matches_prefix_rejects_unusable_prefixes(bad):
    with pytest.raises(ValueError):
        Sha(SHA1).matches_prefix(bad)
