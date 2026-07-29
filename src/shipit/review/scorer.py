"""Recall, false positives, and unadjudicated emissions per Variant, scored
deterministically over banked Review-round records.

See docs/adr/0048-ground-truth-fixture-deterministic-scorer.md.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..finding import Severity
from .groundtruth import Fixture, Label, PinnedRange
from .match import Claim, MatchVerdict, match_claim

__all__ = [
    "UNDERPOWERED_FLOOR",
    "Adjudication",
    "ScoreReport",
    "TierScore",
    "VariantScore",
    "render_report",
    "score_records",
]

#: Below this many positives a tier is statistically underpowered.
UNDERPOWERED_FLOOR = 20

_NO_VARIANT = "(none)"


@dataclass(frozen=True)
class TierScore:
    severity: Severity
    positives: int
    recalled: int

    @property
    def underpowered(self) -> bool:
        return self.positives < UNDERPOWERED_FLOOR


@dataclass(frozen=True)
class Adjudication:
    kind: str
    variant: str
    pr_id: str
    file: str
    line: int | None
    severity: str
    text: str
    label_id: str | None = None


@dataclass(frozen=True)
class VariantScore:
    variant: str
    rounds: int
    pr_ids: tuple[str, ...]
    tiers: tuple[TierScore, ...]
    recalled_label_ids: tuple[str, ...]
    false_positives: tuple[Adjudication, ...]
    unadjudicated: tuple[Adjudication, ...]
    near_misses: tuple[Adjudication, ...]


@dataclass(frozen=True)
class ScoreReport:
    fixture_version: int
    variants: tuple[VariantScore, ...]
    confirmed_labels: int
    candidate_labels: int
    records_seen: int
    records_scored: int


def _sha_matches(recorded: str, pinned: str) -> bool:
    a, b = recorded.strip().lower(), pinned.strip().lower()
    if len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def _pin_for(fixture: Fixture, record: Mapping[str, Any]) -> PinnedRange | None:
    repo = record.get("round.repo")
    rng = record.get("round.range")
    if not isinstance(repo, str) or not isinstance(rng, Mapping):
        return None
    base, head = rng.get("base"), rng.get("head")
    if not isinstance(base, str) or not isinstance(head, str):
        return None
    for pin in fixture.prs:
        if (
            pin.repo.lower() == repo.lower()
            and _sha_matches(base, pin.base_sha)
            and _sha_matches(head, pin.head_sha)
        ):
            return pin
    return None


def _variant_key(record: Mapping[str, Any]) -> str:
    variant = record.get("round.variant")
    if not isinstance(variant, Mapping):
        return _NO_VARIANT
    content_hash = variant.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        return _NO_VARIANT
    label = variant.get("label")
    return f"{content_hash} [{label}]" if label is not None else content_hash


def _posted_findings(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    findings = record.get("round.findings")
    if not isinstance(findings, Sequence):
        return []
    return [
        f
        for f in findings
        if isinstance(f, Mapping)
        and f.get("disposition") == "post"
        and f.get("duplicate_of") is None
    ]


def _claim_of(finding: Mapping[str, Any]) -> Claim:
    line = finding.get("line")
    return Claim(
        file=str(finding.get("file") or ""),
        line=line if isinstance(line, int) else None,
        text=str(finding.get("text") or ""),
    )


def _adjudication(
    kind: str,
    variant: str,
    pin: PinnedRange,
    finding: Mapping[str, Any],
    label_id: str | None = None,
) -> Adjudication:
    claim = _claim_of(finding)
    return Adjudication(
        kind=kind,
        variant=variant,
        pr_id=pin.id,
        file=claim.file,
        line=claim.line,
        severity=str(finding.get("severity") or ""),
        text=claim.text,
        label_id=label_id,
    )


def score_records(
    fixture: Fixture, records: Sequence[Mapping[str, Any]]
) -> ScoreReport:
    joined: dict[str, dict[str, Any]] = {}
    records_scored = 0
    for record in records:
        pin = _pin_for(fixture, record)
        if pin is None:
            continue
        records_scored += 1
        variant = _variant_key(record)
        state = joined.setdefault(
            variant,
            {
                "rounds": 0,
                "pins": {},
                "recalled": set(),
                "fps": [],
                "unadj": [],
                "near": [],
            },
        )
        state["rounds"] += 1
        state["pins"][pin.id] = pin
        labels = fixture.labels_for(pin.id)  # confirmed only
        for finding in _posted_findings(record):
            claim = _claim_of(finding)
            matched: Label | None = None
            near: Label | None = None
            for label in labels:
                verdict = match_claim(
                    claim, file=label.file, lines=label.lines, texts=label.texts
                )
                if verdict is MatchVerdict.MATCH:
                    matched = label
                    break
                if verdict is MatchVerdict.NEAR_MISS and near is None:
                    near = label
            if matched is not None:
                if matched.verdict == "real":
                    state["recalled"].add(matched.id)
                else:
                    state["fps"].append(
                        _adjudication(
                            "false-positive", variant, pin, finding, matched.id
                        )
                    )
            elif near is not None:
                state["near"].append(
                    _adjudication("near-miss", variant, pin, finding, near.id)
                )
            else:
                state["unadj"].append(_adjudication("unmatched", variant, pin, finding))

    variants: list[VariantScore] = []
    for variant in sorted(joined):
        state = joined[variant]
        pin_ids = sorted(state["pins"])
        in_scope = [
            label
            for pid in pin_ids
            for label in fixture.labels_for(pid)
            if label.verdict == "real"
        ]
        tiers = []
        for severity in Severity:
            tier_labels = [lb for lb in in_scope if lb.severity is severity]
            defects: dict[str, bool] = {}
            for lb in tier_labels:
                key = lb.defect_key
                defects[key] = defects.get(key, False) or lb.id in state["recalled"]
            tiers.append(
                TierScore(
                    severity=severity,
                    positives=len(defects),
                    recalled=sum(defects.values()),
                )
            )
        variants.append(
            VariantScore(
                variant=variant,
                rounds=state["rounds"],
                pr_ids=tuple(pin_ids),
                tiers=tuple(tiers),
                recalled_label_ids=tuple(sorted(state["recalled"])),
                false_positives=tuple(state["fps"]),
                unadjudicated=tuple(state["unadj"]),
                near_misses=tuple(state["near"]),
            )
        )
    return ScoreReport(
        fixture_version=fixture.version,
        variants=tuple(variants),
        confirmed_labels=sum(1 for lb in fixture.labels if lb.confirmed),
        candidate_labels=sum(1 for lb in fixture.labels if not lb.confirmed),
        records_seen=len(records),
        records_scored=records_scored,
    )


#: Adjudication text is model-generated: an escape could forge report structure.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    return _CONTROL_CHARS.sub("·", text)


def _tier_line(tier: TierScore) -> str:
    marker = "  [UNDERPOWERED]" if tier.underpowered else ""
    pct = f" ({tier.recalled / tier.positives:.0%})" if tier.positives else ""
    return f"    {tier.severity.value:<8}  recall {tier.recalled}/{tier.positives}{pct}{marker}"


def _adjudication_lines(items: Sequence[Adjudication]) -> list[str]:
    out = []
    for item in items:
        pr_id = _sanitize(item.pr_id)
        file = _sanitize(item.file)
        loc = f"{file}:{item.line}" if item.line is not None else file
        severity = _sanitize(item.severity) or "?"
        label_id = _sanitize(item.label_id or "")
        head = f"    [{pr_id}] {loc} ({severity}) — {_sanitize(item.text)}"
        out.append(head)
        if item.kind == "near-miss":
            # The id is fixture-supplied: `shlex.quote` keeps it one shell
            # token and `--` keeps Click from reading it as an option.
            out.append(
                f"      ↳ near-missed label {label_id!r} — if same defect:"
                f" shipit eval bank alias --text <phrasing> -- {shlex.quote(label_id)}"
            )
        elif item.kind == "unmatched":
            out.append(
                "      ↳ unknown to the corpus — adjudicate once, then:"
                " shipit eval bank label … --verdict real|not-real"
                " (--defect <family> if it re-anchors a banked defect)"
            )
        elif item.kind == "false-positive":
            out.append(f"      ↳ matched banked not-real label {label_id!r}")
    return out


def render_report(report: ScoreReport) -> str:
    lines = [
        f"ground-truth fixture v{report.fixture_version} — "
        f"{report.confirmed_labels} confirmed labels"
        + (
            f" ({report.candidate_labels} candidates pending confirmation, excluded)"
            if report.candidate_labels
            else ""
        ),
        f"records: {report.records_scored}/{report.records_seen} joined the fixture",
    ]
    if not report.variants:
        lines.append("no in-fixture review-round records — nothing to score")
        return "\n".join(lines) + "\n"
    for vs in report.variants:
        lines += [
            "",
            f"variant {_sanitize(vs.variant)}  "
            f"({vs.rounds} round(s) over "
            f"{', '.join(_sanitize(p) for p in vs.pr_ids)})",
        ]
        lines += [_tier_line(tier) for tier in vs.tiers]
        lines.append(
            f"    false positives (banked not-real matches): {len(vs.false_positives)}"
        )
        lines.append(f"    unadjudicated emissions: {len(vs.unadjudicated)}")
        adjudicable = [*vs.near_misses, *vs.unadjudicated, *vs.false_positives]
        if adjudicable:
            lines.append("  adjudication report:")
            lines += _adjudication_lines(adjudicable)
    return "\n".join(lines) + "\n"
