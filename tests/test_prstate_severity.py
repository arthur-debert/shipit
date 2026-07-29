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
    return render_marker(Finding(severity=severity, text="x"))


GEMINI_BADGE = "![{level}](https://www.gstatic.com/codereviewagent/{level}.svg)"


CHAIN_CASES = [
    (marker(Severity.NIT) + "\nnitpick: wording", "Copilot", {}, Severity.NIT, MARKER),
    (
        GEMINI_BADGE.format(level="high") + " off-by-one here",
        "gemini-code-assist[bot]",
        {},
        Severity.MAJOR,
        ADAPTER,
    ),
    ("please rename this variable", "Copilot", {}, Severity.MINOR, POLICY),
    ("anything at all", "some-human", {}, Severity.MAJOR, DEFAULT),
    (
        marker(Severity.NIT) + "\n" + GEMINI_BADGE.format(level="critical"),
        "gemini-code-assist[bot]",
        {},
        Severity.NIT,
        MARKER,
    ),
    (
        "<!-- shipit:finding severity=warning -->\nissue: retired vocabulary",
        "Copilot",
        {},
        Severity.MINOR,
        POLICY,
    ),
    (
        "<!-- shipit:finding severity=warning -->\nissue: retired vocabulary",
        "some-human",
        {},
        Severity.MAJOR,
        DEFAULT,
    ),
    (
        "<!-- shipit:finding severity=warning -->\n" + GEMINI_BADGE.format(level="low"),
        "gemini-code-assist[bot]",
        {},
        Severity.NIT,
        ADAPTER,
    ),
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
    ("unparseable", "Copilot", {1: Severity.CRITICAL}, Severity.CRITICAL, OVERRIDE),
    ("unparseable", "some-human", {1: Severity.MINOR}, Severity.MINOR, OVERRIDE),
]


@pytest.mark.parametrize("body, author, overrides, expected, source", CHAIN_CASES)
def test_precedence_chain(body, author, overrides, expected, source):
    resolution = resolve_finding_severity(comment(body, author), overrides)
    assert resolution.severity is expected
    assert resolution.source == source
    assert finding_severity(comment(body, author), overrides) is expected


def test_override_keys_on_the_comment_id():
    body = marker(Severity.NIT) + "\nnitpick: x"
    resolution = resolve_finding_severity(comment(body, cid=7), {8: Severity.CRITICAL})
    assert resolution.severity is Severity.NIT
    assert resolution.source == MARKER


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
    adapter = by_name("gemini")
    assert adapter.native_severity(GEMINI_BADGE.format(level=level)) is expected
    assert adapter.native_severity(GEMINI_BADGE.format(level=level.upper())) is expected


def test_gemini_unmappable_is_none():
    adapter = by_name("gemini")
    assert adapter.native_severity("no badge at all") is None
    assert adapter.native_severity("![unknown](https://x/unknown.svg)") is None


def test_gemini_badge_requires_geminis_own_asset_url():
    adapter = by_name("gemini")
    assert (
        adapter.native_severity("![critical](https://example.com/critical.svg)") is None
    )
    assert adapter.native_severity("![high](https://cdn.example.com/high.png)") is None
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
    assert by_name("coderabbit").native_severity(token) is expected


def test_coderabbit_unmappable_is_none():
    assert by_name("coderabbit").native_severity("plain prose") is None


def test_copilot_has_no_native_vocabulary():
    assert by_name("copilot").native_severity("anything") is None


def test_local_reviewers_ride_the_marker_not_an_adapter_mapping():
    for name in ("codex", "agy"):
        assert by_name(name).native_severity("nitpick: x") is None


def test_every_registry_adapter_answers_the_severity_seam():
    for adapter in REGISTRY:
        assert adapter.native_severity("plain prose") in (None, *Severity)


def test_copilot_unclassified_policy_is_minor():
    assert by_name("copilot").unclassified_severity is Severity.MINOR


def test_every_other_adapter_declares_no_unclassified_policy():
    for adapter in REGISTRY:
        if adapter.name == "copilot":
            continue
        assert adapter.unclassified_severity is None
