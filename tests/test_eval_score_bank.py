"""The ``shipit eval score`` / ``shipit eval bank`` verb boundaries (RVW03-WS06):
score reads the fixture + the per-repo review-rounds stores (family-root
injected, exactly like the eval report verb) and prints the deterministic
report; bank writes an adjudicated verdict through the pure banking core and
bumps the fixture version on disk."""

from __future__ import annotations

import io
import json

import pytest
from click.testing import CliRunner

from shipit.harness.eval import store
from shipit.identity import repo_from_slug
from shipit.review.groundtruth import load_fixture
from shipit.verbs.eval import bank as bank_verb
from shipit.verbs.eval import score as score_verb

BASE = "b5c33ce8ba14fe77fa27c7cd2bcfa4c086226722"
HEAD = "bf1d1c584f472e6628f464dd9b173ab6965457f2"

FIXTURE_TOML = f"""
schema = 1
version = 1

[[prs]]
id = "core-440"
repo = "phos-editor/core"
pr = 440
base_sha = "{BASE}"
head_sha = "{HEAD}"

[[labels]]
id = "core-G1"
pr = "core-440"
file = "phos-bench/src/bin/gpu_compare.rs"
lines = [100, 160]
severity = "major"
verdict = "real"
confirmed = true
claim = "readback staging estimate ignores the 256-byte row padding read_texture allocates"
[labels.provenance]
kind = "fix-commit"
ref = "f211ab3"
"""


@pytest.fixture
def fixture_path(tmp_path):
    path = tmp_path / "fixture.toml"
    path.write_text(FIXTURE_TOML, encoding="utf-8")
    return path


def bank_round(base_dir, findings):
    record = {
        "round.schema_version": 2,
        "round.repo": "phos-editor/core",
        "round.pr": None,
        "round.range": {"base": BASE, "head": HEAD},
        "round.variant": {"content_hash": "sha256:aaa", "label": "arm-a"},
        "round.findings": findings,
    }
    store.append_record(
        record,
        repo_from_slug("phos-editor/core"),
        base_dir,
        kind=store.REVIEW_ROUNDS_KIND,
    )


class TestScoreRun:
    def test_scores_stores_of_the_fixture_repos(self, tmp_path, fixture_path):
        bank_round(
            tmp_path / "stores",
            [
                {
                    "file": "phos-bench/src/bin/gpu_compare.rs",
                    "line": 120,
                    "severity": "major",
                    "text": "staging estimate ignores 256-byte row padding from read_texture",
                    "disposition": "post",
                    "duplicate_of": None,
                }
            ],
        )
        out = io.StringIO()
        rc = score_verb.run(fixture_path, base_dir=tmp_path / "stores", out=out)
        assert rc == 0
        text = out.getvalue()
        assert "fixture v1" in text
        assert "arm-a" in text
        assert "recall 1/1" in text
        assert "[UNDERPOWERED]" in text  # 1 positive < ~20: never a headline

    def test_missing_stores_render_the_empty_report(self, tmp_path, fixture_path):
        out = io.StringIO()
        assert score_verb.run(fixture_path, base_dir=tmp_path / "empty", out=out) == 0
        assert "nothing to score" in out.getvalue()

    def test_bad_fixture_is_exit_1_never_an_empty_score(self, tmp_path):
        bad = tmp_path / "fixture.toml"
        bad.write_text("version = 0\n", encoding="utf-8")
        assert score_verb.run(bad, base_dir=tmp_path) == 1

    def test_cli_seam(self, tmp_path):
        # The click layer defaults the store family root, so the CLI test stays
        # on the loud-failure path (absent fixture) — the happy path is proven
        # through run() with an injected root above.
        result = CliRunner().invoke(score_verb.cmd, [str(tmp_path / "absent.toml")])
        assert result.exit_code == 1


