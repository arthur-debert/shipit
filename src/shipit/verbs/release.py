"""`shipit release` — the Release pipeline's stage verbs (TOL02, PRD story 19).

Each release stage is independently invocable; this module carries the FIRST
stage, ``shipit release prepare`` (TOL02-WS01 #559) — the pipeline's only
writer of repo history. The effectful shell over three pure cores:

- the **version resolver** (:mod:`shipit.release.version`, ADR-0041): the
  caller supplies ``<semver>`` or a bump word, parsed to a
  :class:`~shipit.release.version.VersionSpec` at the click boundary
  (ADR-0030) and resolved against the repo's existing tags. Tag exists →
  RESUME: skip the bump entirely and re-emit the tag's SHA (ADR-0009).
- the **bump-adapter registry** (:mod:`shipit.release.bump`): the tag
  decision projected into manifests, one closed entry per toolchain of the
  path→toolchain map (ADR-0007) — rust workspace-wide with the lock
  refreshed, npm's ``package.json``, python's ``pyproject.toml``, go's
  zero-file projection — plus the artifact-declared bundle-config hook
  (``tauri.conf.json``; "tauri" never enters the dispatch registry, story 25).
- the **changelog coalesce API** (story 26, consumed not rebuilt):
  :func:`shipit.verbs.changelog.plan_cut` plans BEFORE any manifest is
  touched — an empty release dies with nothing mutated — and
  :func:`~shipit.verbs.changelog.apply_cut` executes the roll; the plan's one
  notes text lands in the tag annotation and the notes file the publish
  stage reuses.

The shell's own rules:

- a CLEAN working tree is a precondition of a history-writing cut: prepare
  refuses before any mutation if a TRACKED file is modified, so no pre-existing
  edit rides the release commit or masks a no-op bump, and a ``-release-rc``
  cut's ``reset_hard`` never destroys uncommitted work. Untracked files are
  exempt (never staged, and ``reset_hard`` leaves them), and a RESUME skips the
  gate entirely (it writes no history) — a leftover notes artifact from a prior
  run never blocks it.
- every external command runs through the one Exec seam (ADR-0028): adapter
  commands via :func:`shipit.execrun.run`, git via the :mod:`shipit.git`
  adapter.
- **stage only intended files** (story 24): exactly the adapter-declared
  manifest pathspecs, the declared bundle-config files, and the changelog
  projection are staged; a bump that changes NONE of a leg's declared files
  is a hard :class:`~shipit.release.ReleaseError` — never an empty commit.
- the bump commit passes the repo's own commit/push checks — ``git commit``
  and ``git push`` run WITHOUT ``--no-verify`` (story 24: ``RELEASE_TOKEN``
  exists to satisfy the ruleset, never to skip checks); a failing hook aborts
  prepare before anything is pushed.
- a ``-release-rc`` live-fire cut is TAG-ONLY (legacy release#663): the bump
  commit travels on the tag, the branch ref is moved back and never pushed,
  so verification cuts leave the branch's version line clean. A plain
  ``-rc.N`` prerelease extracts notes without rolling the changelog but
  pushes branch + tag like a final.
- outputs are uniform and typed (ADR-0030): version, release SHA, prerelease
  flag, notes path — :class:`PrepareResult` rendered as text or ``--json``,
  so the ``wf-prepare`` block and later stages consume them without
  re-parsing.

Exit contract (ADR-0030): 0 prepared/resumed, 1 runtime refusal (via the
shared :func:`~._errors.cli_errors` shell), 2 usage (click's — including a
malformed version argument, rejected at parse by
:data:`~._params.VERSION_SPEC`).
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .. import config, execrun, git
from ..release import ReleaseError
from ..release import bump as bump_mod
from ..release import version as version_mod
from . import changelog as changelog_verb
from ._errors import cli_errors
from ._params import VERSION_SPEC, json_option
from ._render import emit
from ._tool import load_config

logger = logging.getLogger("shipit.release")

#: Each bump command Exec's stated timeout (ADR-0028): ``cargo update`` may
#: refresh the registry index over the network, so the bound is generous —
#: but a bump is never a build, so it stays well under the build verbs' hour.
BUMP_TIMEOUT: float = 600.0

#: Where the coalesced notes text lands when ``--notes-out`` is omitted:
#: repo-root-relative, TRANSIENT (written after the bump commit, never staged
#: by prepare) — the downstream stages' input, not repo content.
DEFAULT_NOTES_FILE = "RELEASE_NOTES.md"

#: The changelog projection's pathspecs — staged when the cut rolls fragments
#: (a final release): the re-rendered projection, the new version section,
#: and the consumed fragment deletions.
_CHANGELOG_STAGE: tuple[str, ...] = ("CHANGELOG.md", "CHANGELOG/*")


@dataclass(frozen=True)
class PrepareResult:
    """The prepare stage's uniform, typed output (ADR-0030).

    ``release_sha`` is the commit the tag names — the bump commit on a fresh
    cut, the EXISTING tag's commit on a resume. ``branch`` is the pushed
    branch, ``None`` on a resume (nothing pushed) and on a tag-only
    ``-release-rc`` cut (the branch ref is deliberately un-advanced).
    ``notes_path`` is where THE one coalesced notes text was written — the
    same text the tag annotation carries and the publish stage reuses.
    """

    version: str
    tag: str
    release_sha: str
    prerelease: bool
    resume: bool
    tag_only: bool
    branch: str | None
    notes_path: str

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
        return {
            "version": self.version,
            "tag": self.tag,
            "release_sha": self.release_sha,
            "prerelease": self.prerelease,
            "resume": self.resume,
            "tag_only": self.tag_only,
            "branch": self.branch,
            "notes_path": self.notes_path,
        }


def format_prepare(result: PrepareResult) -> str:
    """The text rendering of a :class:`PrepareResult`. Pure."""
    if result.resume:
        headline = (
            f"release: {result.version} already prepared — resumed "
            f"(tag {result.tag} exists; nothing bumped, nothing pushed)"
        )
    else:
        headline = f"release: prepared {result.version}"
    kind = "prerelease" if result.prerelease else "final"
    if result.resume:
        pushed = "nothing (resume)"
    elif result.tag_only:
        pushed = f"tag {result.tag} only (-release-rc: branch ref un-advanced)"
    else:
        pushed = f"{result.branch} + tag {result.tag}"
    return "\n".join(
        (
            headline,
            f"  version  {result.version} ({kind})",
            f"  sha      {result.release_sha}",
            f"  pushed   {pushed}",
            f"  notes    {result.notes_path}",
        )
    )


# --------------------------------------------------------------------------
# The effectful boundary (injected in tests)
# --------------------------------------------------------------------------


def _run_bump(argv: Sequence[str], cwd: Path) -> None:
    """Run one bump-adapter command through the one Exec runner (ADR-0028).

    ``check=True``: a failing bump command (missing ``cargo-edit``, an
    ``npm version`` refusal) raises :class:`~shipit.execrun.ExecError`, which
    the shared error shell renders — prepare aborts with nothing committed.
    """
    execrun.run(list(argv), cwd=str(cwd), timeout=BUMP_TIMEOUT)


def _write_notes(notes_path: Path, text: str) -> None:
    """Write THE one notes text to ``notes_path`` (parents created).

    An unwritable destination surfaces as :class:`ReleaseError` — one
    ``error: …`` line (ADR-0030), never a raw ``OSError`` traceback.
    """
    try:
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"cannot write notes to {notes_path}: {exc}") from exc


def _unquote_status_path(field: str) -> str:
    """Decode one ``git status --porcelain`` path field. Pure.

    Git C-quotes (wraps in double quotes, backslash-escapes) any path with
    special characters — a quote, a backslash, a control char, or (with the
    default ``core.quotepath``) a non-ASCII byte, emitted as an octal ``\\NNN``
    of its UTF-8 bytes. An unquoted field is returned verbatim; a quoted one is
    decoded back to the real path so the glob match sees the true name.
    """
    if not field.startswith('"'):
        return field
    inner = field[1:-1]
    raw = inner.encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "backslashreplace").decode("utf-8", "replace")


def _changed_paths(status_lines: list[str]) -> list[str]:
    """The repo-relative paths of ``git status --porcelain`` lines. Pure.

    A rename line (``R  old -> new``) contributes its NEW path — the side a
    stage/commit addresses. A path with special characters arrives C-quoted
    (:func:`_unquote_status_path`) and is decoded back to its real name.
    """
    paths = []
    for line in status_lines:
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(_unquote_status_path(path))
    return paths


def _leg_pathspecs(leg_path: str, patterns: Sequence[str]) -> list[str]:
    """``patterns`` joined onto a leg's map path (``"."`` → repo root). Pure."""
    if leg_path in (".", ""):
        return list(patterns)
    return [f"{leg_path}/{p}" for p in patterns]


