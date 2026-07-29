"""The versioned, in-repo Ground-truth fixture review experiments are scored against.

See docs/adr/0048-ground-truth-fixture-deterministic-scorer.md.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..finding import Severity, parse_severity
from ..identity import repo_from_slug

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

#: Where the fixture lives, relative to the repo root.
DEFAULT_FIXTURE_PATH = Path("lab") / "fixture.toml"

#: The admissible evidence kinds behind a label.
PROVENANCE_KINDS = ("fix-commit", "confirmed-thread", "adjudication")

#: A label's verdict vocabulary: ``real`` feeds recall, ``not-real`` is a banked
#: refutation whose match is a measured false positive.
VERDICTS = ("real", "not-real")


class FixtureError(ValueError):
    """A fixture file that cannot be trusted: parse or validation failure."""


@dataclass(frozen=True)
class Provenance:
    """Why a label is admitted: its evidence ``kind`` + a ``ref`` pointer of that kind."""

    kind: str
    ref: str


@dataclass(frozen=True)
class PinnedRange:
    """One pinned PR range; for an in-PR fixed defect the head must be the round-1 head."""

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
    """A located, evidenced verdict on one defect claim; only ``confirmed`` ones score."""

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
    defect: str | None = None

    @property
    def texts(self) -> tuple[str, ...]:
        """Every admissible phrasing of this defect: the claim + its aliases."""
        return (self.claim, *self.aliases)

    @property
    def defect_key(self) -> str:
        """The identity recall counts under: the family id when set, else the label id."""
        return self.defect or self.id


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
        """The labels of one pinned range — confirmed only by default."""
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


def _optional_str(raw: dict[str, Any], key: str, where: str) -> str:
    """An optional informational field: absent is ``""``; a non-string is a loud defect."""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FixtureError(f"{where}: {key!r} must be a string")
    return value


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
    raw_repo = _require_str(raw, "repo", where)
    try:
        # Store the canonical (lowercased) slug so two pins differing only in
        # case share one identity, rather than double-counting one store.
        parsed = repo_from_slug(raw_repo)
    except ValueError as exc:
        raise FixtureError(f"{where}: {exc}") from exc
    return PinnedRange(
        id=_require_str(raw, "id", where),
        repo=f"{parsed.owner.login}/{parsed.name}",
        pr=pr,
        base_sha=base.lower(),
        head_sha=head.lower(),
        title=_optional_str(raw, "title", where),
        language=_optional_str(raw, "language", where),
        notes=_optional_str(raw, "notes", where),
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
    defect_raw = raw.get("defect")
    defect: str | None = None
    if defect_raw is not None:
        if not isinstance(defect_raw, str) or not defect_raw.strip():
            raise FixtureError(f"{where}: 'defect' must be a non-empty string")
        defect = defect_raw.strip()
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
        defect=defect,
    )


def _validate_defect_families(labels: tuple[Label, ...]) -> None:
    """Check each ``defect`` family is one pinned range, one verdict, one severity."""
    by_id = {label.id: label for label in labels}
    first_of: dict[str, Label] = {}
    for label in labels:
        if label.defect is None:
            continue
        colliding = by_id.get(label.defect)
        if colliding is not None and colliding.defect != label.defect:
            raise FixtureError(
                f"defect family {label.defect!r} collides with label id "
                f"{colliding.id!r}, but that label does not explicitly join "
                "the family"
            )
        first = first_of.setdefault(label.defect, label)
        for field, mismatched in (
            ("pr", label.pr_id != first.pr_id),
            ("verdict", label.verdict != first.verdict),
            ("severity", label.severity is not first.severity),
        ):
            if mismatched:
                raise FixtureError(
                    f"defect family {label.defect!r}: labels {first.id!r} and "
                    f"{label.id!r} disagree on {field} — one defect is one "
                    "pinned range, one verdict, one severity tier"
                )


def parse_fixture(data: dict[str, Any]) -> Fixture:
    """Parsed TOML → validated :class:`Fixture`; pure, and loud on any defect."""
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
    _validate_defect_families(labels)
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


def bank_label(fixture: Fixture, label: Label) -> Fixture:
    """Bank one adjudicated verdict as a new confirmed label; the version bumps."""
    if any(existing.id == label.id for existing in fixture.labels):
        raise FixtureError(f"label id {label.id!r} already banked")
    if label.pr_id not in {p.id for p in fixture.prs}:
        raise FixtureError(f"label {label.id!r} names unknown pr {label.pr_id!r}")
    defect = label.defect
    if defect is not None:
        if not isinstance(defect, str) or not defect.strip():
            raise FixtureError("defect must be a non-empty string")
        defect = defect.strip()
    banked = replace(label, confirmed=True, defect=defect)
    _validate_defect_families((*fixture.labels, banked))
    return replace(
        fixture, version=fixture.version + 1, labels=(*fixture.labels, banked)
    )


def bank_alias(fixture: Fixture, label_id: str, alias: str) -> Fixture:
    """Bank one adjudicated near-miss phrasing as an alias; the version bumps."""
    alias = alias.strip()
    if not alias:
        raise FixtureError("alias must be non-empty")
    label = fixture.label_by_id(label_id)
    if alias in label.texts:
        raise FixtureError(f"alias already admissible on {label_id!r}")
    updated = replace(label, aliases=(*label.aliases, alias))
    labels = tuple(updated if lb.id == label_id else lb for lb in fixture.labels)
    return replace(fixture, version=fixture.version + 1, labels=labels)


def _toml_str(value: str) -> str:
    """One TOML basic string; TOML also forbids the literal DEL that JSON permits."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def dump_fixture(fixture: Fixture) -> str:
    """The fixture as canonical TOML text; pure and deterministic."""
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
        if label.defect is not None:
            out.append(f"defect = {_toml_str(label.defect)}")
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
    """Serialize + write, re-parsing first so an invalid fixture never reaches disk."""
    text = dump_fixture(fixture)
    parse_fixture(tomllib.loads(text))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace, so a crash mid-write never leaves the corpus truncated.
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