class TestBank:
    def test_bank_label_bumps_version_and_confirms(self, fixture_path):
        result = CliRunner().invoke(
            bank_verb.group,
            [
                "label",
                "--fixture",
                str(fixture_path),
                "--id",
                "core-A1",
                "--pr",
                "core-440",
                "--file",
                "phos-editor/src/eval.rs",
                "--lines",
                "50:60",
                "--severity",
                "major",
                "--verdict",
                "not-real",
                "--claim",
                "Backend::Cpu is used without being imported",
                "--provenance",
                "adjudication:issue-638 T7 rebuttal",
            ],
        )
        assert result.exit_code == 0, result.output
        fixture = load_fixture(fixture_path)
        assert fixture.version == 2
        label = fixture.label_by_id("core-A1")
        assert label.confirmed and label.verdict == "not-real"
        assert label.lines == (50, 60)

    def test_bank_alias_bumps_version(self, fixture_path):
        result = CliRunner().invoke(
            bank_verb.group,
            [
                "alias",
                "--fixture",
                str(fixture_path),
                "core-G1",
                "--text",
                "staging buffer math misses row padding",
            ],
        )
        assert result.exit_code == 0, result.output
        fixture = load_fixture(fixture_path)
        assert fixture.version == 2
        assert (
            "staging buffer math misses row padding"
            in fixture.label_by_id("core-G1").texts
        )

    @pytest.mark.parametrize(
        "args",
        [
            ["--severity", "blocker"],  # retired ladder token
            ["--provenance", "no-colon-ref"],
            ["--lines", "50-60"],  # wrong separator
            ["--pr", "unknown-pr"],
            ["--id", "core-G1"],  # duplicate
        ],
    )
    def test_bad_bank_args_exit_1_and_leave_the_file_untouched(
        self, fixture_path, args
    ):
        base = {
            "--id": "core-A9",
            "--pr": "core-440",
            "--file": "f.rs",
            "--severity": "major",
            "--verdict": "real",
            "--claim": "c is broken",
            "--provenance": "fix-commit:abc1234",
        }
        for key, value in zip(args[::2], args[1::2], strict=True):
            base[key] = value
        argv = ["label", "--fixture", str(fixture_path)]
        for key, value in base.items():
            argv += [key, value]
        result = CliRunner().invoke(bank_verb.group, argv)
        assert result.exit_code == 1
        assert load_fixture(fixture_path).version == 1

    def test_banked_verdict_scores_on_the_next_run(self, tmp_path, fixture_path):
        # The ADR-0048 loop closed end-to-end: an unmatched emission is banked
        # not-real, and the SAME store then scores it as a false positive.
        findings = [
            {
                "file": "phos-editor/src/eval.rs",
                "line": 55,
                "severity": "major",
                "text": "Backend::Cpu is used without being imported so this cannot build",
                "disposition": "post",
                "duplicate_of": None,
            }
        ]
        bank_round(tmp_path / "stores", findings)
        out = io.StringIO()
        score_verb.run(fixture_path, base_dir=tmp_path / "stores", out=out)
        assert "unadjudicated emissions: 1" in out.getvalue()

        result = CliRunner().invoke(
            bank_verb.group,
            [
                "label",
                "--fixture",
                str(fixture_path),
                "--id",
                "core-F1",
                "--pr",
                "core-440",
                "--file",
                "phos-editor/src/eval.rs",
                "--lines",
                "50:60",
                "--severity",
                "major",
                "--verdict",
                "not-real",
                "--claim",
                "Backend::Cpu is used without being imported so the build fails",
                "--provenance",
                "adjudication:issue-638 T7 rebuttal",
            ],
        )
        assert result.exit_code == 0, result.output
        out = io.StringIO()
        score_verb.run(fixture_path, base_dir=tmp_path / "stores", out=out)
        text = out.getvalue()
        assert "fixture v2" in text
        assert "false positives (banked not-real matches): 1" in text
        assert "unadjudicated emissions: 0" in text


def test_round_record_json_shape_matches_scorer_expectations():
    """Guard the seam: the scorer reads the EXACT keys roundrecord writes."""
    from shipit.finding import Disposition, Finding, JudgedFinding, Severity
    from shipit.review import roundrecord

    record = roundrecord.build(
        review={"summary": {"status": "COMMENT"}},
        findings=[
            JudgedFinding(
                Finding(
                    severity=Severity.MAJOR,
                    text="staging estimate ignores 256-byte row padding",
                    file="phos-bench/src/bin/gpu_compare.rs",
                    line=120,
                ),
                Disposition.POST,
            )
        ],
        repo="phos-editor/core",
        pr=None,
        base_sha=BASE,
        head_sha=HEAD,
        reviewer="codex",
        model="gpt-5.1",
        timeout="900",
        instructions_path=None,
        variant={"content_hash": "sha256:aaa", "label": "arm-a"},
        runs=(),
        duration_ms=1,
        total_tokens=None,
        timestamp="2026-07-10T00:00:00Z",
    )
    # JSONL round trip, as the store does it.
    record = json.loads(json.dumps(record))
    from shipit.review.groundtruth import parse_fixture
    from shipit.review.scorer import score_records

    fixture = parse_fixture(
        {
            "schema": 1,
            "version": 1,
            "prs": [
                {
                    "id": "core-440",
                    "repo": "phos-editor/core",
                    "pr": 440,
                    "base_sha": BASE,
                    "head_sha": HEAD,
                }
            ],
            "labels": [
                {
                    "id": "core-G1",
                    "pr": "core-440",
                    "file": "phos-bench/src/bin/gpu_compare.rs",
                    "lines": [100, 160],
                    "severity": "major",
                    "verdict": "real",
                    "confirmed": True,
                    "claim": "readback staging estimate ignores the 256-byte row padding",
                    "provenance": {"kind": "fix-commit", "ref": "f211ab3"},
                }
            ],
        }
    )
    report = score_records(fixture, [record])
    assert report.records_scored == 1
    assert report.variants[0].recalled_label_ids == ("core-G1",)
