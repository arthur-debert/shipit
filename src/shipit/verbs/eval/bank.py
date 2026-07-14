"""``shipit eval bank`` — bank an adjudicated verdict into the Ground-truth fixture.

The Adjudication write-side (ADR-0048, RVW03-WS06): the scorer's adjudication
report surfaces near-misses and unmatched emissions; a human rules on each ONE
time, and this verb banks the verdict — ``bank label`` for an emission the
corpus did not know (``--verdict real`` grows recall's denominator,
``--verdict not-real`` makes that false positive measurable forever after;
``--defect FAMILY`` banks it as another valid anchor of an already-banked
defect — the equivalence family counts once for recall, #751),
``bank alias`` for a near-miss confirmed to be a known label in new words.
Either way the fixture VERSION BUMPS (:mod:`shipit.review.groundtruth` owns
the rules), the file is rewritten canonically, and the diff is reviewed like
code — the human confirmation THIS verb records is what admits a label into
any metric.

Thin boundary over the pure banking core: parse CLI args into a
:class:`~shipit.review.groundtruth.Label` / alias, call
:func:`~shipit.review.groundtruth.bank_label` /
:func:`~shipit.review.groundtruth.bank_alias`, save. Every domain violation
(duplicate id, unknown pr, bad vocabulary) is a loud exit 1 from the domain's
own :class:`~shipit.review.groundtruth.FixtureError`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ...finding import parse_severity
from ...review import groundtruth as gt

_FIXTURE_OPT = click.option(
    "--fixture",
    "fixture_path",
    default=None,
    help="Fixture file (default: lab/fixture.toml under the current directory).",
)


def _load(fixture_path: str | None) -> tuple[gt.Fixture, Path]:
    path = Path(fixture_path) if fixture_path else gt.DEFAULT_FIXTURE_PATH
    return gt.load_fixture(path), path


@click.group(name="bank")
def group() -> None:
    """Bank one adjudicated verdict into the Ground-truth fixture (bumps its version)."""


@group.command(name="label")
@_FIXTURE_OPT
@click.option("--id", "label_id", required=True, help="New unique label id.")
@click.option(
    "--pr", "pr_id", required=True, help="Pinned range id the label belongs to."
)
@click.option(
    "--file", "file_", required=True, help="Repo-relative file the claim points at."
)
@click.option(
    "--lines", default=None, help="Inclusive line range START:END (omit = file-scoped)."
)
@click.option(
    "--severity",
    required=True,
    help="critical|major|minor|nit (the one 4-tier ladder).",
)
@click.option(
    "--verdict",
    required=True,
    type=click.Choice(gt.VERDICTS),
    help="The adjudicated verdict.",
)
@click.option("--claim", required=True, help="One sentence: mechanism → consequence.")
@click.option(
    "--provenance",
    required=True,
    help="KIND:REF — kind one of fix-commit|confirmed-thread|adjudication; "
    "ref a commit SHA, thread URL, or adjudication pointer.",
)
@click.option(
    "--defect",
    default=None,
    help="Equivalence-family id when this label is another valid anchor of an "
    "already-banked defect (family members count once for recall).",
)
def label_cmd(
    fixture_path: str | None,
    label_id: str,
    pr_id: str,
    file_: str,
    lines: str | None,
    severity: str,
    verdict: str,
    claim: str,
    provenance: str,
    defect: str | None,
) -> None:
    """Bank an adjudicated emission as a NEW label (real or not-real)."""
    sev = parse_severity(severity)
    if sev is None:
        print(
            f"error: severity must be one of the 4-tier ladder, not {severity!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    kind, sep, ref = provenance.partition(":")
    if not sep:
        print("error: --provenance must be KIND:REF", file=sys.stderr)
        raise SystemExit(1)
    line_range: tuple[int, int] | None = None
    if lines is not None:
        try:
            lo, _, hi = lines.partition(":")
            line_range = (int(lo), int(hi))
        except ValueError:
            print("error: --lines must be START:END integers", file=sys.stderr)
            raise SystemExit(1) from None
    try:
        fixture, path = _load(fixture_path)
        label = gt.Label(
            id=label_id,
            pr_id=pr_id,
            file=file_,
            severity=sev,
            verdict=verdict,
            claim=claim,
            provenance=gt.Provenance(kind=kind.strip(), ref=ref.strip()),
            lines=line_range,
            confirmed=True,
            defect=defect,
        )
        banked = gt.bank_label(fixture, label)
        gt.save_fixture(banked, path)
    except gt.FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"banked label {label_id!r} ({verdict}); fixture is now v{banked.version}")


@group.command(name="alias")
@_FIXTURE_OPT
@click.argument("label_id")
@click.option(
    "--text", required=True, help="The adjudicated near-miss phrasing to admit."
)
def alias_cmd(fixture_path: str | None, label_id: str, text: str) -> None:
    """Bank an adjudicated near-miss phrasing as an ALIAS on LABEL_ID."""
    try:
        fixture, path = _load(fixture_path)
        banked = gt.bank_alias(fixture, label_id, text)
        gt.save_fixture(banked, path)
    except gt.FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"banked alias on {label_id!r}; fixture is now v{banked.version}")
