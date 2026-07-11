"""The Ground-truth fixture domain (ADR-0048, RVW03-WS06): parse/validate the
in-repo corpus, bank Adjudication verdicts (label + alias, each bumping the
version), serialize deterministically. Also pins the SHIPPED lab/fixture.toml:
it must load, satisfy the v1 acceptance bars, and round-trip canonically."""

from __future__ import annotations

from pathlib import Path

import pytest

from shipit.finding import Severity
from shipit.review.groundtruth import (
    FIXTURE_SCHEMA_VERSION,
    Fixture,
    FixtureError,
    Label,
    PinnedRange,
    Provenance,
    bank_alias,
    bank_label,
    dump_fixture,
    load_fixture,
    parse_fixture,
    save_fixture,
)

PR = {
    "id": "core-440",
    "repo": "phos-editor/core",
    "pr": 440,
    "base_sha": "b5c33ce8ba14fe77fa27c7cd2bcfa4c086226722",
    "head_sha": "bf1d1c584f472e6628f464dd9b173ab6965457f2",
}
LABEL = {
    "id": "core-G1",
    "pr": "core-440",
    "file": "phos-bench/src/bin/gpu_compare.rs",
    "lines": [100, 140],
    "severity": "major",
    "verdict": "real",
    "confirmed": True,
    "claim": "readback staging estimate ignores 256-byte row padding",
    "aliases": ["staging size math misses row padding"],
    "provenance": {"kind": "fix-commit", "ref": "f211ab3"},
}


def data(**over):
    d = {"schema": 1, "version": 1, "prs": [dict(PR)], "labels": [dict(LABEL)]}
    d.update(over)
    return d


def label_data(**over):
    d = dict(LABEL)
    d.update(over)
    return d


class TestParseFixture:
    def test_full_round_parses(self):
        fixture = parse_fixture(data())
        assert fixture.version == 1
        assert fixture.prs[0].repo == "phos-editor/core"
        label = fixture.labels[0]
        assert label.severity is Severity.MAJOR
        assert label.lines == (100, 140)
        assert label.texts == (label.claim, *label.aliases)
        assert label.provenance == Provenance("fix-commit", "f211ab3")

    def test_repo_slug_is_canonicalized_to_lowercase(self):
        # A mixed-case slug validates but is stored canonical, so two pins that
        # differ only by case share one identity (no double-read in eval score).
        fixture = parse_fixture(data(prs=[dict(PR, repo="Phos-Editor/Core")]))
        assert fixture.prs[0].repo == "phos-editor/core"

    def test_lines_are_optional_file_scoped(self):
        raw = label_data()
        del raw["lines"]
        fixture = parse_fixture(data(labels=[raw]))
        assert fixture.labels[0].lines is None

    @pytest.mark.parametrize(
        "corrupt",
        [
            {"version": 0},
            {"version": "one"},
            {"schema": FIXTURE_SCHEMA_VERSION + 1},
            {"prs": [dict(PR, base_sha="not-a-sha")]},
            {"prs": [dict(PR, head_sha="abc")]},  # too short
            {"prs": [dict(PR, repo="not-a-slug")]},  # not owner/name
            {"prs": [dict(PR, repo="owner/name/extra")]},  # too many segments
            {"prs": [dict(PR, title=123)]},  # soft field, wrong type (no coercion)
            {"prs": [dict(PR), dict(PR)]},  # duplicate pr id
            {"labels": [label_data(pr="unknown-pr")]},
            {"labels": [label_data(severity="blocker")]},  # retired ladder
            {"labels": [label_data(verdict="maybe")]},
            {"labels": [label_data(lines=[9, 3])]},
            {"labels": [label_data(provenance={"kind": "vibes", "ref": "x"})]},
            {"labels": [label_data(provenance=None)]},
            {"labels": [label_data(), label_data()]},  # duplicate label id
            {"labels": [label_data(confirmed="yes")]},
        ],
    )
    def test_every_contract_violation_is_loud(self, corrupt):
        with pytest.raises(FixtureError):
            parse_fixture(data(**corrupt))

    def test_labels_for_defaults_to_confirmed_only(self):
        candidate = label_data(id="core-C1", confirmed=False)
        fixture = parse_fixture(data(labels=[dict(LABEL), candidate]))
        assert [lb.id for lb in fixture.labels_for("core-440")] == ["core-G1"]
        assert len(fixture.labels_for("core-440", confirmed_only=False)) == 2


