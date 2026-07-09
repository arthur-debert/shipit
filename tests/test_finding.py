"""Tests for `shipit.finding` — the pure Finding domain (ADR-0044, RVW02).

Table-driven over the module's externally visible behavior: the 4-tier ladder
and its ordering, the merge-block test, the disposition vocabulary, both wire
renderings (Conventional Comments layer + machine marker), lossless
render→parse round-trips, malformed-marker fail-safes, and the severity
precedence chain.
"""

from __future__ import annotations

import pytest

from shipit import finding as fnd
from shipit.finding import (
    CONVENTIONAL_PREFIXES,
    DEFAULT_SEVERITY,
    Disposition,
    Finding,
    Marker,
    Severity,
    order_findings,
    parse_comment,
    parse_marker,
    parse_severity,
    render_comment,
    render_marker,
    resolve_severity,
)

# --- the ladder ---------------------------------------------------------------


def test_ladder_is_the_four_tiers_in_order():
    """The ladder is exactly critical|major|minor|nit, most severe first."""
    assert [s.value for s in Severity] == ["critical", "major", "minor", "nit"]
    assert [s.rank for s in Severity] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("severity", "blocks"),
    [
        (Severity.CRITICAL, True),
        (Severity.MAJOR, True),
        (Severity.MINOR, False),
        (Severity.NIT, False),
    ],
)
def test_merge_block_test_is_the_major_minor_boundary(severity, blocks):
    assert severity.blocks_merge is blocks


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("critical", Severity.CRITICAL),
        ("major", Severity.MAJOR),
        ("minor", Severity.MINOR),
        ("nit", Severity.NIT),
        ("  Major ", Severity.MAJOR),  # tolerant: case + whitespace
        ("NIT", Severity.NIT),
        # The retired presentational triple is NOT the ladder — no mapping.
        ("ERROR", None),
        ("WARNING", None),
        ("INFO", None),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_parse_severity_table(raw, expected):
    assert parse_severity(raw) == expected


def test_dispositions_are_the_four_routes():
    assert {d.value for d in Disposition} == {
        "post",
        "drop-unverified",
        "nit-suppressed",
        "out-of-scope",
    }


# --- ordering ------------------------------------------------------------------


def test_order_findings_is_highest_severity_first_and_stable():
    a = Finding(Severity.NIT, "n1")
    b = Finding(Severity.CRITICAL, "c1")
    c = Finding(Severity.MINOR, "m1")
    d = Finding(Severity.MAJOR, "j1")
    e = Finding(Severity.MAJOR, "j2")  # same tier as d: original order kept
    assert order_findings([a, b, c, d, e]) == [b, d, e, c, a]


# --- the precedence chain -------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "adapter", "override", "expected"),
    [
        # marker wins over adapter
        (Severity.NIT, Severity.CRITICAL, None, Severity.NIT),
        # no marker → adapter mapping
        (None, Severity.MINOR, None, Severity.MINOR),
        # nothing parseable → the major fail-safe (forces a round)
        (None, None, None, Severity.MAJOR),
        # the write-once override beats all three
        (Severity.NIT, Severity.MINOR, Severity.CRITICAL, Severity.CRITICAL),
        (None, None, Severity.NIT, Severity.NIT),
    ],
)
def test_resolve_severity_precedence_chain(marker, adapter, override, expected):
    assert resolve_severity(marker, adapter, override) == expected


def test_default_severity_is_major():
    assert DEFAULT_SEVERITY is Severity.MAJOR


# --- wire rendering: the Conventional Comments human layer ----------------------


@pytest.mark.parametrize(
    ("severity", "prefix"),
    [
        (Severity.CRITICAL, "issue (critical, blocking):"),
        (Severity.MAJOR, "issue (blocking):"),
        (Severity.MINOR, "suggestion (non-blocking):"),
        (Severity.NIT, "nitpick:"),
    ],
)
def test_conventional_comments_prefix_per_tier(severity, prefix):
    assert CONVENTIONAL_PREFIXES[severity] == prefix
    body = render_comment(Finding(severity, "the claim"))
    # marker first (invisible), then the human layer opens with the tier label.
    human = body.split("\n", 1)[1]
    assert human == f"{prefix} the claim"


def test_render_comment_never_carries_the_retired_agent_prefix():
    body = render_comment(Finding(Severity.MAJOR, "claim", category="correctness"))
    assert "Agent:" not in body
    assert "[MAJOR]" not in body


# --- wire rendering: the machine marker -----------------------------------------


def test_marker_carries_the_exact_tuple():
    marker = render_marker(
        Finding(
            Severity.CRITICAL,
            "claim",
            category="cross-file invariants",
            confidence=0.85,
        )
    )
    assert marker.startswith("<!-- shipit:finding ")
    assert marker.endswith("-->")
    assert "severity=critical" in marker
    assert 'category="cross-file invariants"' in marker
    assert "confidence=0.85" in marker


def test_marker_omits_absent_informational_fields():
    marker = render_marker(Finding(Severity.NIT, "claim"))
    assert "category" not in marker
    assert "confidence" not in marker