def _matching(changed: Sequence[str], patterns: Sequence[str]) -> list[str]:
    """The ``changed`` paths matching any glob in ``patterns``. Pure.

    ``fnmatch``-style matching where ``*`` crosses ``/`` — exactly what the
    adapters' ``**/Cargo.toml`` / ``CHANGELOG/*`` pathspecs need.
    """
    return [
        path
        for path in changed
        if any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
    ]


# --------------------------------------------------------------------------
# The verb runner (click-free, seams injectable — the testable surface)
# --------------------------------------------------------------------------


@cli_errors
def run_prepare(
    spec: version_mod.VersionSpec,
    *,
    as_json: bool = False,
    notes_out: str | None = None,
    gitio: Any = git,
    run_cmd: Callable[[Sequence[str], Path], None] | None = None,
    read_tree: Callable[[Path], changelog_verb.ChangelogTree] | None = None,
    today: Callable[[], str] | None = None,
) -> int:
    """Run the prepare stage from the current directory. Returns 0/1.

    ``spec`` arrives parsed (usage errors died at the click boundary).
    ``gitio`` injects the git adapter surface, ``run_cmd`` the adapter-command
    Exec boundary, ``read_tree``/``today`` the changelog verb's filesystem and
    clock seams — the recorded-fixture surface the tests drive (PRD Testing
    Decisions).
    """
    run_cmd = run_cmd or _run_bump

    root_s = gitio.repo_root(cwd=".")
    if root_s is None:
        raise ReleaseError(
            "not inside a git checkout — `release prepare` writes repo history"
        )
    root = Path(root_s)
    cwd = str(root)

    cfg = load_config(root)
    entries = config.load_toolchains(cfg)
    artifacts = config.load_artifacts(cfg)

    resolved = version_mod.resolve(spec, gitio.list_tags(cwd=cwd))
    version = resolved.version
    # A relative --notes-out anchors to the repo root, exactly like the default,
    # so the destination never depends on which subdirectory prepare is run from
    # (an absolute path is honoured as given).
    if notes_out:
        notes_arg = Path(notes_out)
        notes_path = notes_arg if notes_arg.is_absolute() else root / notes_arg
    else:
        notes_path = root / DEFAULT_NOTES_FILE

    # The coalesce plan comes FIRST (story 26): the empty-release refusal and
    # every changelog-model refusal fire here, before any manifest is touched
    # — and on a resume it re-derives THE same notes text (ADR-0009).
    _tree, plan = changelog_verb.plan_cut(
        root, version, read_tree=read_tree, today=today
    )

    if resolved.resume:
        # ADR-0009/0041: the tag already exists — this cut already happened.
        # Re-emit the tag's SHA and notes; bump nothing, push nothing.
        sha = gitio.resolve_commit(f"{resolved.tag}^{{commit}}", cwd=cwd)
        if sha is None:  # pragma: no cover — resume implies the tag resolves
            raise ReleaseError(f"tag {resolved.tag} exists but does not resolve")
        result = PrepareResult(
            version=version,
            tag=resolved.tag,
            release_sha=str(sha),
            prerelease=resolved.prerelease,
            resume=True,
            tag_only=resolved.tag_only,
            branch=None,
            notes_path=str(notes_path),
        )
        _write_notes(notes_path, plan.notes)
        emit(result, format_prepare, as_json=as_json)
        logger.info(
            "release prepare resumed",
            extra={"version": version, "tag": resolved.tag, "sha": str(sha)},
        )
        return 0

    # A history-writing cut runs on a CLEAN tree only — refused BEFORE any
    # mutation, so an unrelated pre-existing edit (a dirty pyproject.toml,
    # Cargo.lock, CHANGELOG.md) can neither ride the release commit nor mask a
    # no-op bump, and a -release-rc `reset_hard` can never destroy uncommitted
    # work. Only TRACKED changes count: an untracked file (a build artifact, a
    # prior run's RELEASE_NOTES.md) is never staged by the explicit-pathspec
    # commit and survives `reset_hard`, so it must not block a release — and the
    # gate is skipped entirely on a resume (above), which writes no history.
    dirty = [
        line for line in gitio.status_porcelain(cwd=cwd) if not line.startswith("??")
    ]
    if dirty:
        raise ReleaseError(
            "working tree has uncommitted changes to tracked files — "
            "`release prepare` writes repo history and must run on a clean "
            "tree; commit or stash them first:\n" + "\n".join(dirty)
        )

    branch = gitio.current_branch(cwd=cwd)
    if branch is None:
        # Checked BEFORE any mutation: a detached HEAD has no branch to carry
        # (or, for -release-rc, to restore) — failing later would leave a
        # half-prepared tree.
        raise ReleaseError(
            "detached HEAD — `release prepare` commits on the release branch"
        )
    base_sha = gitio.head_commit(cwd=cwd)
    if base_sha is None:
        raise ReleaseError("cannot read HEAD — is this an empty repository?")

    # Project the tag decision into the manifests: one adapter per leg of the
    # path→toolchain map (ADR-0007/0041), then the artifact-declared
    # bundle-config hooks — never a "tauri" dispatch label (story 25).
    intended: list[str] = []
    expects: list[tuple[str, list[str]]] = []  # (what, its pathspecs) to verify
    for entry in entries:
        adapter = bump_mod.adapter_for(entry.toolchain)
        leg_dir = root if entry.path in (".", "") else root / entry.path
        for argv in adapter.commands(version):
            run_cmd(argv, leg_dir)
        if adapter.edit_path is not None:
            manifest = leg_dir / adapter.edit_path
            if not manifest.is_file():
                raise ReleaseError(
                    f"{entry.toolchain} leg at {entry.path}: no {adapter.edit_path} "
                    "to bump"
                )
            manifest.write_text(
                bump_mod.edit_for(
                    adapter, manifest.read_text(encoding="utf-8"), version
                ),
                encoding="utf-8",
            )
        if adapter.projects_files:
            specs = _leg_pathspecs(entry.path, adapter.stage)
            intended.extend(specs)
            expects.append((f"{entry.toolchain} leg at {entry.path}", specs))
    for artifact in artifacts:
        if artifact.bundle_config is None:
            continue
        hook_file = root / artifact.bundle_config
        if not hook_file.is_file():
            raise ReleaseError(
                f"[artifacts.{artifact.name}] bundle-config names a missing "
                f"file: {artifact.bundle_config}"
            )
        hook_file.write_text(
            bump_mod.bump_bundle_config(hook_file.read_text(encoding="utf-8"), version),
            encoding="utf-8",
        )
        intended.append(artifact.bundle_config)
        expects.append(
            (f"artifact {artifact.name} bundle-config", [artifact.bundle_config])
        )

    # Execute the cut (a no-op for a prerelease extract): section written,
    # fragments consumed, projection re-rendered — so the bump commit passes
    # the fragment-sync check like any other commit (story 24).
    changelog_verb.apply_cut(root, plan, read_tree=read_tree)
    if plan.mutates:
        intended.extend(_CHANGELOG_STAGE)
        expects.append(("changelog roll", list(_CHANGELOG_STAGE)))

    changed = _changed_paths(gitio.status_porcelain(cwd=cwd))
    for what, specs in expects:
        if not _matching(changed, specs):
            raise ReleaseError(
                f"no-op bump: {what} changed none of its declared files "
                f"({', '.join(specs)}) — the tree already carries {version} but "
                f"tag {resolved.tag} does not exist; refusing an empty commit "
                "(re-running against a different release?)"
            )
    to_commit = sorted(set(_matching(changed, intended)))

    if to_commit:
        # ONLY the intended files (story 24); the commit runs the repo's own
        # commit checks — no --no-verify, no second path around policy. A
        # failing check raises before anything is pushed.
        gitio.add(to_commit, cwd=cwd)
        gitio.commit(f"release: {version}", to_commit, cwd=cwd)
    release_sha = gitio.head_commit(cwd=cwd)
    if release_sha is None:  # pragma: no cover — HEAD read just succeeded
        raise ReleaseError("cannot read HEAD after the bump commit")

    # The tag is the version authority (ADR-0041); its annotation carries THE
    # one coalesced notes text (story 26) — the same text the GH release gets.
    # It is written locally BEFORE the push, so any push failure must delete it
    # again: a leftover local tag would make the next run falsely RESUME
    # (ADR-0009 keys resume off tag existence) and report success on a cut that
    # never reached the remote.
    gitio.tag_annotated(resolved.tag, plan.notes, cwd=cwd)
    try:
        if resolved.tag_only:
            # Live-fire contract (-release-rc): the bump commit travels on the
            # TAG ONLY. Move the branch ref back (the commit stays reachable
            # from the tag), then push nothing but the tag.
            if to_commit:
                gitio.reset_hard(str(base_sha), cwd=cwd)
            gitio.push_tag(resolved.tag, cwd=cwd)
        else:
            # Branch and tag publish ATOMICALLY (both refs or neither) through
            # the repo's pre-push checks (story 24): a tag-ref rejection can
            # never leave the remote branch-advanced-but-tagless — a partial
            # state the next run could neither resume (no remote tag) nor
            # cleanly redo (the tree already carries the version).
            gitio.push_atomic(branch, resolved.tag, cwd=cwd)
    except Exception:
        # Best-effort rollback of the local state a failed publish leaves behind,
        # so the next run can cleanly REDO rather than falsely resume or dead-end
        # on a no-op bump — each step independently suppressed so one cleanup
        # failure neither blocks the others nor masks the push error that aborted
        # the release (the bare `raise` re-raises that original exception):
        #   - drop the local tag (ADR-0009 keys resume off tag existence, so a
        #     leftover tag would fake a resume on an unpublished cut);
        #   - for a non-tag-only cut, move the branch ref back off the bump
        #     commit (the tag-only path already reset it inside the try) — else
        #     the tree still carries the version and the redo hits `no-op bump`.
        with contextlib.suppress(Exception):
            gitio.delete_tag(resolved.tag, cwd=cwd)
        if not resolved.tag_only and to_commit:
            with contextlib.suppress(Exception):
                gitio.reset_hard(str(base_sha), cwd=cwd)
        raise

    result = PrepareResult(
        version=version,
        tag=resolved.tag,
        release_sha=str(release_sha),
        prerelease=resolved.prerelease,
        resume=False,
        tag_only=resolved.tag_only,
        branch=None if resolved.tag_only else branch,
        notes_path=str(notes_path),
    )
    # Written AFTER the commit so the transient notes artifact can never ride
    # the bump commit (and never trips the whole-tree commit gate).
    _write_notes(notes_path, plan.notes)
    emit(result, format_prepare, as_json=as_json)
    logger.info(
        "release prepared",
        extra={
            "version": version,
            "tag": resolved.tag,
            "sha": str(release_sha),
            "prerelease": resolved.prerelease,
            "tag_only": resolved.tag_only,
            "committed": len(to_commit),
        },
    )
    return 0