class TestDefectFamilies:
    """The explicit equivalence-family identity (#751): one defect, several
    valid anchors, declared in fixture data — never inferred similarity."""

    def test_defect_family_parses_and_keys(self):
        fixture = parse_fixture(
            data(
                labels=[
                    label_data(defect="core-fam"),
                    label_data(
                        id="core-G2",
                        file="phos-editor/src/graph_session.rs",
                        defect="core-fam",
                    ),
                ]
            )
        )
        assert [lb.defect_key for lb in fixture.labels] == ["core-fam", "core-fam"]

    def test_label_without_defect_is_its_own_key(self):
        label = parse_fixture(data()).labels[0]
        assert label.defect is None and label.defect_key == "core-G1"

    @pytest.mark.parametrize(
        "second",
        [
            # a family may not straddle verdicts, severities, or pinned ranges.
            label_data(id="core-G2", defect="fam", verdict="not-real"),
            label_data(id="core-G2", defect="fam", severity="nit"),
        ],
    )
    def test_incoherent_family_is_loud(self, second):
        with pytest.raises(FixtureError, match="defect family"):
            parse_fixture(data(labels=[label_data(defect="fam"), second]))

    def test_family_across_pinned_ranges_is_loud(self):
        pr2 = dict(PR, id="core-441", pr=441)
        with pytest.raises(FixtureError, match="defect family"):
            parse_fixture(
                data(
                    prs=[dict(PR), pr2],
                    labels=[
                        label_data(defect="fam"),
                        label_data(id="core-G2", pr="core-441", defect="fam"),
                    ],
                )
            )

    def test_blank_defect_is_loud(self):
        with pytest.raises(FixtureError):
            parse_fixture(data(labels=[label_data(defect="  ")]))

    def test_family_id_cannot_silently_capture_an_unfamilied_label(self):
        with pytest.raises(FixtureError, match="collides with label id"):
            parse_fixture(
                data(
                    labels=[
                        label_data(),
                        label_data(id="core-G2", defect="core-G1"),
                    ]
                )
            )

    def test_defect_round_trips_through_dump(self, tmp_path):
        fixture = parse_fixture(data(labels=[label_data(defect="core-fam")]))
        path = tmp_path / "fixture.toml"
        save_fixture(fixture, path)
        assert load_fixture(path).labels[0].defect == "core-fam"

    def test_bank_label_joins_a_family(self):
        fixture = parse_fixture(data(labels=[label_data(defect="core-fam")]))
        anchor = Label(
            id="core-G2",
            pr_id="core-440",
            file="phos-editor/src/graph_session.rs",
            severity=Severity.MAJOR,
            verdict="real",
            claim="the same defect stated at its other anchor site",
            provenance=Provenance("adjudication", "sheet-3"),
            defect="  core-fam  ",
        )
        banked = bank_label(fixture, anchor)
        assert banked.label_by_id("core-G2").defect == "core-fam"

    def test_bank_label_rejects_a_blank_defect(self):
        fixture = parse_fixture(data())
        blank = Label(
            id="core-G2",
            pr_id="core-440",
            file="phos-editor/src/graph_session.rs",
            severity=Severity.MAJOR,
            verdict="real",
            claim="c",
            provenance=Provenance("adjudication", "r"),
            defect="  ",
        )
        with pytest.raises(FixtureError, match="defect must be a non-empty string"):
            bank_label(fixture, blank)

    def test_bank_label_rejects_a_family_id_collision(self):
        fixture = parse_fixture(data())
        colliding = Label(
            id="core-G2",
            pr_id="core-440",
            file="phos-editor/src/graph_session.rs",
            severity=Severity.MAJOR,
            verdict="real",
            claim="c",
            provenance=Provenance("adjudication", "r"),
            defect="core-G1",
        )
        with pytest.raises(FixtureError, match="collides with label id"):
            bank_label(fixture, colliding)

    def test_bank_label_rejects_an_incoherent_family_member(self):
        fixture = parse_fixture(data(labels=[label_data(defect="core-fam")]))
        wrong_tier = Label(
            id="core-G2",
            pr_id="core-440",
            file="phos-editor/src/graph_session.rs",
            severity=Severity.NIT,  # the family is major
            verdict="real",
            claim="c",
            provenance=Provenance("adjudication", "r"),
            defect="core-fam",
        )
        with pytest.raises(FixtureError, match="defect family"):
            bank_label(fixture, wrong_tier)


