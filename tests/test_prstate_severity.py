"""The engine's finding-severity read (ADR-0044): precedence chain + adapters.

Table-driven pins on the two seams RVW02-WS02 adds: the engine-side precedence
chain (`prstate.severity.resolve_finding_severity` — machine marker →
reviewer-adapter mapping → the adapter's unclassified-severity policy (#743) →
`major` fail-safe, beaten only by a write-once override) and the per-adapter
native-format mappings in the reviewer registry (each app reviewer's adapter
owns mapping its native severity vocabulary to the shared 4-tier ladder — and,
for a reviewer with no vocabulary at all, its explicit unclassified policy).
"""

from __future__ import annotations

import pytest

from shipit.finding import Finding, Severity, render_marker
from shipit.prstate.model import ReviewComment
from shipit.prstate.reviewers import REGISTRY, by_name
from shipit.prstate.severity import (
    ADAPTER,
    DEFAULT,
    MARKER,
    OVERRIDE,
    POLICY,
    finding_severity,
    resolve_finding_severity,
)


def comment(body: str, author: str = "Copilot", cid: int = 1) -> ReviewComment:
    return ReviewComment(comment_id=cid, path="a.py", line=1, body=body, author=author)


def marker(severity: Severity) -> str:
    """A WS01 wire-format marker line for `severity` (the domain's renderer —
    the same bytes a local reviewer posts)."""
    return render_marker(Finding(severity=severity, text="x"))


GEMINI_BADGE = "![{level}](https://www.gstatic.com/codereviewagent/{level}.svg)"


# --- the precedence chain, table-driven (ADR-0044) ---------------------------
# Columns: body, author, overrides -> (severity, source). One row per rung,
# plus the beats-relations between rungs.

CHAIN_CASES = [
    # marker present → marker wins (no adapter, no override)
    (marker(Severity.NIT) + "\nnitpick: wording", "Copilot", {}, Severity.NIT, MARKER),
    # no marker, author's adapter maps its native format → adapter rung
    (
        GEMINI_BADGE.format(level="high") + " off-by-one here",
        "gemini-code-assist[bot]",
        {},
        Severity.MAJOR,
        ADAPTER,
    ),
    # no marker, no native vocabulary (Copilot) → the adapter's unclassified
    # policy (#743): minor, so a Copilot-only nit round can fire the Breaker
    ("please rename this variable", "Copilot", {}, Severity.MINOR, POLICY),
    # unknown author (no adapter matches, so no policy) → the `major` fail-safe
    ("anything at all", "some-human", {}, Severity.MAJOR, DEFAULT),
    # a marker BEATS the author's adapter mapping (marker is the stronger rung)
    (
        marker(Severity.NIT) + "\n" + GEMINI_BADGE.format(level="critical"),
        "gemini-code-assist[bot]",
        {},
        Severity.NIT,
        MARKER,
    ),
    # a MALFORMED marker parses to no severity → falls through the chain (for
    # Copilot: no native mapping either, so its unclassified policy decides)
    (
        "<!-- shipit:finding severity=warning -->\nissue: retired vocabulary",
        "Copilot",
        {},
        Severity.MINOR,
        POLICY,
    ),
    # ...and all the way to the `major` fail-safe for a no-adapter author
    (
        "<!-- shipit:finding severity=warning -->\nissue: retired vocabulary",
        "some-human",
        {},
        Severity.MAJOR,
        DEFAULT,
    ),
    # ...but falls through to the ADAPTER rung when the author has one
    (
        "<!-- shipit:finding severity=warning -->\n" + GEMINI_BADGE.format(level="low"),
        "gemini-code-assist[bot]",
        {},
        Severity.NIT,
        ADAPTER,
    ),
    # a write-once override beats EVERYTHING — marker included, both directions
    (
        marker(Severity.CRITICAL) + "\nissue (critical, blocking): x",
        "Copilot",
        {1: Severity.NIT},
        Severity.NIT,
        OVERRIDE,
    ),
    (
        marker(Severity.NIT) + "\nnitpick: x",
        "Copilot",
        {1: Severity.CRITICAL},
        Severity.CRITICAL,
        OVERRIDE,
    ),
    # override beats the policy rung as well (Copilot's policy would say minor)
    ("unparseable", "Copilot", {1: Severity.CRITICAL}, Severity.CRITICAL, OVERRIDE),
    # ...and the fail-safe (no-adapter author)
    ("unparseable", "some-human", {1: Severity.MINOR}, Severity.MINOR, OVERRIDE),
]


@pytest.mark.parametrize("body, author, overrides, expected, source", CHAIN_CASES)
def test_precedence_chain(body, author, overrides, expected, source):
    resolution = resolve_finding_severity(comment(body, author), overrides)
    assert resolution.severity is expected
    assert resolution.source == source
    # the convenience read agrees with the full resolution
    assert finding_severity(comment(body, author), overrides) is expected


