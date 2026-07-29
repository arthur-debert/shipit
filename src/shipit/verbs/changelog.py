"""`shipit changelog` — the release-notes tool's effectful shell over
``CHANGELOG/`` fragments: check, check-fragment, render, coalesce.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click

from .. import changelog as core
from .. import git
from ..changelog import ChangelogError
from ._errors import cli_errors

logger = logging.getLogger("shipit.changelog")


@contextmanager
def _fs_mutation(action: str) -> Iterator[None]:
    """Map a filesystem ``OSError`` raised by ``action`` to a :class:`ChangelogError`."""
    try:
        yield
    except OSError as exc:
        raise ChangelogError(f"{action}: {exc}") from exc


@dataclass(frozen=True)
class ChangelogTree:
    """Everything the core needs, read once from a changelog root."""

    root: Path
    has_dir: bool
    fragments: tuple[core.Fragment, ...]
    sections: dict[str, str]
    legacy: str | None
    committed: str | None
    invalid: tuple[str, ...]


def _resolve_root(start: Path, *, repo_root: Callable[..., str | None]) -> Path:
    """The nearest ancestor of ``start`` holding ``CHANGELOG/``, else the repo root, else ``start``."""
    for candidate in (start, *start.parents):
        if (candidate / core.CHANGELOG_DIR).is_dir():
            return candidate
    top = repo_root(cwd=str(start))
    return Path(top) if top else start


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_tree(root: Path) -> ChangelogTree:
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


def render_current(root: Path) -> str | None:
    """The rendered ``CHANGELOG.md`` text for ``root``, or ``None`` when the tree cannot be answered for."""
    tree = _read_tree(root)
    if not tree.has_dir or tree.invalid:
        return None
    return core.render(tree.fragments, tree.sections, legacy=tree.legacy)


def _require_model(tree: ChangelogTree) -> None:
    """Raise unless ``tree`` has a ``CHANGELOG/`` directory with parseable filenames."""
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


def plan_cut(
    root: Path,
    version: str,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    today: Callable[[], str] | None = None,
) -> tuple[ChangelogTree, core.CoalescePlan]:
    """Plan the cut of ``version``; raises :class:`ChangelogError` on any refusal."""
    read_tree = read_tree or _read_tree
    tree = read_tree(root)
    _require_model(tree)
    plan = core.plan_coalesce(
        version,
        tree.fragments,
        date=(today or _today)(),
        existing_section=tree.sections.get(version),
    )
    return tree, plan


def apply_cut(
    root: Path,
    plan: core.CoalescePlan,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
) -> None:
    """Execute a mutating plan against ``root``; a no-op for a non-mutating plan."""
    if plan.section is None:
        return
    read_tree = read_tree or _read_tree
    changelog_dir = root / core.CHANGELOG_DIR
    with _fs_mutation(f"cannot cut {plan.version}"):
        section_path = changelog_dir / f"{plan.version}{core.FRAGMENT_SUFFIX}"
        section_path.write_text(plan.section, encoding="utf-8")
        for name in plan.consumed:
            (changelog_dir / name).unlink()
        after = read_tree(root)
        _require_model(after)
        rendered = core.render(after.fragments, after.sections, legacy=after.legacy)
        (root / core.CHANGELOG_FILE).write_text(rendered, encoding="utf-8")


GATED_BASE_REF = "main"


@dataclass(frozen=True)
class FragmentGate:
    """``ok`` (exit 0 when true, else 1) plus the human line to print either way."""

    ok: bool
    message: str


def decide_fragment_gate(
    *,
    base_ref: str,
    has_unreleased_fragment: Callable[[], bool],
) -> FragmentGate:
    """``has_unreleased_fragment`` is a thunk, invoked only when the base ref is gated."""
    base = base_ref.strip()
    if not base:
        return FragmentGate(True, "changelog: not a PR context — no fragment required")
    if base != GATED_BASE_REF:
        return FragmentGate(
            True,
            f"changelog: PR base {base!r} is not {GATED_BASE_REF!r} — no "
            "fragment required (only PRs to "
            f"{GATED_BASE_REF} are gated)",
        )
    if has_unreleased_fragment():
        return FragmentGate(
            True,
            f"changelog: OK — {core.CHANGELOG_DIR}/ has an unreleased fragment",
        )
    return FragmentGate(
        False,
        f"no {core.CHANGELOG_DIR}/{core.FRAGMENT_PREFIX}*{core.FRAGMENT_SUFFIX} "
        "fragment present — add one so the release has notes to cut (this is the "
        "same condition that makes 'release' refuse an empty cut)",
    )


@cli_errors
def run_check(
    path: str | None = None,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    repo_root: Callable[..., str | None] | None = None,
) -> int:
    """0 when the committed ``CHANGELOG.md`` equals a re-render of ``CHANGELOG/``, else the diff and 1."""
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
def run_check_fragment(
    path: str | None = None,
    *,
    base_ref: str | None = None,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
) -> int:
    """0 when no fragment is required or ``CHANGELOG/`` holds one, else 1."""
    read_tree = read_tree or _read_tree
    root = _resolve_root(Path(path or ".").resolve(), repo_root=git.repo_root)
    if base_ref is None:
        base_ref = os.environ.get("GITHUB_BASE_REF", "")
    verdict = decide_fragment_gate(
        base_ref=base_ref,
        has_unreleased_fragment=lambda: bool(read_tree(root).fragments),
    )
    print(verdict.message)
    logger.info(
        "changelog fragment gate %s",
        "passed" if verdict.ok else "failed",
        extra={"root": str(root), "base_ref": base_ref.strip(), "ok": verdict.ok},
    )
    return 0 if verdict.ok else 1


@cli_errors
def run_render(
    path: str | None = None,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    repo_root: Callable[..., str | None] | None = None,
) -> int:
    """Regenerate ``CHANGELOG.md`` from ``CHANGELOG/*``; returns an exit code."""
    read_tree = read_tree or _read_tree
    root = _resolve_root(
        Path(path or ".").resolve(), repo_root=repo_root or git.repo_root
    )
    tree = read_tree(root)
    _require_model(tree)
    rendered = core.render(tree.fragments, tree.sections, legacy=tree.legacy)
    with _fs_mutation(f"cannot write {core.CHANGELOG_FILE}"):
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
    """Cut ``version``: write the section, consume the fragments, emit the notes."""
    read_tree = read_tree or _read_tree
    root = _resolve_root(
        Path(path or ".").resolve(), repo_root=repo_root or git.repo_root
    )
    tree, plan = plan_cut(root, version, read_tree=read_tree, today=today)

    report = sys.stdout if notes_out else sys.stderr

    notes_path = Path(notes_out) if notes_out else None
    if notes_path is not None:
        try:
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            if notes_path.is_dir():
                raise OSError(f"{notes_out} is a directory")
            if notes_path.exists():
                writable = os.access(notes_path, os.W_OK)
            else:
                writable = os.access(notes_path.parent, os.W_OK | os.X_OK)
            if not writable:
                raise OSError(f"{notes_out} is not writable")
        except OSError as exc:
            raise ChangelogError(f"cannot write notes to {notes_out}: {exc}") from exc

    if plan.section is not None:
        apply_cut(root, plan, read_tree=read_tree)
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


@click.group(name="changelog")
def changelog() -> None:
    """The language-agnostic release-notes tool over CHANGELOG/ fragments."""


@changelog.command(name="check")
@click.argument("path", required=False)
def check_cmd(path: str | None) -> None:
    """Fail unless CHANGELOG.md matches a re-render of CHANGELOG/."""
    raise SystemExit(run_check(path))


@changelog.command(name="check-fragment")
@click.argument("path", required=False)
def check_fragment_cmd(path: str | None) -> None:
    """Fail a PR to main when CHANGELOG/ holds no unreleased fragment."""
    raise SystemExit(run_check_fragment(path))


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
    """Cut VERSION: coalesce the unreleased fragments and emit the notes."""
    raise SystemExit(run_coalesce(version, path, notes_out=notes_out))