class TestBanking:
    def test_bank_label_appends_confirmed_and_bumps_version(self):
        fixture = parse_fixture(data())
        new = Label(
            id="core-A1",
            pr_id="core-440",
            file="phos-editor/src/eval.rs",
            severity=Severity.MAJOR,
            verdict="not-real",
            claim="Backend::Cpu import is missing",
            provenance=Provenance("adjudication", "issue-638 T7 rebuttal"),
        )
        banked = bank_label(fixture, new)
        assert banked.version == 2
        assert banked.label_by_id("core-A1").confirmed is True
        # not-real is a first-class verdict: the banked refutation is scoreable.
        assert banked.label_by_id("core-A1").verdict == "not-real"
        assert fixture.version == 1  # pure: the input fixture is untouched

    def test_bank_label_rejects_duplicate_id_and_unknown_pr(self):
        fixture = parse_fixture(data())
        dupe = Label(
            id="core-G1",
            pr_id="core-440",
            file="x.rs",
            severity=Severity.NIT,
            verdict="real",
            claim="c",
            provenance=Provenance("adjudication", "r"),
        )
        with pytest.raises(FixtureError):
            bank_label(fixture, dupe)
        orphan = Label(
            id="core-A2",
            pr_id="nope",
            file="x.rs",
            severity=Severity.NIT,
            verdict="real",
            claim="c",
            provenance=Provenance("adjudication", "r"),
        )
        with pytest.raises(FixtureError):
            bank_label(fixture, orphan)

    def test_bank_alias_appends_and_bumps_version(self):
        fixture = parse_fixture(data())
        banked = bank_alias(
            fixture, "core-G1", "padding math wrong for readback buffers"
        )
        assert banked.version == 2
        assert (
            "padding math wrong for readback buffers"
            in banked.label_by_id("core-G1").texts
        )

    def test_bank_alias_rejects_blank_duplicate_and_unknown_label(self):
        fixture = parse_fixture(data())
        with pytest.raises(FixtureError):
            bank_alias(fixture, "core-G1", "  ")
        with pytest.raises(FixtureError):
            bank_alias(fixture, "core-G1", LABEL["aliases"][0])
        with pytest.raises(FixtureError):
            bank_alias(fixture, "nope", "text")


