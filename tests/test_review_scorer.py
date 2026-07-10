"""The deterministic scorer (ADR-0048, RVW03-WS06): banked review-round records
vs the Ground-truth fixture — recall / false positives / unadjudicated per
Variant, underpowered tiers marked, near-misses + unmatched emissions surfaced
for Adjudication. Pure function of (fixture, record dicts); no LLM, no I/O."""

from __future__ import annotations

from shipit.finding import Severity
from shipit.review.groundtruth import parse_fixture
from shipit.review.scorer import (
    UNDERPOWERED_FLOOR,
    render_report,
    score_records,
)

BASE = "b5c33ce8ba14fe77fa27c7cd2bcfa4c086226722"
HEAD = "bf1d1c584f472e6628f464dd9b173ab6965457f2"

GT_CLAIM = (
    "gpu_fallback_reason estimates readback staging cost as w*h*16, ignoring "
    "the 256-byte row padding read_texture allocates"
)


def fixture(labels=None):
    return parse_fixture(
        {
            "schema": 1,
            "version": 3,
            "prs": [
                {
                    "id": "core-440",
                    "repo": "phos-editor/core",
                    "pr": 440,
                    "base_sha": BASE,
                    "head_sha": HEAD,
                }
            ],
            "labels": labels
            if labels is not None
            else [
                {
                    "id": "core-G1",
                    "pr": "core-440",
                    "file": "phos-bench/src/bin/gpu_compare.rs",
                    "lines": [100, 160],
                    "severity": "major",
                    "verdict": "real",
                    "confirmed": True,
                    "claim": GT_CLAIM,
                    "provenance": {"kind": "fix-commit", "ref": "f211ab3"},
                },
                {
                    "id": "core-F1",
                    "pr": "core-440",
                    "file": "phos-editor/src/eval.rs",
                    "lines": [50, 60],
                    "severity": "major",
                    "verdict": "not-real",
                    "confirmed": True,
                    "claim": "Backend::Cpu is used without being imported so the build fails",
                    "provenance": {
                        "kind": "adjudication",
                        "ref": "issue-638 T7 rebuttal",
                    },
                },
                {
                    "id": "core-C1",
                    "pr": "core-440",
                    "file": "phos-editor/src/node.rs",
                    "lines": [140, 160],
                    "severity": "major",
                    "verdict": "real",
                    "confirmed": False,  # candidate: must never enter a metric
                    "claim": "cross-module contract docs describe the pre-activation world",
                    "provenance": {"kind": "fix-commit", "ref": "deadbee"},
                },
            ],
        }
    )


def record(findings, *, variant="arm-a", repo="phos-editor/core", base=BASE, head=HEAD):
    return {
        "round.schema_version": 2,
        "round.repo": repo,
        "round.pr": None,
        "round.range": {"base": base, "head": head},
        "round.variant": {"content_hash": "sha256:aaa", "label": variant},
        "round.findings": findings,
    }


def finding(
    file, line, text, *, severity="major", disposition="post", duplicate_of=None
):
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "text": text,
        "disposition": disposition,
        "duplicate_of": duplicate_of,
    }


def hit(text=GT_CLAIM, line=120):
    return finding("phos-bench/src/bin/gpu_compare.rs", line, text)


def tier(report, variant_idx, severity):
    vs = report.variants[variant_idx]
    return next(t for t in vs.tiers if t.severity is severity)


class TestJoin:
    def test_out_of_fixture_records_are_counted_not_scored(self):
        report = score_records(
            fixture(),
            [record([hit()], repo="other/repo"), record([hit()], head="f" * 40)],
        )
        assert report.records_seen == 2
        assert report.records_scored == 0
        assert report.variants == ()

    def test_sha_prefix_matching_joins_abbreviated_records(self):
        # replay records may pin at short SHAs; the fixture pins full ones.
        report = score_records(
            fixture(), [record([hit()], base=BASE[:8], head=HEAD[:8])]
        )
        assert report.records_scored == 1

    def test_report_names_the_fixture_version(self):
        assert score_records(fixture(), []).fixture_version == 3