@pytest.mark.parametrize("severity", list(Severity))
def test_marker_round_trip_per_tier(severity):
    original = Finding(severity, "claim", category="tests", confidence=0.5)
    parsed = parse_marker(render_marker(original))
    assert parsed == Marker(severity=severity, category="tests", confidence=0.5)


@pytest.mark.parametrize(
    "category",
    [
        "cross-file invariants",  # spaces → quoted
        'quo"ted',  # the value delimiter itself
        "a--b",  # illegal inside an HTML comment
        "amp&ersand",  # the escape escape
        "&quot;already&#45;&#45;escaped&amp;",  # escape sequences as literal text
        "",
    ],
)
def test_marker_category_escaping_round_trips_losslessly(category):
    original = Finding(Severity.MINOR, "x", category=category)
    marker_text = render_marker(original)
    assert "--" not in marker_text.replace("-->", "", 1).replace("<!--", "", 1)
    parsed = parse_marker(marker_text)
    assert parsed is not None
    assert parsed.category == category


# --- malformed markers (table-driven fail-safes) --------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # no marker at all → None (nothing machine-readable present)
        ("just a human comment", None),
        ("<!-- some other comment -->", None),
        ("<!-- shipit:findingx severity=nit -->", None),  # tag must match exactly
        # marker present but severity garbage → severity None (→ major fail-safe)
        (
            "<!-- shipit:finding severity=catastrophic -->",
            Marker(severity=None),
        ),
        # the retired triple in a marker is garbage too
        (
            "<!-- shipit:finding severity=ERROR -->",
            Marker(severity=None),
        ),
        # marker with no attrs at all
        ("<!-- shipit:finding -->", Marker(severity=None)),
        # unparseable confidence → dropped, severity survives
        (
            "<!-- shipit:finding severity=nit confidence=high -->",
            Marker(severity=Severity.NIT),
        ),
        # unknown keys are ignored
        (
            "<!-- shipit:finding severity=minor blast_radius=big -->",
            Marker(severity=Severity.MINOR),
        ),
        # duplicate keys: first one wins
        (
            "<!-- shipit:finding severity=nit severity=critical -->",
            Marker(severity=Severity.NIT),
        ),
        # only the FIRST marker counts
        (
            "<!-- shipit:finding severity=minor -->\n"
            "<!-- shipit:finding severity=critical -->",
            Marker(severity=Severity.MINOR),
        ),
        # truncated marker (never closed) → not a marker
        ("<!-- shipit:finding severity=nit", None),
    ],
)
def test_parse_marker_malformed_table(body, expected):
    assert parse_marker(body) == expected


# --- the full comment round trip -------------------------------------------------


@pytest.mark.parametrize(
    "original",
    [
        Finding(Severity.CRITICAL, "SQL injection via f-string", file="db.py", line=3),
        Finding(
            Severity.MAJOR,
            "off-by-one drops the last hunk",
            file="diff.py",
            line=88,
            category="correctness",
            confidence=0.9,
            evidence="for i in range(len(hunks) - 1):",
            fix="iterate over hunks directly",
        ),
        Finding(
            Severity.MINOR,
            "multi-line claim\nspanning two lines",
            category="tests",
            confidence=0.25,
            evidence="assert x\nassert y",
        ),
        Finding(Severity.NIT, "naming: prefer snake_case", fix="rename to foo_bar"),
        # unicode text and evidence
        Finding(Severity.MAJOR, "café ≠ cafe", evidence="s = 'café'"),
    ],
)
def test_render_parse_round_trip_is_lossless(original):
    """render_comment → parse_comment recovers the EXACT finding (location is a
    thread property, so it is passed back in)."""
    body = render_comment(original)
    recovered = parse_comment(body, file=original.file, line=original.line)
    assert recovered == original


def test_parse_comment_recovers_exact_severity_from_body_alone():
    """The acceptance criterion: severity survives GitHub via the body alone."""
    for severity in Severity:
        body = render_comment(Finding(severity, "claim"))
        assert parse_comment(body).severity == severity


def test_parse_comment_without_marker_fails_safe_to_major():
    """An unparseable finding defaults to major: it forces a round rather than
    slipping past the Breaker."""
    parsed = parse_comment("nitpick: no marker rode this comment")
    assert parsed.severity is Severity.MAJOR
    assert parsed.text == "no marker rode this comment"


def test_parse_comment_with_garbage_marker_fails_safe_to_major():
    parsed = parse_comment(
        "<!-- shipit:finding severity=wat -->\nissue (blocking): claim"
    )
    assert parsed.severity is Severity.MAJOR
    assert parsed.text == "claim"


def test_finding_is_frozen():
    found = Finding(Severity.NIT, "x")
    with pytest.raises(AttributeError):
        found.severity = Severity.CRITICAL  # type: ignore[misc]


def test_module_is_pure():
    """The domain module does no I/O: `re` is its ONLY module import — no
    os/subprocess/network, no shipit adapter (gh/git) rides it."""
    import types

    imported = {
        name for name, value in vars(fnd).items() if isinstance(value, types.ModuleType)
    }
    assert imported == {"re"}