def test_override_keys_on_the_comment_id():
    # An override for a DIFFERENT comment id is not this finding's override.
    body = marker(Severity.NIT) + "\nnitpick: x"
    resolution = resolve_finding_severity(comment(body, cid=7), {8: Severity.CRITICAL})
    assert resolution.severity is Severity.NIT
    assert resolution.source == MARKER


# --- per-adapter native-format mappings (the registry owns them) -------------


@pytest.mark.parametrize(
    "level, expected",
    [
        ("critical", Severity.CRITICAL),
        ("high", Severity.MAJOR),
        ("medium", Severity.MINOR),
        ("low", Severity.NIT),
    ],
)
def test_gemini_maps_its_badge_levels(level, expected):
    # Gemini Code Assist's Critical/High/Medium/Low rides each comment as a
    # severity badge image whose alt text is the native token.
    adapter = by_name("gemini")
    assert adapter.native_severity(GEMINI_BADGE.format(level=level)) is expected
    # case-insensitive: the alt text may render capitalized
    assert adapter.native_severity(GEMINI_BADGE.format(level=level.upper())) is expected


def test_gemini_unmappable_is_none():
    adapter = by_name("gemini")
    assert adapter.native_severity("no badge at all") is None
    assert adapter.native_severity("![unknown](https://x/unknown.svg)") is None


def test_gemini_badge_requires_geminis_own_asset_url():
    # An unrelated image (or a quoted example) whose alt text merely happens to
    # be a level token is NOT a Gemini badge — only the `codereviewagent/` asset
    # is, so it must not skew severity resolution.
    adapter = by_name("gemini")
    assert (
        adapter.native_severity("![critical](https://example.com/critical.svg)") is None
    )
    assert adapter.native_severity("![high](https://cdn.example.com/high.png)") is None
    # ...but the real badge URL shape (`<level>-priority.svg`) still maps.
    live = "![high](https://www.gstatic.com/codereviewagent/high-priority.svg)"
    assert adapter.native_severity(live) is Severity.MAJOR


@pytest.mark.parametrize(
    "token, expected",
    [
        ("_⚠️ Potential issue_ | _🔴 Critical_", Severity.CRITICAL),
        ("_⚠️ Potential issue_ | _🟠 Major_", Severity.MAJOR),
        ("_🛠️ Refactor suggestion_ | _🟡 Minor_", Severity.MINOR),
        ("_⚠️ Potential issue_\n\nthe claim", Severity.MAJOR),
        ("_🛠️ Refactor suggestion_\n\nthe claim", Severity.MINOR),
        ("_🧹 Nitpick_\n\nthe claim", Severity.NIT),
    ],
)
def test_coderabbit_maps_its_markers(token, expected):
    # CodeRabbit's explicit severity pill wins over the kind marker riding the
    # same comment (declaration order is precedence); a kind marker alone maps
    # by kind.
    assert by_name("coderabbit").native_severity(token) is expected


def test_coderabbit_unmappable_is_none():
    assert by_name("coderabbit").native_severity("plain prose") is None


def test_copilot_has_no_native_vocabulary():
    # Deliberate (ADR-0044): Copilot emits no severity anywhere — bodies and
    # API metadata alike (#743) — so its adapter maps nothing; an unmarked
    # Copilot finding resolves through the unclassified policy instead.
    assert by_name("copilot").native_severity("anything") is None


def test_local_reviewers_ride_the_marker_not_an_adapter_mapping():
    # codex/agy post the WS01 two-layer format: the machine marker (the
    # chain's stronger rung) carries their severity, so their adapters add no
    # native mapping of their own.
    for name in ("codex", "agy"):
        assert by_name(name).native_severity("nitpick: x") is None


def test_every_registry_adapter_answers_the_severity_seam():
    # The engine asks EVERY adapter the same question — the method is part of
    # the adapter interface, never a name-branch.
    for adapter in REGISTRY:
        assert adapter.native_severity("plain prose") in (None, *Severity)


# --- the unclassified-severity policy rung (#743) ----------------------------


def test_copilot_unclassified_policy_is_minor():
    # The #743 fix: Copilot's unclassified findings resolve `minor` — the
    # thread still resolves before Ready, but a Copilot-only round of them can
    # fire the no-major-finding Breaker instead of riding to the round cap.
    assert by_name("copilot").unclassified_severity is Severity.MINOR


def test_every_other_adapter_declares_no_unclassified_policy():
    # The `major` fail-safe stays the rule: only Copilot (no severity
    # vocabulary at all) declares a policy. A reviewer WITH a native format
    # (coderabbit, gemini) or a machine marker (codex, agy) whose finding
    # nevertheless fails to parse should still force a round.
    for adapter in REGISTRY:
        if adapter.name == "copilot":
            continue
        assert adapter.unclassified_severity is None