class TestRecall:
    def test_matching_finding_recalls_the_label(self):
        report = score_records(fixture(), [record([hit()])])
        assert report.variants[0].recalled_label_ids == ("core-G1",)
        major = tier(report, 0, Severity.MAJOR)
        assert (major.recalled, major.positives) == (1, 1)

    def test_wording_variant_still_recalls(self):
        rephrased = (
            "staging buffer size for the GPU readback is estimated as w*h*16 "
            "which ignores the 256-byte row padding that read_texture allocates, "
            "so padded widths panic"
        )
        report = score_records(fixture(), [record([hit(rephrased)])])
        assert report.variants[0].recalled_label_ids == ("core-G1",)

    def test_missing_finding_scores_zero_recall(self):
        report = score_records(fixture(), [record([])])
        major = tier(report, 0, Severity.MAJOR)
        assert (major.recalled, major.positives) == (0, 1)

    def test_candidate_labels_never_enter_the_denominator(self):
        report = score_records(fixture(), [record([])])
        # confirmed real majors = just core-G1; the candidate core-C1 is excluded.
        assert tier(report, 0, Severity.MAJOR).positives == 1
        assert report.candidate_labels == 1

    def test_routed_out_findings_never_score(self):
        # The calibrator-dropped app-G1 failure mode (issue #665): a dropped
        # finding did NOT reach the PR, so it must not count as recall.
        dropped = dict(hit(), disposition="drop-unverified")
        merged_dupe = dict(hit(), duplicate_of=0)
        report = score_records(fixture(), [record([dropped, merged_dupe])])
        assert report.variants[0].recalled_label_ids == ()

    def test_variants_score_separately(self):
        report = score_records(
            fixture(), [record([hit()], variant="arm-a"), record([], variant="arm-b")]
        )
        by_name = {vs.variant: vs for vs in report.variants}
        # the arm key mirrors the eval report: `content_hash [label]`.
        assert by_name["sha256:aaa [arm-a]"].recalled_label_ids == ("core-G1",)
        assert by_name["sha256:aaa [arm-b]"].recalled_label_ids == ()

    def test_same_label_different_hash_do_not_collapse(self):
        # Two prompt versions carrying the SAME A/B label are DISTINCT arms —
        # keying on the label alone would merge their denominators and recalls.
        def rec(content_hash, findings):
            return {
                "round.schema_version": 2,
                "round.repo": "phos-editor/core",
                "round.pr": None,
                "round.range": {"base": BASE, "head": HEAD},
                "round.variant": {"content_hash": content_hash, "label": "arm-a"},
                "round.findings": findings,
            }

        report = score_records(
            fixture(), [rec("sha256:aaa", [hit()]), rec("sha256:bbb", [])]
        )
        by_name = {vs.variant: vs for vs in report.variants}
        assert by_name["sha256:aaa [arm-a]"].recalled_label_ids == ("core-G1",)
        assert by_name["sha256:bbb [arm-a]"].recalled_label_ids == ()


class TestFalsePositivesAndAdjudication:
    def test_matching_a_not_real_label_is_a_measured_false_positive(self):
        fp = finding(
            "phos-editor/src/eval.rs",
            55,
            "Backend::Cpu is referenced but never imported, so this fails to build",
        )
        report = score_records(fixture(), [record([fp])])
        vs = report.variants[0]
        assert len(vs.false_positives) == 1
        assert vs.false_positives[0].label_id == "core-F1"
        assert vs.recalled_label_ids == ()

    def test_unmatched_emission_lands_in_the_adjudication_report(self):
        new = finding("phos-editor/src/other.rs", 10, "entirely novel defect claim")
        report = score_records(fixture(), [record([new])])
        vs = report.variants[0]
        assert len(vs.unadjudicated) == 1
        assert vs.unadjudicated[0].kind == "unmatched"

    def test_near_miss_surfaces_with_its_label_id(self):
        # right file + line, wording the lexicon does not know → alias feeder.
        near = finding(
            "phos-bench/src/bin/gpu_compare.rs",
            120,
            "the fallback decision sizes its staging buffer allocation incorrectly",
        )
        report = score_records(fixture(), [record([near])])
        vs = report.variants[0]
        assert [n.label_id for n in vs.near_misses] == ["core-G1"]
        assert vs.unadjudicated == ()


class TestDeterminism:
    def test_same_inputs_same_report(self):
        records = [
            record([hit(), finding("phos-editor/src/other.rs", 10, "novel claim")]),
            record([], variant="arm-b"),
        ]
        assert score_records(fixture(), records) == score_records(fixture(), records)

    def test_render_is_stable_and_names_the_version(self):
        report = score_records(fixture(), [record([hit()])])
        text = render_report(report)
        assert text == render_report(report)
        assert "fixture v3" in text


class TestUnderpowered:
    def test_small_tiers_render_with_the_marker(self):
        report = score_records(fixture(), [record([hit()])])
        major = tier(report, 0, Severity.MAJOR)
        assert major.positives < UNDERPOWERED_FLOOR and major.underpowered
        assert "[UNDERPOWERED]" in render_report(report)

    def test_tier_at_the_floor_is_powered(self):
        labels = [
            {
                "id": f"core-G{i}",
                "pr": "core-440",
                "file": f"src/f{i}.rs",
                "lines": [1, 5],
                "severity": "major",
                "verdict": "real",
                "confirmed": True,
                "claim": f"defect number {i} with distinct wording token{i}",
                "provenance": {"kind": "fix-commit", "ref": "abc1234"},
            }
            for i in range(UNDERPOWERED_FLOOR)
        ]
        report = score_records(fixture(labels=labels), [record([])])
        assert not tier(report, 0, Severity.MAJOR).underpowered

    def test_empty_store_renders_the_empty_report(self):
        text = render_report(score_records(fixture(), []))
        assert "nothing to score" in text


class TestReportSanitization:
    def test_control_chars_in_emission_text_cannot_forge_output(self):
        # Adjudication text is model-generated round-record data: an ANSI escape
        # or bare newline must not reach the terminal and forge report structure.
        hostile = finding(
            "phos-editor/src/other.rs",
            10,
            "\x1b[2Kspoofed\nHEADER: fake recall 99/99",
        )
        text = render_report(score_records(fixture(), [record([hostile])]))
        assert "\x1b" not in text
        # the embedded newline is neutralized: the whole emission stays one line.
        emission = next(ln for ln in text.splitlines() if "spoofed" in ln)
        assert "fake recall 99/99" in emission
