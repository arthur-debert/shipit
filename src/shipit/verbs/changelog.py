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
* ``check-fragment`` — the PR-TIME FRAGMENT GATE (issue #1073): fail a PR
  merging to ``main`` that adds no ``CHANGELOG/unreleased-*.md`` fragment, with
  a ``Changelog: skip`` commit-trailer escape hatch for docs/CI/chore-only PRs.
  The cut-time empty-release refusal (:func:`shipit.changelog.plan_coalesce`)
  fires only once every merged PR is in, so this per-PR gate catches the miss
  where a fragment can still be added. Self-gating and offline: the base ref is
  read from the CI runner env and everything else — the added fragment and the
  skip trailer — from the PR's own git, so no ``gh`` auth and no CI event
  trigger is involved. The pure decision is :func:`decide_fragment_gate`.
* ``render`` — regenerate ``CHANGELOG.md`` from ``CHANGELOG/*`` (the fix for a
  failing ``check``).
* ``coalesce VERSION`` — the cut-time face (story 26): refuse an empty release,
  coalesce the fragments into the supplied version's section, and emit ONE
  notes text for both the tag annotation and the GitHub release. The version
  is a supplied bare semver (ADR-0041) — bump words resolve in TOL02's version
  resolver, never here. A prerelease version EXTRACTS the notes without
  consuming the fragments; a final version rolls them. The release pipeline's
  prepare stage (:mod:`shipit.verbs.release`) consumes the same behavior
  through this module's :func:`plan_cut` / :func:`apply_cut` pair over the
  core API (:func:`shipit.changelog.plan_coalesce`), so the release plumbing
  (bump, tag, publish) stays out of this verb.

Exit contract (story 8, ADR-0030): 0 success/synced, 1 runtime refusal or a
failing check (one ``error: …`` line via the shared :func:`~._errors.cli_errors`
shell, or the check report + diff), 2 usage (click's).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import click

from .. import changelog as core
from .. import git
from ..changelog import ChangelogError
from ._errors import cli_errors

logger = logging.getLogger("shipit.changelog")


@contextmanager
def _fs_mutation(action: str) -> Iterator[None]:
    """Map a filesystem ``OSError`` to the uniform :class:`ChangelogError`
    surface (ADR-0030): a write/unlink against a read-only tree or file exits 1
    with one ``error: …`` line, never a raw traceback (``OSError`` is NOT in
    ``KNOWN_ERRORS``). ``action`` names the failed operation for the message.
    """
    try:
        yield
    except OSError as exc:
        raise ChangelogError(f"{action}: {exc}") from exc


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


def render_current(root: Path) -> str | None:
    """The CURRENT renderer's ``CHANGELOG.md`` text for ``root``, or ``None``
    when it cannot answer — no ``CHANGELOG/`` directory (the fragment model is
    not adopted) or unparseable version filenames (a render would silently drop
    the mis-named section).

    The install reconcile's changelog seam (TOL01-WS08 #578): a renderer change
    (a new generated-file header, a section fix) makes every consumer's
    committed projection stale against ``shipit changelog check``, and the
    reconcile PR is the sanctioned channel that refreshes it (ADR-0033) —
    :func:`shipit.install.reconcile.gather` compares this text against the
    committed file and :func:`shipit.install.apply.apply` writes it. ``None``
    means "nothing to say", never a refusal: install must not turn a repo
    without the fragment convention into an error the way ``check`` does.
    """
    tree = _read_tree(root)
    if not tree.has_dir or tree.invalid:
        return None
    return core.render(tree.fragments, tree.sections, legacy=tree.legacy)


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


def plan_cut(
    root: Path,
    version: str,
    *,
    read_tree: Callable[[Path], ChangelogTree] | None = None,
    today: Callable[[], str] | None = None,
) -> tuple[ChangelogTree, core.CoalescePlan]:
    """Read and validate the ``CHANGELOG/`` tree under ``root`` and plan the
    cut for ``version`` — the coalesce API's READ side (story 26), shared by
    ``shipit changelog coalesce`` (:func:`run_coalesce`) and the release
    pipeline's prepare stage (:mod:`shipit.verbs.release`).

    ``version`` is a supplied bare semver (ADR-0041). Raises
    :class:`ChangelogError` for a tree the model cannot answer for and for
    every :func:`shipit.changelog.plan_coalesce` refusal — notably the
    EMPTY-RELEASE refusal, which is why prepare plans the cut BEFORE touching
    any manifest: a release with no notes dies with nothing mutated.
    """
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
    """Execute a MUTATING :class:`~shipit.changelog.CoalescePlan` against the
    tree under ``root``: write the version section, consume the fragments, and
    re-render ``CHANGELOG.md`` from the post-cut tree (so the fragment-sync
    check stays green on the cut commit). A no-op for a plan that does not
    mutate (a prerelease extract, a resume).

    The ONE cut executor, shared by ``shipit changelog coalesce``
    (:func:`run_coalesce`) and the release pipeline's prepare stage
    (:mod:`shipit.verbs.release`), which consumes the coalesce API rather than
    re-implementing the roll (PRD story 26). Filesystem failures surface as
    :class:`ChangelogError` (ADR-0030), never a raw ``OSError``.
    """
    if plan.section is None:
        return
    read_tree = read_tree or _read_tree
    changelog_dir = root / core.CHANGELOG_DIR
    # Wrap the cut's filesystem mutations so an OSError (read-only checkout,
    # transient IO) exits 1 with an ``error: …`` line instead of a traceback
    # (ADR-0030). _require_model raises ChangelogError, which passes through.
    with _fs_mutation(f"cannot cut {plan.version}"):
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


# --------------------------------------------------------------------------
# The PR-time fragment gate (story: issue #1073)
# --------------------------------------------------------------------------

#: The base branch the fragment gate enforces. Only PRs to `main` are gated:
#: work-stream PRs target an epic branch and are exempt (the umbrella PR to
#: `main` carries the fragment), so the user-facing note belongs on the `main`
#: merge, not every intermediate WS merge.
GATED_BASE_REF = "main"

#: The commit trailer that opts a PR out of the gate — for docs/CI/chore-only
#: PRs that legitimately add no release note. A ``Changelog: skip`` trailer on
#: ANY commit the PR adds passes the gate unconditionally. A trailer, not a
#: GitHub label: it rides the same git the gate already reads (offline, no event
#: payload, no CI trigger), so it works on a re-run and on a laptop and never
#: churns the CI suite the way a mutable label toggle does.
SKIP_TRAILER_KEY = "Changelog"
SKIP_TRAILER_VALUE = "skip"


@dataclass(frozen=True)
class FragmentGate:
    """The pure fragment-gate verdict: ``ok`` (exit 0 when true, else 1) plus
    the human line to print either way."""

    ok: bool
    message: str


def _is_fragment_path(path: str) -> bool:
    """Whether a git-diff path names a ``CHANGELOG/`` unreleased fragment.

    Paths ride verbatim from ``git diff --name-only`` (repo-relative, forward
    slashes). The predicate reuses the core :func:`shipit.changelog.is_fragment_name`
    over the basename and requires the immediate parent to be the ``CHANGELOG/``
    directory — so a stray ``docs/unreleased-notes.md`` never counts, while a
    monorepo sub-changelog (``pkg/CHANGELOG/unreleased-x.md``) still does.
    """
    p = PurePosixPath(path)
    return p.parent.name == core.CHANGELOG_DIR and core.is_fragment_name(p.name)


def decide_fragment_gate(
    *,
    base_ref: str,
    skip_requested: Callable[[], bool],
    changed_paths: Callable[[], Sequence[str] | None],
) -> FragmentGate:
    """The PURE fragment-gate decision (issue #1073), git/env reads injected.

    Given the PR's base ref, a thunk that reports whether a ``Changelog: skip``
    trailer is present, and a thunk that yields the PR's own changed paths (the
    three-dot merge-base diff), decide whether a changelog fragment is required
    and, if so, whether one was added. BOTH thunks are only invoked on the
    branch that needs them — the skip thunk after the base short-circuits, the
    diff thunk only when the base is gated and no skip trailer is set — so a
    laptop/lefthook run (empty base) and an epic-branch PR touch NEITHER git read.

    Passes (``ok=True``) when any of:

    * ``base_ref`` is empty — not a PR context (a laptop/lefthook run), so the
      gate never blocks local work.
    * ``base_ref`` is not :data:`GATED_BASE_REF` — a WS PR to an epic branch is
      exempt.
    * ``skip_requested()`` is True — a ``Changelog: skip`` trailer
      (:data:`SKIP_TRAILER_KEY`/:data:`SKIP_TRAILER_VALUE`) is present on one of
      the PR's commits.
    * a ``CHANGELOG/unreleased-*.md`` path was ADDED in the diff. The diff is
      the merge-base ``base...HEAD`` (:func:`shipit.git.added_paths_since`,
      ``--diff-filter=A``), so a fragment the PR introduces counts even when
      amended across review rounds (the base never had it → still a net add),
      while merely modifying, deleting, or renaming a fragment already present
      on the base does NOT — that adds no new release note.

    Fails (``ok=False``) when the base is ``main``, no skip trailer is set, and
    the diff carries no fragment — or when the diff is unavailable (``None``),
    which for a required gate is a loud refusal (better than passing a PR whose
    fragment could not be verified), not a silent pass.
    """
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
    if skip_requested():
        return FragmentGate(
            True,
            f"changelog: {SKIP_TRAILER_KEY}: {SKIP_TRAILER_VALUE} trailer present "
            "— no fragment required",
        )
    paths = changed_paths()
    if paths is None:
        return FragmentGate(
            False,
            f"changelog: could not diff against origin/{GATED_BASE_REF} — "
            "cannot verify a changelog fragment was added; fetch the base "
            f"branch (or add a {SKIP_TRAILER_KEY!r}: {SKIP_TRAILER_VALUE!r} "
            "trailer to a commit)",
        )
    added = [p for p in paths if _is_fragment_path(p)]
    if added:
        shown = ", ".join(sorted(added))
        return FragmentGate(
            True,
            f"changelog: OK — fragment added ({shown})",
        )
    return FragmentGate(
        False,
        f"no {core.CHANGELOG_DIR}/{core.FRAGMENT_PREFIX}*{core.FRAGMENT_SUFFIX} "
        f"fragment added — every PR to {GATED_BASE_REF} needs one: a "
        f"{core.CHANGELOG_DIR}/{core.FRAGMENT_PREFIX}*{core.FRAGMENT_SUFFIX} "
        f"fragment (a package's own {core.CHANGELOG_DIR}/ in a monorepo counts "
        f"too), or a '{SKIP_TRAILER_KEY}: {SKIP_TRAILER_VALUE}' trailer on a "
        "commit for docs/CI/chore-only PRs",
    )


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
def run_check_fragment(
    path: str | None = None,
    *,
    base_ref: str | None = None,
    changed_paths_fn: Callable[[str, str], Sequence[str] | None] | None = None,
    skip_requested_fn: Callable[[str, str], bool | None] | None = None,
) -> int:
    """The PR-time fragment gate: 0 when no fragment is required or one was
    added, else 1 (issue #1073).

    Self-gating, offline (no ``gh`` auth), pure git. The PR context comes from
    git, not a live API call: the base ref from ``GITHUB_BASE_REF`` (empty
    off-PR), the escape hatch from a ``Changelog: skip`` commit trailer read out
    of the PR's own commits. Only PRs to :data:`GATED_BASE_REF` are gated. The
    whole decision is the pure :func:`decide_fragment_gate`; this shell only
    resolves the seams.

    The seams are injectable for tests: ``base_ref`` overrides the env read,
    ``changed_paths_fn`` (``(base_ref, cwd) -> paths | None``) and
    ``skip_requested_fn`` (``(base_ref, cwd) -> bool | None``) the two git
    boundaries — mirroring the ``ci plan`` injection pattern
    (:func:`shipit.verbs.ci.run`). Both are wrapped as THUNKS the pure decision
    calls lazily, so a laptop run (empty base) and an epic-branch PR never touch
    git. The default fragment boundary is :func:`shipit.git.added_paths_since`
    (``--diff-filter=A`` against ``origin/<base>``): only a fragment the PR ADDS
    satisfies the gate, so modifying/deleting/renaming a pre-existing base
    fragment cannot. The default skip boundary is
    :func:`shipit.git.skip_changelog_requested`; only an explicit ``True`` skips
    — an unverifiable read (``None``) falls through to the fragment requirement
    (the ``is True`` below), so a broken git read can never silently bypass the gate.
    """
    root = Path(path or ".").resolve()
    if base_ref is None:
        base_ref = os.environ.get("GITHUB_BASE_REF", "")
    fetch = changed_paths_fn or (
        lambda ref, cwd: git.added_paths_since(f"origin/{ref}", cwd=cwd)
    )
    skip_fn = skip_requested_fn or (
        lambda ref, cwd: git.skip_changelog_requested(f"origin/{ref}", cwd=cwd)
    )
    verdict = decide_fragment_gate(
        base_ref=base_ref,
        skip_requested=lambda: skip_fn(base_ref.strip(), str(root)) is True,
        changed_paths=lambda: fetch(base_ref.strip(), str(root)),
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
    """Regenerate ``CHANGELOG.md`` from ``CHANGELOG/*`` (the projection write —
    and the fix a failing ``check`` names)."""
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
    """Cut ``version``: plan pure, then execute — write the section, consume
    the fragments, re-render the projection, and emit THE one notes text.

    The notes destination: ``--notes-out FILE`` writes the text (the artifact
    the tag annotation and GH release consume) with the report on stdout;
    without it the notes print VERBATIM to stdout (pipe-able) and the report
    moves to stderr, so stdout is exactly the one text either way it is asked
    for. A prerelease extracts (nothing written, fragments kept); a resume of
    an already-cut version re-emits the identical notes (ADR-0009).

    An unusable ``--notes-out`` — unwritable parent, a directory, or a
    read-only target — is rejected as a :class:`ChangelogError` BEFORE the tree
    is mutated: its writability is probed up front. Any residual write failure
    (e.g. the disk filling between the probe and the write) still surfaces as a
    :class:`ChangelogError`, never a raw traceback — so a bad destination never
    leaves a cut tree with no notes artifact.
    """
    read_tree = read_tree or _read_tree
    root = _resolve_root(
        Path(path or ".").resolve(), repo_root=repo_root or git.repo_root
    )
    tree, plan = plan_cut(root, version, read_tree=read_tree, today=today)

    report = sys.stdout if notes_out else sys.stderr

    # Fail BEFORE mutating the tree when the notes destination is unusable:
    # coalesce's contract is that the cut and its one notes artifact land
    # together (story 26), so a bad ``--notes-out`` must not leave a cut tree
    # (section written, fragments gone, CHANGELOG.md re-rendered) with no notes.
    # Create the parent and reject a directory / unwritable target — but probe
    # write access WITHOUT creating the target file, so a cut that fails after
    # this leaves no stray empty notes file behind. Probe an existing target for
    # write; a to-be-created target needs write AND execute (search) on the
    # parent directory (POSIX). Any stat/permission failure here (e.g. a
    # non-traversable parent) is itself a pre-mutation refusal. The final write
    # happens only on success.
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


# --------------------------------------------------------------------------
# Click glue
# --------------------------------------------------------------------------


@click.group(name="changelog")
def changelog() -> None:
    """The language-agnostic release-notes tool over CHANGELOG/ fragments.

    Release notes accumulate as CHANGELOG/unreleased-*.md fragments, one per
    feature/fix PR; CHANGELOG.md is rendered from them, never hand-edited.
    `check` is the PR-time fragment-sync check (the changelog-sync lane);
    `check-fragment` is the PR-time gate that a PR to main added a fragment
    (`Changelog: skip` trailer escape hatch); `coalesce` is the cut-time roll
    that emits the one release-notes text.
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


@changelog.command(name="check-fragment")
@click.argument("path", required=False)
def check_fragment_cmd(path: str | None) -> None:
    """Fail a PR to main that adds no changelog fragment (`Changelog: skip` hatch).

    The PR-time counterpart to the cut-time empty-release refusal: it fires
    per-PR so a missing fragment is caught before merge, not at the next cut.
    Self-gating and offline — the base ref comes from the CI runner env
    (GITHUB_BASE_REF), the fragment check and the skip trailer both from the PR's
    own git. Passes with no fragment off-PR, on a non-main base, or when a commit
    on the PR carries a `Changelog: skip` trailer (docs/CI/chore-only PRs).
    """
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
    """Cut VERSION: coalesce the unreleased fragments and emit the notes.

    VERSION is a supplied bare semver (never inferred; bump words resolve in
    the release pipeline, not here). Zero fragments is a hard refusal. A
    prerelease version (e.g. 1.2.3-rc.1) extracts the notes without consuming
    the fragments; a final version rolls them into CHANGELOG/VERSION.md and
    re-renders CHANGELOG.md.
    """
    raise SystemExit(run_coalesce(version, path, notes_out=notes_out))
