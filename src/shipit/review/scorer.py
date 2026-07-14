"""scorer — deterministic scoring of banked Review-round records against the fixture.

The measuring instrument of the Review Lab (ADR-0048, RVW03-WS06): given the
in-repo Ground-truth fixture (:mod:`shipit.review.groundtruth`) and the banked
**Review-round records** (:mod:`shipit.review.roundrecord` — the JSONL store the
replay/review paths append to), report **recall**, **false positives**, and
**unadjudicated** counts per **Variant**. Zero tokens, zero LLM, deterministic
across runs: scoring banked records is free, repeatable, and CI-runnable —
which is the whole point (a misjudging semantic matcher here would be the
RVW02 calibrator failure reproduced inside the ruler).

The pipeline, all PURE (:func:`score_records` is a function of fixture +
record dicts; the CLI boundary lives in :mod:`shipit.verbs.eval.score`):

1. A record joins the fixture when its ``round.repo`` equals a pinned range's
   repo and its ``round.range`` base+head SHAs prefix-match the pin (records
   and fixture may pin at different abbreviation lengths). Records outside the
   fixture are counted, never scored.
2. Within a joined record only POSTED findings score (``disposition == post``
   and canonical — the same read every reporter uses, RVW02-WS04): the scorer
   measures what the review CONCLUDED, i.e. what reached (or would reach) the
   PR.
3. Each posted finding meets the pin's CONFIRMED labels through the one
   matching primitive (:func:`shipit.review.match.match_claim`: same file,
   line within the label's range, claim-token overlap with aliases honored).
   Candidate (unconfirmed) labels never enter a metric — opinion must not sit
   in a denominator.
4. Per Variant (``round.variant`` content-hash, decorated with the optional
   A/B label — the same key the eval report groups by, so an arm reads alike
   on both surfaces): a ``real`` label is RECALLED when any posted
   finding of that variant matches it; a finding matching a ``not-real`` label
   is a measured FALSE POSITIVE (the banked refutation paying rent); a finding
   matching nothing is UNADJUDICATED — not wrong, *unknown to the corpus* —
   and near-misses are surfaced beside it in the adjudication report
   (:class:`Adjudication`), each carrying the banking suggestion that grows
   the fixture (new label, or alias on a near-missed label).

Recall counts DEFECTS, not labels: labels sharing a declared ``defect``
equivalence family (:attr:`shipit.review.groundtruth.Label.defect_key` — one
defect with several valid file/site anchors, #673/#751) contribute ONE
denominator slot per tier and are recalled together when ANY member label
matches, so equivalent labels and equivalent emissions never double-count.
Labels without a family keep counting separately — distinct defects and
repeated instances stay distinguishable.

Recall denominators are per-variant honest: a variant is only measured against
labels of pins it actually has records for. Any severity tier whose positive
count is below :data:`UNDERPOWERED_FLOOR` renders with an **underpowered**
marker, never as a headline number (ADR-0048 — the 0/3-style coin flip must
announce itself). Every report names the fixture version it scored against;
numbers from different versions are never comparable.
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

#: A severity tier with fewer ground-truth positives than this is statistically
#: underpowered (ADR-0048's "~20"): its recall renders with a marker, never as
#: a headline — one finding must not be able to swing a metric by 30+ points.
UNDERPOWERED_FLOOR = 20

#: The bucket a record with no variant groups under (mirrors the eval report's
#: convention, so the two surfaces name an unlabeled arm identically).
_NO_VARIANT = "(none)"


@dataclass(frozen=True)
class TierScore:
    """One severity tier of one variant: recalled / positives (+ the power marker).

    Both counts are DEFECTS (equivalence families count once), not labels."""

    severity: Severity
    positives: int
    recalled: int

    @property
    def underpowered(self) -> bool:
        return self.positives < UNDERPOWERED_FLOOR


@dataclass(frozen=True)
class Adjudication:
    """One emission awaiting a human verdict — the fixture's growth path.

    ``kind`` is ``near-miss`` (matched a label's location but not its lexicon —
    bank an alias on ``label_id``) or ``unmatched`` (the corpus does not know
    this claim — bank a new label, real or not-real). ``pr_id``/``file``/
    ``line``/``severity``/``text`` locate and state the emission so the human
    can rule on it without replaying anything.
    """

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
    """One Variant's scorecard across every fixture pin it has records for.

    ``recalled_label_ids`` lists the individual anchor LABELS that matched
    (useful for seeing which anchor of a family a review hit); the ``tiers``
    counts collapse those to defects, so a multi-anchor family recalled at two
    anchors still scores as one."""

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
    """The whole scoring run: per-variant scores + what was and wasn't scored."""

    fixture_version: int
    variants: tuple[VariantScore, ...]
    confirmed_labels: int
    candidate_labels: int
    records_seen: int
    records_scored: int


