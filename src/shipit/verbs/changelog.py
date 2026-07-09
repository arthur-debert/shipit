"""`shipit changelog` — the release-notes Tool's effectful shell (TOL01-WS06 #554).

The verb fronts the pure core (:mod:`shipit.changelog` — discovery
classification, coalescing, rendering, the sync diff) with the one effectful
layer: resolving the changelog root (a git read through the adapter over the
one Exec seam, ADR-0028), reading ``CHANGELOG/`` bodies, and writing the
rendered projection / cut section / notes file. Three subcommands, one model
(docs/dev/workflows.lex §4):

* ``check`` — the FRAGMENT-SYNC CHECK (PRD story 18): re-render ``CHANGELOG.md``
  from ``CHANGELOG/`` and diff against the committed file. A PR that edits the
  changelog without a fragment, or adds a fragment without re-rendering, fails
  here BEFORE merge, with the diff surfaced. This is the invocation the
  ``changelog-sync`` Lane declares (``.shipit.toml [lanes]``,
  :data:`shipit.config.CHANGELOG_SYNC_LANE`) — WS05's planner routes it in CI
  and the identical command runs on a laptop: one definition, enforced
  everywhere.
* ``render`` — regenerate ``CHANGELOG.md`` from ``CHANGELOG/*`` (the fix for a
  failing ``check``).
* ``coalesce VERSION`` — the cut-time face (story 26): refuse an empty release,
  coalesce the fragments into the supplied version's section, and emit ONE
  notes text for both the tag annotation and the GitHub release. The version
  is a supplied bare semver (ADR-0041) — bump words resolve in TOL02's version
  resolver, never here. A prerelease version EXTRACTS the notes without
  consuming the fragments; a final version rolls them. TOL02's prepare stage
  consumes the same behavior through the core API
  (:func:`shipit.changelog.plan_coalesce`), so the release plumbing (bump, tag,
  publish) stays out of this verb.

Exit contract (story 8, ADR-0030): 0 success/synced, 1 runtime refusal or a
failing check (one ``error: …`` line via the shared :func:`~._errors.cli_errors`
shell, or the check report + diff), 2 usage (click's).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click

from .. import changelog as core
from .. import git
from ..changelog import ChangelogError
from ._errors import cli_errors

logger = logging.getLogger("shipit.changelog")


# --------------------------------------------------------------------------
# The filesystem + git boundary (injected in tests)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangelogTree:
    """Everything the core needs, read once from a changelog root.

    ``has_dir`` is whether ``CHANGELOG/`` exists at all (absent → the model is
    not set up, a hard refusal — fragments are the DECLARED model, so there is
    no legacy skip-when-missing nicety here). ``committed`` is the current
    ``CHANGELOG.md`` text, ``None`` when the file does not exist.
    """

    root: Path
    has_dir: bool
    fragments: tuple[core.Fragment, ...]
    sections: dict[str, str]
    legacy: str | None
    committed: str | None
    invalid: tuple[str, ...]


def _resolve_root(start: Path, *, repo_root: Callable[..., str | None]) -> Path:
    """The changelog root for ``start``: the first ancestor (including
    ``start``) carrying a ``CHANGELOG/`` directory, else the git working-tree
    root (the one ``rev-parse`` boundary, through the git adapter's Exec seam),
    else ``start`` itself.

    The ancestor walk keeps monorepo sub-changelogs addressable (run next to
    the one you mean); the git fallback makes the repo-root convention work
    from any subdirectory of a repo that has not adopted the model yet (so the
    refusal names the right place to create ``CHANGELOG/``).
    """
    for candidate in (start, *start.parents):
        if (candidate / core.CHANGELOG_DIR).is_dir():
            return candidate
    top = repo_root(cwd=str(start))
    return Path(top) if top else start


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_tree(root: Path) -> ChangelogTree:
    """Read and classify the ``CHANGELOG/`` tree under ``root``.

    Discovery is the DIRECTORY listing, not the git index: the check's verdict
    must cover the working tree an author is about to commit (a just-written
    fragment counts before ``git add``), and by CI time listing and index
    agree. Classification itself is the pure :func:`shipit.changelog.classify_dir`.
    """
    changelog_dir = root / core.CHANGELOG_DIR
    if not changelog_dir.is_dir():
        return ChangelogTree(
            root=root,
            has_dir=False,
            fragments=(),
            sections={},
            legacy=None,
            committed=None,
            invalid=(),
        )
    listing = core.classify_dir(p.name for p in changelog_dir.iterdir() if p.is_file())
    fragments = tuple(
        core.Fragment(name=name, body=_read_text(changelog_dir / name))
        for name in listing.fragments
    )
    sections = {
        stem: _read_text(changelog_dir / f"{stem}{core.FRAGMENT_SUFFIX}")
        for stem in listing.versions
    }
    legacy_path = changelog_dir / "legacy.md"
    legacy = _read_text(legacy_path) if legacy_path.is_file() else None
    committed_path = root / core.CHANGELOG_FILE
    committed = _read_text(committed_path) if committed_path.is_file() else None
    return ChangelogTree(
        root=root,
        has_dir=True,
        fragments=fragments,
        sections=sections,
        legacy=legacy,
        committed=committed,
        invalid=listing.invalid,
    )


def _require_model(tree: ChangelogTree) -> None:
    """Refuse a tree the model cannot answer for: no ``CHANGELOG/`` directory
    (fragments are the declared model — set it up rather than skip), or
    unparseable version filenames (a mis-named section would silently vanish
    from the render, so it is loud instead)."""
    if not tree.has_dir:
        raise ChangelogError(
            f"no {core.CHANGELOG_DIR}/ directory under {tree.root} — the "
            "changelog model keeps one fragment per PR in "
            f"{core.CHANGELOG_DIR}/{core.FRAGMENT_PREFIX}<slug>{core.FRAGMENT_SUFFIX}; "
            "create the directory and a first fragment to adopt it"
        )
    if tree.invalid:
        raise ChangelogError(
            f"unparseable version filename(s) in {core.CHANGELOG_DIR}/: "
            f"{' '.join(tree.invalid)} (expected bare-semver "
            f"<version>{core.FRAGMENT_SUFFIX}, no 'v' prefix)"
        )


def _today() -> str:
    """The cut date stamped into a rolled section (UTC, ``YYYY-MM-DD``)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# The verb runners (click-free, seams injectable — the testable surface)
# --------------------------------------------------------------------------


@cli_errors
def run_check(
    path: str | None = None,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    repo_root: Callable[..., str | None] | None = None,
) -> int:
    """The fragment-sync check: 0 when the committed ``CHANGELOG.md`` equals a
    re-render of ``CHANGELOG/``, else the diff + 1.

    The exact invocation the ``changelog-sync`` Lane runs in CI and a laptop
    runs directly — one definition (story 18). The remediation is always the
    same and is printed with the failure: ``shipit changelog render``, commit.
    """
    read_tree = read_tree or _read_tree
    root = _resolve_root(
        Path(path or ".").resolve(), repo_root=repo_root or git.repo_root
    )
    tree = read_tree(root)
    _require_model(tree)
    rendered = core.render(tree.fragments, tree.sections, legacy=tree.legacy)
    diff = core.sync_diff(rendered, tree.committed)
    if diff is None:
        print(
            f"changelog: OK — {core.CHANGELOG_FILE} matches "
            f"{core.CHANGELOG_DIR}/ ({len(tree.fragments)} unreleased fragment"
            f"{'s' if len(tree.fragments) != 1 else ''})"
        )
        logger.info(
            "changelog check passed",
            extra={"root": str(root), "fragments": len(tree.fragments)},
        )
        return 0
    print(
        f"changelog: FAILED — {core.CHANGELOG_FILE} does not match a re-render "
        f"of {core.CHANGELOG_DIR}/ (a fragment added without re-rendering, or "
        "the changelog edited without a fragment)"
    )
    print(diff, end="" if diff.endswith("\n") else "\n")
    print("fix: run `shipit changelog render` and commit the result")
    logger.info(
        "changelog check failed",
        extra={"root": str(root), "fragments": len(tree.fragments)},
    )
    return 1


@cli_errors
def run_render(
    path: str | None = None,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    repo_root: Callable[..., str | None] | None = None,
) -> int:
    """Regenerate ``CHANGELOG.md`` from ``CHANGELOG/*`` (the projection write —
    and the fix a failing ``check`` names)."""
    read_tree = read_tree or _read_tree
    root = _resolve_root(
        Path(path or ".").resolve(), repo_root=repo_root or git.repo_root
    )
    tree = read_tree(root)
    _require_model(tree)
    rendered = core.render(tree.fragments, tree.sections, legacy=tree.legacy)
    (root / core.CHANGELOG_FILE).write_text(rendered, encoding="utf-8")
    print(
        f"changelog: rendered {core.CHANGELOG_FILE} "
        f"({len(tree.fragments)} unreleased fragment"
        f"{'s' if len(tree.fragments) != 1 else ''}, "
        f"{len(tree.sections)} version section"
        f"{'s' if len(tree.sections) != 1 else ''})"
    )
    logger.info(
        "changelog rendered",
        extra={
            "root": str(root),
            "fragments": len(tree.fragments),
            "versions": len(tree.sections),
        },
    )
    return 0


@cli_errors
def run_coalesce(
    version: str,
    path: str | None = None,
    *,
    notes_out: str | None = None,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    repo_root: Callable[..., str | None] | None = None,
    today: Callable[[], str] | None = None,
) -> int:
    """Cut ``version``: plan pure, then execute — write the section, consume
    the fragments, re-render the projection, and emit THE one notes text.

    The notes destination: ``--notes-out FILE`` writes the text (the artifact
    the tag annotation and GH release consume) with the report on stdout;
    without it the notes print VERBATIM to stdout (pipe-able) and the report
    moves to stderr, so stdout is exactly the one text either way it is asked
    for. A prerelease extracts (nothing written, fragments kept); a resume of
    an already-cut version re-emits the identical notes (ADR-0009).

    An unusable ``--notes-out`` (unwritable parent, a directory) is rejected as
    a :class:`ChangelogError` BEFORE the tree is mutated, so a bad destination
    never leaves a cut tree with no notes artifact written.
    """
    read_tree = read_tree or _read_tree
    root = _resolve_root(
        Path(path or ".").resolve(), repo_root=repo_root or git.repo_root
    )
    tree = read_tree(root)
    _require_model(tree)
    plan = core.plan_coalesce(
        version,
        tree.fragments,
        date=(today or _today)(),
        existing_section=tree.sections.get(version),
    )

    report = sys.stdout if notes_out else sys.stderr
    changelog_dir = root / core.CHANGELOG_DIR

    # Fail BEFORE mutating the tree when the notes destination is unusable:
    # coalesce's contract is that the cut and its one notes artifact land
    # together (story 26), so a bad ``--notes-out`` must not leave a cut tree
    # (section written, fragments gone, CHANGELOG.md re-rendered) with no notes.
    notes_path = Path(notes_out) if notes_out else None
    if notes_path is not None:
        try:
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            if notes_path.is_dir():
                raise OSError(f"{notes_out} is a directory")
        except OSError as exc:
            raise ChangelogError(f"cannot write notes to {notes_out}: {exc}") from exc

    if plan.section is not None:
        section_path = changelog_dir / f"{plan.version}{core.FRAGMENT_SUFFIX}"
        section_path.write_text(plan.section, encoding="utf-8")
        for name in plan.consumed:
            (changelog_dir / name).unlink()
        # Re-render the projection from the POST-cut tree so the committed
        # CHANGELOG.md and the fragment dir move in one step (the sync check
        # stays green on the cut commit).
        after = read_tree(root)
        _require_model(after)
        rendered = core.render(after.fragments, after.sections, legacy=after.legacy)
        (root / core.CHANGELOG_FILE).write_text(rendered, encoding="utf-8")
        print(
            f"changelog: coalesced {len(plan.consumed)} fragment"
            f"{'s' if len(plan.consumed) != 1 else ''} into "
            f"{core.CHANGELOG_DIR}/{plan.version}{core.FRAGMENT_SUFFIX} and "
            f"re-rendered {core.CHANGELOG_FILE}",
            file=report,
        )
    elif plan.prerelease:
        print(
            f"changelog: prerelease {plan.version} — notes extracted, "
            f"{len(tree.fragments)} unreleased fragment"
            f"{'s' if len(tree.fragments) != 1 else ''} kept for the final",
            file=report,
        )
    else:
        print(
            f"changelog: {plan.version} already cut — re-emitting its notes (resume)",
            file=report,
        )

    if notes_path is not None:
        try:
            notes_path.write_text(plan.notes, encoding="utf-8")
        except OSError as exc:
            raise ChangelogError(f"cannot write notes to {notes_out}: {exc}") from exc
        print(f"changelog: notes -> {notes_out}", file=report)
    else:
        sys.stdout.write(plan.notes)
    logger.info(
        "changelog coalesce",
        extra={
            "root": str(root),
            "version": plan.version,
            "prerelease": plan.prerelease,
            "consumed": len(plan.consumed),
            "mutated": plan.mutates,
        },
    )
    return 0


# --------------------------------------------------------------------------
# Click glue
# --------------------------------------------------------------------------


@click.group(name="changelog")
def changelog() -> None:
    """The language-agnostic release-notes tool over CHANGELOG/ fragments.

    Release notes accumulate as CHANGELOG/unreleased-*.md fragments, one per
    feature/fix PR; CHANGELOG.md is rendered from them, never hand-edited.
    `check` is the PR-time fragment-sync check (the changelog-sync lane);
    `coalesce` is the cut-time roll that emits the one release-notes text.
    """


@changelog.command(name="check")
@click.argument("path", required=False)
def check_cmd(path: str | None) -> None:
    """Fail unless CHANGELOG.md matches a re-render of CHANGELOG/.

    The fragment-sync check: a fragment added without re-rendering, or a
    hand-edited CHANGELOG.md without a fragment, fails with the diff. The
    same invocation runs in the changelog-sync lane and on a laptop.
    """
    raise SystemExit(run_check(path))


@changelog.command(name="render")
@click.argument("path", required=False)
def render_cmd(path: str | None) -> None:
    """Regenerate CHANGELOG.md from CHANGELOG/* (the fix for a failing check)."""
    raise SystemExit(run_render(path))


@changelog.command(name="coalesce")
@click.argument("version")
@click.argument("path", required=False)
@click.option(
    "--notes-out",
    type=click.Path(dir_okay=False),
    help=(
        "Write the coalesced release-notes text to FILE (the one text the tag "
        "annotation and the GitHub release both consume). Without it the notes "
        "print verbatim to stdout."
    ),
)
def coalesce_cmd(version: str, path: str | None, notes_out: str | None) -> None:
    """Cut VERSION: coalesce the unreleased fragments and emit the notes.

    VERSION is a supplied bare semver (never inferred; bump words resolve in
    the release pipeline, not here). Zero fragments is a hard refusal. A
    prerelease version (e.g. 1.2.3-rc.1) extracts the notes without consuming
    the fragments; a final version rolls them into CHANGELOG/VERSION.md and
    re-renders CHANGELOG.md.
    """
    raise SystemExit(run_coalesce(version, path, notes_out=notes_out))