class TestSerialization:
    def test_dump_load_round_trip_is_identity(self, tmp_path):
        fixture = parse_fixture(data())
        path = tmp_path / "fixture.toml"
        save_fixture(fixture, path)
        assert load_fixture(path) == fixture
        # deterministic: same fixture, same bytes (ADR-0048 re-run property).
        text = path.read_text()
        save_fixture(fixture, path)
        assert path.read_text() == text

    def test_dump_escapes_quotes_and_backslashes(self, tmp_path):
        fixture = parse_fixture(
            data(labels=[label_data(claim='claim with "quotes" and back\\slash')])
        )
        path = tmp_path / "fixture.toml"
        save_fixture(fixture, path)
        assert (
            load_fixture(path).labels[0].claim == 'claim with "quotes" and back\\slash'
        )

    def test_non_bmp_claim_round_trips(self, tmp_path):
        # An emoji (or any non-BMP char) in a claim must serialize as literal
        # UTF-8, not a `🚀` surrogate pair — TOML forbids surrogate
        # escapes, so the pair would make save's own round-trip parse reject it.
        fixture = parse_fixture(
            data(labels=[label_data(claim="rocket 🚀 in the readback path")])
        )
        path = tmp_path / "fixture.toml"
        save_fixture(fixture, path)
        assert load_fixture(path).labels[0].claim == "rocket 🚀 in the readback path"

    def test_del_control_char_in_claim_round_trips(self, tmp_path):
        # DEL (U+007F) is the one char json.dumps leaves literal but TOML forbids
        # in a basic string — it must be escaped or the save round-trip crashes.
        fixture = parse_fixture(data(labels=[label_data(claim="before\x7fafter")]))
        path = tmp_path / "fixture.toml"
        save_fixture(fixture, path)
        assert load_fixture(path).labels[0].claim == "before\x7fafter"

    def test_save_is_atomic_and_leaves_no_temp(self, tmp_path):
        # Overwrite an existing file, then assert only the target remains (the
        # same-directory temp is always cleaned up — no `.tmp-<pid>` litter).
        path = tmp_path / "fixture.toml"
        save_fixture(parse_fixture(data()), path)
        save_fixture(parse_fixture(data(version=2)), path)
        assert load_fixture(path).version == 2
        assert [p.name for p in tmp_path.iterdir()] == ["fixture.toml"]

    def test_save_validates_a_programmatic_fixture(self, tmp_path):
        # One rule set: a Label assembled in code (bad verdict) must fail the
        # same parse contract a hand-written file does, before touching disk.
        bad = Fixture(
            version=1,
            prs=(
                PinnedRange(
                    id="p", repo="o/r", pr=1, base_sha="a" * 40, head_sha="b" * 40
                ),
            ),
            labels=(
                Label(
                    id="l",
                    pr_id="p",
                    file="f.py",
                    severity=Severity.MAJOR,
                    verdict="maybe",
                    claim="c",
                    provenance=Provenance("fix-commit", "abc1234"),
                ),
            ),
        )
        path = tmp_path / "fixture.toml"
        with pytest.raises(FixtureError):
            save_fixture(bad, path)
        assert not path.exists()

    def test_missing_file_is_loud(self, tmp_path):
        with pytest.raises(FixtureError):
            load_fixture(tmp_path / "absent.toml")


@pytest.fixture(scope="module")
def shipped():
    return load_fixture(Path(__file__).resolve().parents[1] / "lab" / "fixture.toml")


class TestShippedFixture:
    """The committed lab/fixture.toml IS product data — pin its acceptance bars.

    The version pin tracks the fixture's current banked version: every
    adjudication session bumps it (v1 → v35 at the first cells banking), and
    this test is updated in the same diff as the deliberate bump.
    """

    def test_loads_and_pins_current_version(self, shipped):
        assert shipped.version == 39

    def test_8_to_12_pinned_ranges(self, shipped):
        assert 8 <= len(shipped.prs) <= 12

    def test_at_least_25_major_or_worse_labels(self, shipped):
        major_plus = [lb for lb in shipped.labels if lb.severity.blocks_merge]
        assert len(major_plus) >= 25

    def test_every_label_carries_provenance(self, shipped):
        assert all(lb.provenance.kind and lb.provenance.ref for lb in shipped.labels)

    def test_includes_the_three_ws05_baseline_prs(self, shipped):
        ids = {p.id for p in shipped.prs}
        assert {"core-440", "app-391", "lex-820"} <= ids

    def test_spans_language_and_repo(self, shipped):
        assert len({p.repo for p in shipped.prs}) >= 4
        assert len({p.language for p in shipped.prs if p.language}) >= 3

    def test_673_residual_families_are_declared(self, shipped):
        # The v35–v37 adjudication evidence on #673: each documented cross-file
        # residual's promoted labels share one explicit defect family (#751) —
        # graph-session, distance, native-spec, and GPU-residency.
        families = {}
        for label in shipped.labels:
            if label.defect is not None:
                families.setdefault(label.defect, set()).add(label.id)
        assert families == {
            "core-gpu-region-coverage": {"core-G11", "core-G26"},
            "lex-below-title-annotation": {"lex-G5", "lex-G23"},
            "app-probe-bypasses-viewport-render": {"app-G6", "app-G14"},
            "core-stale-residency-doc": {"core-G14", "core-G19", "core-G25"},
        }

    def test_is_in_canonical_form(self, shipped):
        path = Path(__file__).resolve().parents[1] / "lab" / "fixture.toml"
        assert path.read_text(encoding="utf-8") == dump_fixture(shipped), (
            "lab/fixture.toml is not canonical — re-save it via "
            "shipit.review.groundtruth.save_fixture (banking does this for you)"
        )