# --------------------------------------------------------------------------
# Click glue
# --------------------------------------------------------------------------


@click.group(name="release")
def release() -> None:
    """The release pipeline, one independently invocable stage per subcommand.

    The tag is the version authority (ADR-0041): `prepare` resolves the
    supplied version, projects it into the manifests, and writes commit +
    annotated tag. Later stages (preflight, bundle, sign, publish) land as
    their work streams do.
    """


@release.command(name="prepare")
@click.argument("version", type=VERSION_SPEC)
@click.option(
    "--notes-out",
    type=click.Path(dir_okay=False),
    help=(
        "Write the coalesced release-notes text to FILE (default: "
        f"{DEFAULT_NOTES_FILE} at the repo root). The same text lands in the "
        "tag annotation; the publish stage reuses this file."
    ),
)
@json_option
def prepare_cmd(
    version: version_mod.VersionSpec, notes_out: str | None, as_json: bool
) -> None:
    """Prepare the release: bump, changelog roll, commit, annotated tag, push.

    VERSION is a bare semver (1.2.3, 1.2.3-rc.1) or a bump word
    (major | minor | patch) resolved against the latest tag — never inferred
    from fragments or commits. If the tag already exists the run RESUMES:
    nothing is bumped and the tag's SHA is re-emitted. A -release-rc version
    is a live-fire cut: the bump commit travels on the tag only and the
    branch ref is never advanced.
    """
    raise SystemExit(run_prepare(version, notes_out=notes_out, as_json=as_json))
