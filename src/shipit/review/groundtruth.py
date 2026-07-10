"""groundtruth — the versioned, in-repo Ground-truth fixture (ADR-0048, RVW03-WS06).

The fixture is the corpus review experiments are SCORED against (CONTEXT.md
"Ground-truth fixture"): pinned historical portfolio PR ranges
(:class:`PinnedRange`: repo + base/head SHA) and their evidence-backed
:class:`Label`\\ s — a located claim judged ``real`` or ``not-real``, every one
carrying :class:`Provenance` (a fix commit, a maintainer-confirmed thread, or a
banked Adjudication). It lives IN the repo (``lab/fixture.toml``), reviewed
like code and versioned like data: ``version`` bumps whenever the label set
changes, every scored result names the version it ran against, and numbers
from different versions are never comparable (ADR-0048).

Two label populations, one file: a **confirmed** label was human-confirmed and
is scoreable ground truth; an unconfirmed one is a **candidate** (scout-mined,
awaiting the maintainer's verdict) that the scorer must EXCLUDE from metrics —
admitting opinion into the denominator is how the 3-sample coin flips of RVW02
happened. The labeling session that produces candidates is fan-out work (scout
agents mine fix-commit archaeology, a normalizer compresses, the human
confirms — the coordinator never ingests the raw bulk); this module only
defines what a label IS and how one banks.

**Banking** (:func:`bank_label` / :func:`bank_alias`) is the Adjudication
write-path: a confirmed verdict on an unmatched emission becomes a new label
(real or not-real — a banked not-real label is what makes false positives
measurable), a confirmed near-miss becomes a phrasing alias on its label; both
BUMP the version. Banking is pure (fixture in → fixture out);
:func:`save_fixture` serializes deterministically (:func:`dump_fixture`) so a
bank is a reviewable one-hunk diff. The file is regenerated on every save —
hand comments do not survive, by design: the fixture is data, its docs live in
``lab/README.md``.

No LLM touches any of this — the fixture absorbs semantics over time precisely
so the scorer (:mod:`shipit.review.scorer`) can stay deterministic forever.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..finding import Severity, parse_severity

__all__ = [
    "DEFAULT_FIXTURE_PATH",
    "FIXTURE_SCHEMA_VERSION",
    "Fixture",
    "FixtureError",
    "Label",
    "PinnedRange",
    "Provenance",
    "bank_alias",
    "bank_label",
    "dump_fixture",
    "load_fixture",
    "parse_fixture",
    "save_fixture",
]

#: Bump when the fixture FILE FORMAT changes (field set / shapes). Distinct from
#: the fixture's own ``version``, which bumps when the LABEL SET changes.
FIXTURE_SCHEMA_VERSION = 1

#: Where the fixture lives, relative to the repo root. In-repo on purpose
#: (ADR-0048): labels are reviewed like code and their history is git's.
DEFAULT_FIXTURE_PATH = Path("lab") / "fixture.toml"

#: The admissible evidence kinds (CONTEXT.md "Ground-truth label"): a commit
#: that fixed the defect, a review thread the maintainer confirmed, or a banked
#: one-time Adjudication verdict.
PROVENANCE_KINDS = ("fix-commit", "confirmed-thread", "adjudication")

#: A label's verdict vocabulary: ``real`` labels feed recall; ``not-real``
#: labels are banked refutations — matching one is a measured false positive.
VERDICTS = ("real", "not-real")


class FixtureError(ValueError):
    """A fixture file that cannot be trusted: parse or validation failure.

    Always LOUD (never a silent skip): a scorer running against a half-read
    fixture would report numbers whose denominator nobody can reproduce.
    """


@dataclass(frozen=True)
class Provenance:
    """Why a label is admitted: its evidence kind + the pointer to it.

    ``ref`` is a commit SHA for ``fix-commit``, a thread URL for
    ``confirmed-thread``, a short free-form pointer (issue/comment/date) for
    ``adjudication``.
    """

    kind: str
    ref: str


@dataclass(frozen=True)
class PinnedRange:
    """One pinned historical PR range: the unit review experiments replay.

    ``base_sha``/``head_sha`` pin exactly what a replay reviews (for an in-PR
    fixed defect the head is the ROUND-1 head — the last commit before the
    first review — else the fix would have erased its own ground truth).
    ``repo`` is the ``owner/name`` slug; ``pr`` the PR number; ``language`` and
    ``notes`` are informational (the corpus must SPAN language/size/character,
    ADR-0048, so the spread is worth recording).
    """

    id: str
    repo: str
    pr: int
    base_sha: str
    head_sha: str
    title: str = ""
    language: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Label:
    """One Ground-truth label: a located, evidenced verdict on one defect claim.

    ``lines`` is the inclusive line range at the range's PINNED head (``None``
    = file-scoped, for defects without one anchor line — e.g. a contract doc
    that lies throughout); ``aliases`` are banked alternate phrasings
    (Adjudication grows them); ``confirmed`` gates scoring — only a
    human-confirmed label enters any metric. ``severity`` uses the one 4-tier
    ladder (:class:`shipit.finding.Severity`).
    """

    id: str
    pr_id: str
    file: str
    severity: Severity
    verdict: str
    claim: str
    provenance: Provenance
    lines: tuple[int, int] | None = None
    aliases: tuple[str, ...] = ()
    confirmed: bool = False

    @property
    def texts(self) -> tuple[str, ...]:
        """Every admissible phrasing of this defect: the claim + its aliases —
        the ``texts`` the matching primitive takes (:func:`shipit.review.match.match_claim`)."""
        return (self.claim, *self.aliases)


@dataclass(frozen=True)
class Fixture:
    """The whole corpus: pinned ranges + labels + the version scores cite."""

    version: int
    prs: tuple[PinnedRange, ...] = ()
    labels: tuple[Label, ...] = ()
    schema: int = FIXTURE_SCHEMA_VERSION

    def labels_for(
        self, pr_id: str, *, confirmed_only: bool = True
    ) -> tuple[Label, ...]:
        """The labels of one pinned range — confirmed only by default (the
        scorer's view; candidates never enter a metric)."""
        return tuple(
            label
            for label in self.labels
            if label.pr_id == pr_id and (label.confirmed or not confirmed_only)
        )

    def label_by_id(self, label_id: str) -> Label:
        """The one label with ``label_id``; loud :class:`FixtureError` if absent."""
        for label in self.labels:
            if label.id == label_id:
                return label
        raise FixtureError(f"no label {label_id!r} in fixture")


def _require_str(raw: dict[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def _parse_pr(raw: Any, index: int) -> PinnedRange:
    where = f"prs[{index}]"
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: must be a table")
    pr = raw.get("pr")
    if not isinstance(pr, int) or pr <= 0:
        raise FixtureError(f"{where}: 'pr' must be a positive PR number")
    base = _require_str(raw, "base_sha", where)
    head = _require_str(raw, "head_sha", where)
    for name, sha in (("base_sha", base), ("head_sha", head)):
        if len(sha) < 7 or any(c not in "0123456789abcdef" for c in sha.lower()):
            raise FixtureError(f"{where}: {name!r} must be a hex SHA (≥7 chars)")
    return PinnedRange(
        id=_require_str(raw, "id", where),
        repo=_require_str(raw, "repo", where),
        pr=pr,
        base_sha=base.lower(),
        head_sha=head.lower(),
        title=str(raw.get("title", "")),
        language=str(raw.get("language", "")),
        notes=str(raw.get("notes", "")),
    )


def _parse_label(raw: Any, index: int, pr_ids: set[str]) -> Label:
    where = f"labels[{index}]"
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: must be a table")
    pr_id = _require_str(raw, "pr", where)
    if pr_id not in pr_ids:
        raise FixtureError(f"{where}: unknown pr {pr_id!r}")
    severity = parse_severity(raw.get("severity"))
    if severity is None:
        raise FixtureError(f"{where}: 'severity' must be one of the 4-tier ladder")
    verdict = _require_str(raw, "verdict", where)
    if verdict not in VERDICTS:
        raise FixtureError(f"{where}: 'verdict' must be one of {VERDICTS}")
    prov_raw = raw.get("provenance")
    if not isinstance(prov_raw, dict):
        raise FixtureError(f"{where}: 'provenance' table is required")
    kind = _require_str(prov_raw, "kind", f"{where}.provenance")
    if kind not in PROVENANCE_KINDS:
        raise FixtureError(
            f"{where}: provenance kind must be one of {PROVENANCE_KINDS}"
        )
    lines_raw = raw.get("lines")
    lines: tuple[int, int] | None = None
    if lines_raw is not None:
        if (
            not isinstance(lines_raw, list)
            or len(lines_raw) != 2
            or not all(isinstance(n, int) and n > 0 for n in lines_raw)
            or lines_raw[0] > lines_raw[1]
        ):
            raise FixtureError(
                f"{where}: 'lines' must be [start, end] with start ≤ end"
            )
        lines = (lines_raw[0], lines_raw[1])
    aliases_raw = raw.get("aliases", [])
    if not isinstance(aliases_raw, list) or not all(
        isinstance(a, str) for a in aliases_raw
    ):
        raise FixtureError(f"{where}: 'aliases' must be a list of strings")
    confirmed = raw.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise FixtureError(f"{where}: 'confirmed' must be a bool")
    return Label(
        id=_require_str(raw, "id", where),
        pr_id=pr_id,
        file=_require_str(raw, "file", where),
        severity=severity,
        verdict=verdict,
        claim=_require_str(raw, "claim", where),
        provenance=Provenance(
            kind=kind, ref=_require_str(prov_raw, "ref", f"{where}.provenance")
        ),
        lines=lines,
        aliases=tuple(aliases_raw),
        confirmed=confirmed,
    )


def parse_fixture(data: dict[str, Any]) -> Fixture:
    """Parsed TOML → validated :class:`Fixture`. PURE; loud on any defect.

    Validates the full contract here — unique ids, labels referencing pinned
    ranges, the severity ladder, verdict + provenance vocabularies, sane line
    ranges — so every consumer downstream (scorer, banking, tests) can trust a
    :class:`Fixture` unconditionally.
    """
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise FixtureError("fixture 'version' must be a positive integer")
    schema = data.get("schema", FIXTURE_SCHEMA_VERSION)
    if schema != FIXTURE_SCHEMA_VERSION:
        raise FixtureError(
            f"fixture schema {schema!r} != supported {FIXTURE_SCHEMA_VERSION} — "
            "this shipit is too old or the file too new"
        )
    prs = tuple(_parse_pr(raw, i) for i, raw in enumerate(data.get("prs", [])))
    pr_ids = {p.id for p in prs}
    if len(pr_ids) != len(prs):
        raise FixtureError("duplicate pr ids in fixture")
    labels = tuple(
        _parse_label(raw, i, pr_ids) for i, raw in enumerate(data.get("labels", []))
    )
    label_ids = [label.id for label in labels]
    if len(set(label_ids)) != len(label_ids):
        raise FixtureError("duplicate label ids in fixture")
    return Fixture(version=version, prs=prs, labels=labels, schema=schema)


def load_fixture(path: Path) -> Fixture:
    """Read + validate the fixture file at ``path``. The one read boundary."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise FixtureError(f"no fixture at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise FixtureError(f"fixture {path} is not valid TOML: {exc}") from exc
    return parse_fixture(data)


# --- banking: the Adjudication write-path (pure core) -------------------------


def bank_label(fixture: Fixture, label: Label) -> Fixture:
    """Bank one adjudicated verdict as a new label; the version bumps.

    The unmatched-emission flow (ADR-0048): the human confirmed the emission is
    ``real`` (a defect the corpus did not know) or ``not-real`` (a banked
    refutation that makes the same false positive measurable forever after).
    A banked label arrives ``confirmed=True`` by definition — Adjudication IS
    the confirmation. Duplicate ids and unknown pr ids are loud.
    """
    if any(existing.id == label.id for existing in fixture.labels):
        raise FixtureError(f"label id {label.id!r} already banked")
    if label.pr_id not in {p.id for p in fixture.prs}:
        raise FixtureError(f"label {label.id!r} names unknown pr {label.pr_id!r}")
    banked = replace(label, confirmed=True)
    return replace(
        fixture, version=fixture.version + 1, labels=(*fixture.labels, banked)
    )


def bank_alias(fixture: Fixture, label_id: str, alias: str) -> Fixture:
    """Bank one adjudicated near-miss phrasing as an alias; the version bumps.

    The near-miss flow (ADR-0048): right file, overlapping lines, wording the
    lexicon did not know — the human confirmed it names the same defect, so the
    phrasing joins the label's admissible texts and matches forever after.
    """
    alias = alias.strip()
    if not alias:
        raise FixtureError("alias must be non-empty")
    label = fixture.label_by_id(label_id)
    if alias in label.texts:
        raise FixtureError(f"alias already admissible on {label_id!r}")
    updated = replace(label, aliases=(*label.aliases, alias))
    labels = tuple(updated if lb.id == label_id else lb for lb in fixture.labels)
    return replace(fixture, version=fixture.version + 1, labels=labels)


# --- deterministic serialization ----------------------------------------------


def _toml_str(value: str) -> str:
    """One TOML basic string. ``json.dumps`` escaping is valid TOML basic-string
    escaping (same ``\\"``/``\\\\``/control-char rules), so reuse it."""
    return json.dumps(value)


def dump_fixture(fixture: Fixture) -> str:
    """The fixture as canonical TOML text. PURE, deterministic (ADR-0048's
    free-to-re-run property applied to the write side: same fixture, same
    bytes — so a bank is a minimal reviewable diff). Field order is fixed;
    entries keep their banked order (append-only history reads naturally)."""
    out: list[str] = [
        "# Ground-truth fixture — versioned corpus for the deterministic review",
        "# scorer (ADR-0048). Format + banking flow: lab/README.md.",
        "# GENERATED-CANONICAL: edit via `shipit eval bank` (or edit + re-save);",
        "# hand comments do not survive a save.",
        "",
        f"schema = {fixture.schema}",
        f"version = {fixture.version}",
    ]
    for pr in fixture.prs:
        out += [
            "",
            "[[prs]]",
            f"id = {_toml_str(pr.id)}",
            f"repo = {_toml_str(pr.repo)}",
            f"pr = {pr.pr}",
            f"base_sha = {_toml_str(pr.base_sha)}",
            f"head_sha = {_toml_str(pr.head_sha)}",
        ]
        if pr.title:
            out.append(f"title = {_toml_str(pr.title)}")
        if pr.language:
            out.append(f"language = {_toml_str(pr.language)}")
        if pr.notes:
            out.append(f"notes = {_toml_str(pr.notes)}")
    for label in fixture.labels:
        out += [
            "",
            "[[labels]]",
            f"id = {_toml_str(label.id)}",
            f"pr = {_toml_str(label.pr_id)}",
            f"file = {_toml_str(label.file)}",
        ]
        if label.lines is not None:
            out.append(f"lines = [{label.lines[0]}, {label.lines[1]}]")
        out += [
            f"severity = {_toml_str(label.severity.value)}",
            f"verdict = {_toml_str(label.verdict)}",
            f"confirmed = {'true' if label.confirmed else 'false'}",
            f"claim = {_toml_str(label.claim)}",
        ]
        if label.aliases:
            aliases = ", ".join(_toml_str(a) for a in label.aliases)
            out.append(f"aliases = [{aliases}]")
        out += [
            "[labels.provenance]",
            f"kind = {_toml_str(label.provenance.kind)}",
            f"ref = {_toml_str(label.provenance.ref)}",
        ]
    return "\n".join(out) + "\n"


def save_fixture(fixture: Fixture, path: Path) -> None:
    """Serialize + write; the one write boundary (creates parents).

    Round-trips the serialization through the parser FIRST, so a
    programmatically-built fixture (the banking verbs assemble
    :class:`Label`\\ s from CLI args) obeys the exact same contract a
    hand-written file does — one validation rule set, not two — and an invalid
    bank can never reach disk.
    """
    text = dump_fixture(fixture)
    parse_fixture(tomllib.loads(text))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