def _sha_matches(recorded: str, pinned: str) -> bool:
    """Prefix-tolerant SHA equality (either side may abbreviate, ≥7 chars each)."""
    a, b = recorded.strip().lower(), pinned.strip().lower()
    if len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def _pin_for(fixture: Fixture, record: Mapping[str, Any]) -> PinnedRange | None:
    """The pinned range this record replayed, else None (out-of-fixture round)."""
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
    """The record's experiment-arm key — the SAME rendering the eval report's
    variant axis produces (:func:`shipit.verbs.eval.report._variant_bucket`):
    ``content_hash``, or ``content_hash [label]`` when an A/B label is present,
    else ``(none)``. Keyed on the content-hash IDENTITY (label only decorates)
    so two arms of the same prompt sharing one label never collapse into a
    single bucket — which would mix their denominators and recalled sets."""
    variant = record.get("round.variant")
    if not isinstance(variant, Mapping):
        return _NO_VARIANT
    content_hash = variant.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        return _NO_VARIANT
    label = variant.get("label")
    return f"{content_hash} [{label}]" if label is not None else content_hash


def _posted_findings(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The findings that reached (or would reach) the PR: ``post`` AND canonical
    (``duplicate_of is None``) — the RVW02-WS04 posted read, never raw disposition."""
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
    """Score every in-fixture record against ``fixture``. PURE, deterministic.

    Iteration order is fixed (records in store order, labels in fixture order,
    variants sorted) so the same store + the same fixture render the same
    report byte-for-byte, forever — ADR-0048's free-to-re-run property.
    """
    # variant -> accumulator state
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
        labels = fixture.labels_for(pin.id)  # confirmed only — the scoreable truth
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
        # The honest denominator: real confirmed labels of the pins this
        # variant actually has records for.
        in_scope = [
            label
            for pid in pin_ids
            for label in fixture.labels_for(pid)
            if label.verdict == "real"
        ]
        tiers = []
        for severity in Severity:
            tier_labels = [lb for lb in in_scope if lb.severity is severity]
            # Count DEFECTS, not labels: a declared equivalence family (one
            # defect, several valid anchors — #673/#751) fills one denominator
            # slot and is recalled when ANY of its anchor labels matched. A
            # family never straddles tiers or pins (parse validates severity
            # and pr coherence), so grouping within the tier is total.
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


# --- rendering (text; the CLI's output layer) ---------------------------------


#: Control characters (C0/C1 + DEL) that must never reach the terminal verbatim:
#: adjudication text is model-generated round-record data, so an embedded ANSI
#: escape or bare newline could forge report structure or spoof output (CWE-150).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    """One line of untrusted record text, safe to interpolate into the report:
    control characters (ESC, CR/LF, the rest of C0/C1 + DEL) become a visible
    ``·`` so external text can neither move the cursor nor break the layout."""
    return _CONTROL_CHARS.sub("·", text)


def _tier_line(tier: TierScore) -> str:
    marker = "  [UNDERPOWERED]" if tier.underpowered else ""
    pct = f" ({tier.recalled / tier.positives:.0%})" if tier.positives else ""
    return f"    {tier.severity.value:<8}  recall {tier.recalled}/{tier.positives}{pct}{marker}"


def _adjudication_lines(items: Sequence[Adjudication]) -> list[str]:
    out = []
    for item in items:
        # Every interpolated field is external (finding text from the round
        # records, ids/paths from the fixture file) — all sanitized so neither
        # source can forge terminal output (CWE-150).
        pr_id = _sanitize(item.pr_id)
        file = _sanitize(item.file)
        loc = f"{file}:{item.line}" if item.line is not None else file
        severity = _sanitize(item.severity) or "?"
        label_id = _sanitize(item.label_id or "")
        head = f"    [{pr_id}] {loc} ({severity}) — {_sanitize(item.text)}"
        out.append(head)
        if item.kind == "near-miss":
            # The id is fixture-supplied, so the copy-paste command guards it on
            # two layers: shlex.quote keeps it one shell token (whitespace), and
            # the `--` terminator keeps Click from parsing an option-looking id
            # (e.g. `--fixture=…`) as an option instead of the LABEL_ID argument.
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
    """The score report as text. Deterministic; the adjudication report rides
    at the end so near-misses/unmatched emissions are impossible to miss."""
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
