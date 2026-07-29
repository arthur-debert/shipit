from __future__ import annotations

import dataclasses

import pytest

from shipit.identity import Sha

SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "a" * 64


def test_accepts_a_full_sha1():
    assert Sha(SHA1).value == SHA1


def test_accepts_a_full_sha256():
    assert Sha(SHA256).value == SHA256


def test_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Sha(SHA1).value = SHA256  # type: ignore[misc]


def test_str_is_the_normalized_value():
    assert str(Sha(SHA1.upper())) == SHA1


def test_lowercase_normalizes():
    assert Sha(SHA1.upper()).value == SHA1


def test_strips_whitespace():
    assert Sha(f"  {SHA1}\n").value == SHA1


def test_case_variants_are_one_identity():
    assert Sha(SHA1.upper()) == Sha(SHA1)
    assert hash(Sha(SHA1.upper())) == hash(Sha(SHA1))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "deadbeef",
        SHA1[:39],
        SHA1 + "0",
        "g" * 40,
        "deadbeef deadbeef deadbeef deadbeef dead",
        None,
        40,
    ],
)
def test_rejects_non_full_sha_values(bad):
    with pytest.raises(ValueError):
        Sha(bad)


def test_equal_full_shas_compare_equal():
    assert Sha(SHA1) == Sha(SHA1)
    assert Sha(SHA1) != Sha(SHA256)


def test_comparing_against_a_raw_str_refuses_loudly():
    with pytest.raises(TypeError):
        Sha(SHA1) == SHA1  # noqa: B015 - the comparison itself is the assertion
    with pytest.raises(TypeError):
        Sha(SHA1) != "deadbeef"  # noqa: B015


def test_comparing_against_none_is_false_not_an_error():
    assert Sha(SHA1) != None  # noqa: E711 - the reflected comparison is the point


def test_dict_probe_with_a_raw_str_fails_loud_not_silent_miss():
    d = {Sha(SHA1): "x"}
    with pytest.raises(TypeError):
        d.get(SHA1)


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
        "",
        "abc",
        "xyz4",
        SHA1 + "0",
    ],
)
def test_matches_prefix_rejects_unusable_prefixes(bad):
    with pytest.raises(ValueError):
        Sha(SHA1).matches_prefix(bad)
