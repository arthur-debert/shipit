"""`shipit release` — the Release pipeline's stage verbs (TOL02, PRD story 19).

Each release stage is independently invocable; this module carries the
pipeline's planner and its effectful stages:

- ``shipit release preflight`` (TOL02-WS02 #560) — the planner (see
  :func:`run_preflight`): (artifact map, resolved version, event) → the
  machine-readable plan, hard-failing on missing secrets before any write;
- ``shipit release prepare`` (TOL02-WS01 #559) — the pipeline's only writer
  of repo history;
- ``shipit release bundle`` (TOL02-WS03 #561) — the composition of build
  outputs into unsigned Artifacts, the effectful walk over the closed
  composition registry (:mod:`shipit.release.bundle`);
- ``shipit release assert-bundle`` (TOL02-WS03 #561) — the scar-#2
  integrity guard (workflows.lex §3.2), the thin shell over the pure core
  (:mod:`shipit.release.integrity`);
- ``shipit release sign`` (TOL02-WS04 #562) — the consumer-agnostic mac
  signer unit (workflows.lex §3.1: reopen → resign inner-first → reseal →
  notarize → staple), the thin shell over :mod:`shipit.release.sign` that
  owns the scratch-dir lifecycle. Act-untestable (real macOS + real Apple
  credentials); remote verification is the TOL02-WS07 lex rc.
- ``shipit release publish`` (TOL02-WS05 #563) — the TERMINAL stage: the
  effectful walk over the closed endpoint-adapter registry
  (:mod:`shipit.release.publish`), dispatching each artifact's declared
  Distribution endpoints, gated by the scar-#3 refusal and the central RC
  guard (both pure cores there).

``prepare`` is the effectful shell over three pure cores:

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
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .. import config, events, execrun, gh, git, redact
from ..changelog import is_prerelease
from ..release import ReleaseError
from ..release import bump as bump_mod
from ..release import bundle as bundle_mod
from ..release import integrity as integrity_mod
from ..release import preflight as preflight_mod
from ..release import publish as publish_mod
from ..release import sign as sign_mod
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

#: Each composition command Exec's stated timeout (ADR-0028): a declared mac
#: bundler (``tauri build``) — and the deb composition's cargo-deb
#: self-provision (``cargo install``, a cold compile) — legitimately runs
#: long, so the bound matches the build verbs' hour rather than the bump
#: commands' minutes.
BUNDLE_TIMEOUT: float = 3600.0

#: The bundle output tree when ``--out`` is omitted: repo-root-relative — the
#: legacy packaging steps' ``dist/`` home. The ONLY place compositions write
#: (ADR-0009's barrier); uploads are publish's job, signing the signer's.
DEFAULT_BUNDLE_DIR = "dist"


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


def format_preflight(release_plan: preflight_mod.ReleasePlan) -> str:
    """The text rendering of a :class:`~shipit.release.preflight.ReleasePlan`.
    Pure. The workflow consumes ``--json``; this is the operator's summary."""
    kind = "prerelease" if release_plan.prerelease else "final"
    lines = [
        f"release preflight: {release_plan.version} ({kind}, "
        f"event {release_plan.event})",
        f"  artifacts  {', '.join(release_plan.artifacts) or 'none'}",
        f"  matrix     {len(release_plan.matrix)} "
        f"entr{'y' if len(release_plan.matrix) == 1 else 'ies'}"
        + (
            f" ({', '.join(e.platform for e in release_plan.matrix)})"
            if release_plan.matrix
            else ""
        ),
        f"  stages     {', '.join(release_plan.stages)}",
        f"  endpoints  {', '.join(release_plan.endpoints)}",
        f"  secrets    {', '.join(release_plan.secrets)}",
    ]
    # The plan's either-satisfies requirements (#746): one line per set, so
    # the operator sees that ANY listed trio satisfies it — these names are
    # deliberately not mixed into the `secrets` conjunction line above.
    lines.extend(
        f"  either     {alt.label}: "
        + " or ".join(f"{a.label} ({', '.join(a.names)})" for a in alt.alternatives)
        for alt in release_plan.secret_alternatives
    )
    if release_plan.tag_only:
        lines.append(
            "  rc guard   -release-rc: GH release only, external endpoints dropped"
        )
    if release_plan.unsigned:
        lines.append("  UNSIGNED   break-glass: sign stage skipped (recorded)")
    return "\n".join(lines)


@cli_errors
def run_preflight(
    spec: version_mod.VersionSpec,
    *,
    event: str = "dispatch",
    unsigned: bool = False,
    as_json: bool = False,
    gitio: Any = git,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run the preflight planner from the current directory. Returns 0/1.

    The thin shell over the pure core (ADR-0030): load the artifact map,
    resolve the supplied version against the repo's tags (ADR-0041 — the
    same resolution prepare will make), plan
    (:func:`shipit.release.preflight.plan`), record the ``--unsigned``
    break-glass (story 29: every use is a durable ``release.unsigned``
    event), hard-fail on missing required secrets (story 28 — checked
    against the injected ``env``; the workflow's caller injects each GitHub
    secret as a same-named env var), and render text or ``--json``. The plan
    refusals (phantom release, nothing-to-break-glass) and the presence
    failure are :class:`~shipit.release.ReleaseError` → exit 1.
    """
    root_s = gitio.repo_root(cwd=".")
    if root_s is None:
        raise ReleaseError(
            "not inside a git checkout — `release preflight` reads the "
            "repo's declarations"
        )
    root = Path(root_s)
    cfg = load_config(root)
    artifacts = config.load_artifacts(cfg)
    resolved = version_mod.resolve(spec, gitio.list_tags(cwd=str(root)))

    release_plan = preflight_mod.plan(
        artifacts, resolved, event=event, unsigned=unsigned
    )
    if unsigned:
        # The break-glass record (story 29): emitted AFTER the plan accepted
        # the flip (a refused --unsigned never counts as a use), before any
        # output consumes the unsigned plan.
        events.emit(
            logger,
            "release.unsigned",
            "release preflight --unsigned: sign stage skipped for %s (%s)",
            release_plan.version,
            release_plan.tag,
            extra={"version": release_plan.version, "tag": release_plan.tag},
        )
    missing = preflight_mod.missing_secrets(
        release_plan, os.environ if env is None else env
    )
    if missing:
        raise ReleaseError(
            f"missing required secrets: {', '.join(missing)} — the plan "
            "cannot run to publish; failing now, before prepare writes any "
            "history"
        )
    emit(release_plan, format_preflight, as_json=as_json)
    logger.info(
        "release preflight planned",
        extra={
            "version": release_plan.version,
            "tag": release_plan.tag,
            "event": release_plan.event,
            "unsigned": release_plan.unsigned,
            "matrix": len(release_plan.matrix),
            "stages": ",".join(release_plan.stages),
            "endpoints": ",".join(release_plan.endpoints),
        },
    )
    return 0


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
# The bundle stage (TOL02-WS03): build outputs → unsigned Artifacts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleResult:
    """The bundle stage's uniform, typed output (ADR-0030).

    ``composed`` carries what each declared composition produced (out-tree-
    relative paths); ``skipped`` the declared compositions that do not apply
    to this target (a deb on a mac run — the per-OS matrix runs them on
    theirs); ``passthrough`` the artifacts with NO bundle declaration
    (zero-bundle artifacts like a tag-only release stay legal and untouched).
    """

    target: str
    out: str
    composed: tuple[bundle_mod.Composed, ...]
    skipped: tuple[tuple[str, str], ...]
    passthrough: tuple[str, ...]

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
        return {
            "target": self.target,
            "out": self.out,
            "composed": [c.to_dict() for c in self.composed],
            "skipped": [
                {"artifact": name, "composition": comp} for name, comp in self.skipped
            ],
            "passthrough": list(self.passthrough),
        }


def format_bundle(result: BundleResult) -> str:
    """The text rendering of a :class:`BundleResult`. Pure."""
    if not result.composed and not result.skipped:
        return "release: no bundle declared — nothing to compose"
    count = len(result.composed)
    lines = [
        f"release: bundled {count} artifact{'s' if count != 1 else ''} "
        f"for {result.target} -> {result.out}"
    ]
    for composed in result.composed:
        lines.append(
            f"  {composed.artifact}  [{composed.composition}]  "
            f"{', '.join(composed.outputs)}"
        )
    for name, comp in result.skipped:
        lines.append(f"  {name}  [{comp}]  skipped: not for this target")
    for name in result.passthrough:
        lines.append(f"  {name}  passthrough: no bundle declared")
    return "\n".join(lines)


def _run_compose(argv: Sequence[str], cwd: Path) -> execrun.ExecResult:
    """Run one composition command through the one Exec runner (ADR-0028).

    ``check=True``: a failing composition command (a bundler refusal, a
    failing cargo-deb self-provision) raises
    :class:`~shipit.execrun.ExecError`, which the shared error shell renders
    — the bundle stage aborts non-zero with later artifacts untouched
    (ADR-0009's barrier for the callers that chain stages).
    """
    return execrun.run(list(argv), cwd=str(cwd), timeout=BUNDLE_TIMEOUT)


@cli_errors
def run_bundle(
    *,
    target: str | None = None,
    out: str | None = None,
    artifact: str | None = None,
    as_json: bool = False,
    run_cmd: bundle_mod.RunCmd | None = None,
    gitio: Any = git,
) -> int:
    """Run the bundle stage from the current directory. Returns 0/1.

    Walks the ``[artifacts]`` map in declaration order and runs each declared
    composition that applies to the target (:mod:`shipit.release.bundle`);
    an artifact with no bundle declaration passes through untouched. The
    FIRST failing composition aborts the stage non-zero — nothing is written
    outside the bundle output tree, preserving ADR-0009's all-or-nothing
    barrier for chained stages. ``artifact`` narrows the walk to ONE declared
    artifact — the per-matrix-entry contract wf-build rides (each entry is
    one artifact × platform, and its cross-job bundle artifact must carry
    exactly that artifact's outputs: a whole-map tree would put every
    artifact's binary in every entry's tree and fail wf-publish's
    per-artifact assert-bundle on any multi-artifact repo); an unknown name
    is a loud refusal naming the declared set. ``run_cmd`` injects the Exec
    boundary — the recorded-invocation surface the tests drive; ``gitio``
    the git adapter.

    Config and the output tree anchor to the CHECKOUT ROOT (``gitio.repo_root``,
    like ``prepare``), not the process cwd: ``load_config`` reads ``.shipit.toml``
    from that exact dir without walking parents, so rooting at cwd would make a
    run from a subdirectory silently see zero artifacts and mis-anchor ``--out``.
    """
    run_cmd = run_cmd or _run_compose
    root_s = gitio.repo_root(cwd=".")
    if root_s is None:
        raise ReleaseError(
            "not inside a git checkout — `release bundle` composes a checkout's "
            "build outputs"
        )
    root = Path(root_s)
    cfg = load_config(root)
    entries = config.load_toolchains(cfg)
    artifacts = config.load_artifacts(cfg)
    if artifact is not None:
        selected = tuple(a for a in artifacts if a.name == artifact)
        if not selected:
            declared = ", ".join(a.name for a in artifacts) or "none declared"
            raise ReleaseError(
                f"--artifact {artifact}: no such artifact in the [artifacts] "
                f"map (declared: {declared})"
            )
        artifacts = selected

    resolved = target or bundle_mod.host_target(platform.system(), platform.machine())
    if resolved is None:
        raise ReleaseError(
            f"cannot derive a target triple for this host "
            f"({platform.system()}/{platform.machine()}) — pass --target"
        )
    out_arg = Path(out) if out else Path(DEFAULT_BUNDLE_DIR)
    # A relative --out anchors to the repo root, exactly like the default, so
    # the output tree never depends on which subdirectory bundle is run from.
    out_dir = out_arg if out_arg.is_absolute() else root / out_arg

    composed: list[bundle_mod.Composed] = []
    skipped: list[tuple[str, str]] = []
    passthrough: list[str] = []
    for artifact in artifacts:
        if artifact.bundle is None:
            passthrough.append(artifact.name)
            continue
        comp = bundle_mod.composition(artifact.bundle.composition)
        if comp is None:  # pragma: no cover — the parse boundary validated it
            raise ReleaseError(
                f"[artifacts.{artifact.name}] names unknown composition "
                f"{artifact.bundle.composition!r}"
            )
        if not comp.applies(resolved):
            skipped.append((artifact.name, comp.name))
            continue
        composed.append(
            comp.compose(
                bundle_mod.ComposeRequest(
                    artifact=artifact,
                    entries=entries,
                    root=root,
                    out_dir=out_dir,
                    target=resolved,
                    run_cmd=run_cmd,
                )
            )
        )

    result = BundleResult(
        target=resolved,
        out=str(out_dir),
        composed=tuple(composed),
        skipped=tuple(skipped),
        passthrough=tuple(passthrough),
    )
    emit(result, format_bundle, as_json=as_json)
    logger.info(
        "release bundle complete",
        extra={
            "target": resolved,
            "out": str(out_dir),
            "composed": len(composed),
            "skipped": len(skipped),
            "passthrough": len(passthrough),
        },
    )
    return 0


# --------------------------------------------------------------------------
# assert-bundle (TOL02-WS03): the scar-#2 integrity guard, workflows.lex §3.2
# --------------------------------------------------------------------------


def format_assert_bundle(verdict: integrity_mod.BundleVerdict) -> str:
    """The text rendering of a :class:`~shipit.release.integrity.BundleVerdict`
    — verdict plus expected/actual names, the §3.2 diagnosis. Pure."""
    if verdict.ok:
        return (
            f"assert-bundle: ok — main binary {verdict.expected!r} "
            f"(tree {verdict.tree})"
        )
    found = ", ".join(verdict.actual) if verdict.actual else "none"
    line = (
        f"assert-bundle: FAIL — expected main binary {verdict.expected!r}, "
        f"found: {found}"
    )
    if verdict.problem is not None:
        line += f" ({verdict.problem})"
    return f"{line} (tree {verdict.tree})"


@cli_errors
def run_assert_bundle(
    tree: str,
    *,
    artifact: str | None = None,
    expected: str | None = None,
    as_json: bool = False,
    gitio: Any = git,
) -> int:
    """Run the integrity guard over the bundle tree at ``tree``. Returns 0/1.

    ``expected`` short-circuits the artifact map entirely (the name to assert,
    supplied directly); otherwise the expected name resolves from the named
    ``artifact``'s declaration — or the repo's ONE artifact when unnamed —
    through the fallback chain (mainBinaryName → productName → package name,
    :func:`shipit.release.integrity.expected_main_binary`). Pure over the
    tree: no network, no toolchain. On failure the verdict with expected and
    actual names lands on stderr (exit 1), so the WS06 blocks — the signer's
    entry and the unsigned publish path — call this with no extra plumbing;
    ``--json`` renders the same typed verdict on stdout either way.

    The artifact-map branch anchors config to the CHECKOUT ROOT
    (``gitio.repo_root``), not the process cwd: ``load_config`` does not walk
    up to ``.shipit.toml``, so a run from a subdirectory would otherwise misread
    the repo as declaring zero artifacts. ``--expected`` needs no checkout.
    """
    if expected is None:
        root_s = gitio.repo_root(cwd=".")
        if root_s is None:
            raise ReleaseError(
                "not inside a git checkout — resolve the expected name from the "
                "artifact map, or pass --expected NAME"
            )
        artifacts = config.load_artifacts(load_config(Path(root_s)))
        if artifact is not None:
            match = next((a for a in artifacts if a.name == artifact), None)
            if match is None:
                known = ", ".join(a.name for a in artifacts) or "none declared"
                raise ReleaseError(
                    f"unknown artifact {artifact!r} — declared artifacts: {known}"
                )
        elif len(artifacts) == 1:
            match = artifacts[0]
        else:
            raise ReleaseError(
                f"this repo declares {len(artifacts)} artifacts — name one "
                f"(`shipit release assert-bundle TREE ARTIFACT`) or pass "
                f"--expected"
            )
        expected = integrity_mod.expected_main_binary(match)

    verdict = integrity_mod.check_tree(Path(tree), expected)
    if as_json:
        print(json.dumps(verdict.to_dict(), indent=2))
    if verdict.ok:
        if not as_json:
            print(format_assert_bundle(verdict))
        logger.info(
            "assert-bundle passed",
            extra={"tree": verdict.tree, "expected": verdict.expected},
        )
        return 0
    # The failure diagnosis goes to STDERR (the acceptance contract: verdict +
    # expected/actual names on stderr) — even under --json, whose typed verdict
    # rides stdout without colliding.
    print(format_assert_bundle(verdict), file=sys.stderr)
    logger.error(
        "assert-bundle failed",
        extra={
            "tree": verdict.tree,
            "expected": verdict.expected,
            "actual": ", ".join(verdict.actual),
        },
    )
    return 1


# --------------------------------------------------------------------------
# The sign stage (TOL02-WS04): the consumer-agnostic mac signer unit
# --------------------------------------------------------------------------


def format_sign(result: sign_mod.SignResult) -> str:
    """The text rendering of a :class:`~shipit.release.sign.SignResult`. Pure."""
    staple = "stapled" if result.stapled else "staple failed (non-fatal)"
    return "\n".join(
        (
            f"release: signed + notarized {result.app} -> {result.dmg}",
            f"  identity  {result.identity}",
            f"  nested    {result.nested_signed} nested signable(s) signed before the .app",
            f"  notary    {result.submission_id} ({staple})",
        )
    )


def _run_sign_cmd(argv: Sequence[str], timeout: float) -> execrun.ExecResult:
    """Run one signer command through the one Exec runner (ADR-0028).

    ``check=True``: a failing tool (a codesign refusal, a keychain collision,
    an hdiutil error) raises :class:`~shipit.execrun.ExecError`, which the
    shared error shell renders — the sign stage aborts non-zero with the
    temporary keychain torn down and decoded credentials wiped by the core's
    ``finally`` blocks. Each command's timeout is STATED by the core
    (:mod:`shipit.release.sign`'s per-stage constants), never the runner's
    implicit default.
    """
    return execrun.run(list(argv), timeout=timeout)


@cli_errors
def run_sign(
    tree: str,
    *,
    out: str | None = None,
    entitlements: str | None = None,
    notary_timeout: int = sign_mod.DEFAULT_NOTARY_TIMEOUT_MIN,
    as_json: bool = False,
    run_cmd: sign_mod.RunCmd | None = None,
    env: Mapping[str, str] | None = None,
    uniq: Callable[[], str] | None = None,
    mint_pass: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Run the sign stage over the bundle tree at ``tree``. Returns 0/1.

    The consumer-agnostic transformer needs no git checkout and no
    ``.shipit.toml``: its inputs are the tree (carrying the reseal payload +
    at most one ``.dmg``) and the credential env vars, hard-failing with the
    missing names when they are absent (:mod:`shipit.release.sign`). This
    shell owns the scratch dir every intermediate lives under — removed whole
    on any exit, so no decoded credential material and no half-signed
    intermediate survives the run. ``run_cmd`` injects the Exec boundary (the
    recorded-invocation surface the tests drive); ``env``/``uniq``/
    ``mint_pass``/``sleep`` the credential source and nondeterminism seams.
    """
    run_cmd = run_cmd or _run_sign_cmd
    tree_path = Path(tree)
    out_arg = Path(out) if out else tree_path
    seams: dict[str, Any] = {}
    for name, value in (("uniq", uniq), ("mint_pass", mint_pass), ("sleep", sleep)):
        if value is not None:
            seams[name] = value
    scratch = Path(tempfile.mkdtemp(prefix="shipit-sign-"))
    try:
        result = sign_mod.sign_bundle(
            sign_mod.SignRequest(
                tree=tree_path,
                out_dir=out_arg,
                scratch=scratch,
                run_cmd=run_cmd,
                env=os.environ if env is None else env,
                entitlements=Path(entitlements) if entitlements else None,
                timeout_minutes=notary_timeout,
                **seams,
            )
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    emit(result, format_sign, as_json=as_json)
    logger.info(
        "release sign complete",
        extra={
            "app": result.app,
            "dmg": result.dmg,
            "submission_id": result.submission_id,
            "stapled": result.stapled,
            "nested_signed": result.nested_signed,
        },
    )
    return 0


# --------------------------------------------------------------------------
# The publish stage (TOL02-WS05): staged Artifacts → Distribution endpoints
# --------------------------------------------------------------------------

#: Each publish command Exec's stated timeout (ADR-0028): ``cargo publish``
#: verify-builds the crate before uploading, so the bound matches the build
#: verbs' hour rather than a network call's minutes.
PUBLISH_TIMEOUT: float = 3600.0


@dataclass(frozen=True)
class PublishResult:
    """The publish stage's uniform, typed output (ADR-0030).

    ``published`` carries each completed endpoint dispatch's actions;
    ``skipped`` the dispatches the PLAN skipped, with the stated reason (the
    RC guard, brew's stable-only rule) — skips are verdicts, never silence.
    """

    version: str
    tag: str
    prerelease: bool
    live_fire: bool
    published: tuple[publish_mod.Published, ...]
    skipped: tuple[tuple[str, str, str], ...]

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
        return {
            "version": self.version,
            "tag": self.tag,
            "prerelease": self.prerelease,
            "live_fire": self.live_fire,
            "published": [p.to_dict() for p in self.published],
            "skipped": [
                {"artifact": artifact, "endpoint": endpoint, "reason": reason}
                for artifact, endpoint, reason in self.skipped
            ],
        }


def format_publish(result: PublishResult) -> str:
    """The text rendering of a :class:`PublishResult`. Pure."""
    if not result.published and not result.skipped:
        return "release: no endpoints declared — nothing to publish"
    count = len(result.published)
    headline = (
        f"release: published {result.version} to {count} "
        f"endpoint{'s' if count != 1 else ''}"
    )
    if result.live_fire:
        headline += " (live-fire -release-rc: GH release only)"
    lines = [headline]
    for published in result.published:
        lines.append(
            f"  {published.artifact}  [{published.endpoint}]  "
            f"{'; '.join(published.actions)}"
        )
    for artifact, endpoint, reason in result.skipped:
        lines.append(f"  {artifact}  [{endpoint}]  skipped: {reason}")
    return "\n".join(lines)


def _run_publish_cmd(
    argv: Sequence[str], cwd: Path, env: Any = None
) -> execrun.ExecResult:
    """Run one adapter command through the one Exec runner (ADR-0028),
    check=True: a failing command raises :class:`~shipit.execrun.ExecError`,
    rendered by the shared error shell — publish aborts fail-fast, and a
    re-run resumes (ADR-0009 phase 2)."""
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        env=dict(env) if env else None,
        timeout=PUBLISH_TIMEOUT,
    )


def _probe_publish_cmd(
    argv: Sequence[str], cwd: Path, env: Any = None
) -> execrun.ExecResult:
    """Run one adapter command as a probe (check=False): a nonzero rc is a
    NORMAL answer the adapter classifies — the already-published resume path
    of ``cargo publish`` / ``npm publish``."""
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        env=dict(env) if env else None,
        check=False,
        timeout=PUBLISH_TIMEOUT,
    )


@cli_errors
def run_publish(
    spec: version_mod.VersionSpec,
    *,
    build_result: str,
    bundle_result: str,
    sign_result: str,
    matrix: str | None = None,
    stages: str | None = None,
    assets: str | None = None,
    notes: str | None = None,
    testpypi: bool = False,
    as_json: bool = False,
    gitio: Any = git,
    ghio: Any = gh,
    run_cmd: publish_mod.RunCmd | None = None,
    probe: publish_mod.Probe | None = None,
    env: Any = None,
) -> int:
    """Run the publish stage from the current directory. Returns 0/1.

    The order of operations IS the invariant set:

    1. The scar-#3 refusal gate first (:func:`shipit.release.publish.check_gate`,
       PRD story 32) — pure, before any I/O, so a blocked publish touches
       nothing. ``matrix``/``stages`` are the preflight plan's fields
       VERBATIM — the stage-liveness facts (issue #745): an empty matrix
       proves build non-live, while a stages list without ``bundle`` proves
       bundle non-live; the gate then accepts ``skipped`` for exactly those
       stages. Omitted facts default to LIVE — the strict
       contract (a caller that states no plan never weakens the gate);
       liveness is never inferred from the result strings.
    2. The plan (:func:`shipit.release.publish.plan`): the RC guard and
       brew's stable-only rule decided centrally, ``release`` endpoints
       ordered before ``derived`` ones (stories 33/35). WS02's preflight
       will emit this same plan; until then publish derives it from the map.
    3. Token validation for every NON-SKIPPED dispatch — a missing token is
       one loud refusal BEFORE the first dispatch, never a silent adapter
       skip (stories 43–45); present tokens are registered with the central
       redactor so no Exec record can leak them.
    4. The dispatches, in plan order, fail-fast: external endpoints cannot
       roll back, so a mid-run failure aborts and the RE-RUN converges
       (ADR-0009 phase 2 — every adapter treats already-published as
       success).

    ``spec`` must carry a concrete semver (the click boundary rejects bump
    words — publish ships the version prepare cut, it never re-resolves).
    ``run_cmd``/``probe`` inject the Exec boundary, ``gitio``/``ghio`` the
    git/gh adapters, ``env`` the token lookup surface — the recorded-fixture
    surface the tests drive (PRD Testing Decisions).

    Config and the asset tree anchor to the CHECKOUT ROOT (``gitio.repo_root``,
    like ``bundle``): ``load_config`` reads ``.shipit.toml`` from that exact
    dir without walking parents.
    """
    # The refusal gate runs FIRST — pure, before any filesystem or git read
    # (ADR-0040: the block passes results AND plan facts in, the VERB
    # enforces). Liveness derives from the plan verbatim; an omitted fact
    # stays live/strict.
    build_live = True if matrix is None else publish_mod.build_is_live(matrix)
    bundle_live = True if stages is None else publish_mod.bundle_is_live(stages)
    publish_mod.check_gate(
        build_result,
        bundle_result,
        sign_result,
        build_live=build_live,
        bundle_live=bundle_live,
    )

    run_cmd = run_cmd or _run_publish_cmd
    probe = probe or _probe_publish_cmd
    env_map = os.environ if env is None else env

    root_s = gitio.repo_root(cwd=".")
    if root_s is None:
        raise ReleaseError(
            "not inside a git checkout — `release publish` walks a checkout's "
            "artifact map"
        )
    root = Path(root_s)
    cfg = load_config(root)
    entries = config.load_toolchains(cfg)
    artifacts = config.load_artifacts(cfg)

    assert spec.semver is not None  # the click boundary rejects bump words
    version = spec.semver
    tag = f"{version_mod.TAG_PREFIX}{version}"
    prerelease = is_prerelease(version)
    live_fire = publish_mod.is_live_fire(version)

    assets_arg = Path(assets) if assets else Path(DEFAULT_BUNDLE_DIR)
    assets_dir = assets_arg if assets_arg.is_absolute() else root / assets_arg
    notes_arg = Path(notes) if notes else Path(DEFAULT_NOTES_FILE)
    notes_path = notes_arg if notes_arg.is_absolute() else root / notes_arg

    dispatches = publish_mod.plan(artifacts, prerelease=prerelease, live_fire=live_fire)

    # Token validation BEFORE the first dispatch (stories 43-45): one loud
    # refusal naming every missing token, never a silent adapter skip. The
    # tokens that ARE present get registered with the central redactor at
    # the one moment the verb provably reads them.
    missing = publish_mod.missing_secrets(dispatches, env_map, testpypi=testpypi)
    if missing:
        raise ReleaseError(
            "publish refused — required tokens are not set: "
            + ", ".join(f"{key} ({endpoint})" for endpoint, key in missing)
            + " — gh-setup derives and syncs the needed set from the "
            "declared endpoints"
        )
    for dispatch in dispatches:
        if dispatch.skip is not None:
            continue
        for key in publish_mod.required_env_keys(dispatch.adapter, testpypi=testpypi):
            redact.register_secret(env_map[key])

    # The repo slug is resolved only when a planned dispatch needs it (brew's
    # asset URLs) — a laptop RC cut must not require a gh round-trip.
    repo: str | None = None
    if any(d.skip is None and d.adapter.name == "brew" for d in dispatches):
        repo = ghio.current_repo(cwd=str(root)).slug

    published: list[publish_mod.Published] = []
    skipped: list[tuple[str, str, str]] = []
    for dispatch in dispatches:
        if dispatch.skip is not None:
            skipped.append(
                (dispatch.artifact.name, dispatch.adapter.name, dispatch.skip)
            )
            continue
        published.append(
            dispatch.adapter.publish(
                publish_mod.PublishRequest(
                    artifact=dispatch.artifact,
                    entries=entries,
                    root=root,
                    assets_dir=assets_dir,
                    version=version,
                    tag=tag,
                    prerelease=prerelease,
                    notes_path=notes_path,
                    env=env_map,
                    run_cmd=run_cmd,
                    probe=probe,
                    ghio=ghio,
                    gitio=gitio,
                    repo=repo,
                    testpypi=testpypi,
                )
            )
        )

    result = PublishResult(
        version=version,
        tag=tag,
        prerelease=prerelease,
        live_fire=live_fire,
        published=tuple(published),
        skipped=tuple(skipped),
    )
    emit(result, format_publish, as_json=as_json)
    logger.info(
        "release publish complete",
        extra={
            "version": version,
            "tag": tag,
            "prerelease": prerelease,
            "live_fire": live_fire,
            "published": len(published),
            "skipped": len(skipped),
        },
    )
    return 0


# --------------------------------------------------------------------------
# Click glue
# --------------------------------------------------------------------------


@click.group(name="release")
def release() -> None:
    """The release pipeline, one independently invocable stage per subcommand.

    The tag is the version authority (ADR-0041): `preflight` plans the run
    (matrix, live stages, post-RC-guard endpoints, required secrets) and
    validates it before anything is written; `prepare` resolves the supplied
    version, projects it into the manifests, and writes commit + annotated
    tag; `bundle` composes build outputs into the unsigned Artifacts and
    `assert-bundle` guards their integrity (workflows.lex §3.2); `sign` is the
    mac signer unit — it reopens an unsigned .app/.dmg bundle and reseals it
    signed, notarized, and stapled (workflows.lex §3.1); `publish` — the
    terminal stage — dispatches the staged Artifacts to their declared
    Distribution endpoints, gated by the scar-#3 refusal and the central RC
    guard.
    """


@release.command(name="preflight")
@click.argument("version", type=VERSION_SPEC)
@click.option(
    "--event",
    type=click.Choice(preflight_mod.EVENTS),
    default="dispatch",
    show_default=True,
    help=(
        "The triggering release event the plan records — the composed "
        "workflow's dispatch run or a laptop cut."
    ),
)
@click.option(
    "--unsigned",
    is_flag=True,
    help=(
        "Break-glass: plan the unsigned path (sign stage skipped, Apple "
        "secrets unchecked). Explicit and recorded — every use lands a "
        "release.unsigned event; refused when the repo declares no signing."
    ),
)
@json_option
def preflight_cmd(
    version: version_mod.VersionSpec, event: str, unsigned: bool, as_json: bool
) -> None:
    """Plan the release: matrix, live stages, endpoints, required secrets.

    VERSION is a bare semver (1.2.3, 1.2.3-rc.1) or a bump word
    (major | minor | patch) resolved against the latest tag, exactly as
    `prepare` will resolve it. Emits the machine-readable plan the composed
    workflow consumes as job outputs (--json) — decisions are made HERE,
    never re-derived in YAML — and hard-fails while it is still cheap:
    before any toolchain exists and before prepare writes history. A
    -release-rc version plans GH-release-only (external endpoints dropped
    from the plan); missing required secrets are a hard failure.
    """
    raise SystemExit(
        run_preflight(version, event=event, unsigned=unsigned, as_json=as_json)
    )


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


@release.command(name="bundle")
@click.option(
    "--target",
    metavar="TRIPLE",
    help=(
        "The target triple the bundles are named for (<name>-<target>); "
        "default: derived from this host. Naming-only: builds are native "
        "(`shipit build` writes target/release/), so no composition reads "
        "a target/<triple>/ dir."
    ),
)
@click.option(
    "--out",
    type=click.Path(file_okay=False),
    help=f"The bundle output tree (default: {DEFAULT_BUNDLE_DIR} at the repo root).",
)
@click.option(
    "--artifact",
    metavar="NAME",
    help=(
        "Narrow the walk to this one declared artifact (the per-matrix-entry "
        "contract: wf-build passes its entry's artifact so each cross-job "
        "bundle tree carries exactly that artifact's outputs). Unknown names "
        "are refused loudly."
    ),
)
@json_option
def bundle_cmd(
    target: str | None, out: str | None, artifact: str | None, as_json: bool
) -> None:
    """Compose build outputs into the unsigned Artifacts.

    Walks the [artifacts] map and runs each artifact's declared bundle
    composition (archive, deb, wheel, mac-app) for the current target;
    artifacts with no bundle declaration pass through untouched, and
    compositions for other platforms are skipped (the per-OS matrix runs
    them on theirs). --artifact narrows the walk to one declared artifact
    (each matrix entry bundles its own artifact only). Writes only under
    the bundle output tree — no uploads, no signing. Any failing
    composition exits non-zero with later artifacts untouched.
    """
    raise SystemExit(
        run_bundle(target=target, out=out, artifact=artifact, as_json=as_json)
    )


@release.command(name="assert-bundle")
@click.argument("tree", type=click.Path(exists=True, file_okay=False))
@click.argument("artifact", required=False)
@click.option(
    "--expected",
    metavar="NAME",
    help=(
        "Assert this main-binary name directly, bypassing the artifact map's "
        "fallback chain."
    ),
)
@json_option
def assert_bundle_cmd(
    tree: str, artifact: str | None, expected: str | None, as_json: bool
) -> None:
    """Assert the bundle tree's main binary is the expected app.

    The integrity guard (workflows.lex 3.2): signing is not integrity, so
    before a bundle is signed or published, its MAIN binary must be the
    declared app — the expected name resolves via main-binary -> product-name
    -> package name from ARTIFACT's declaration (or the repo's one artifact
    when omitted). Exit 0 when it matches; exit 1 with the verdict and the
    expected/actual names on stderr when it does not.
    """
    raise SystemExit(
        run_assert_bundle(tree, artifact=artifact, expected=expected, as_json=as_json)
    )


@release.command(name="sign")
@click.argument("tree", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--out",
    type=click.Path(file_okay=False),
    help=(
        "Stage the signed .dmg here (default: TREE itself, replacing the "
        "unsigned .dmg under its original filename)."
    ),
)
@click.option(
    "--entitlements",
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Entitlements plist applied when codesigning the top-level .app ONLY "
        "(never its nested frameworks/helpers — that mis-application is what "
        "the notary rejects; mac apps with QL/Spotlight extensions usually "
        "need one)."
    ),
)
@click.option(
    "--notary-timeout",
    type=click.IntRange(min=1),
    default=sign_mod.DEFAULT_NOTARY_TIMEOUT_MIN,
    show_default=True,
    metavar="MIN",
    help="Max minutes to wait for Apple's notary verdict before hard-failing.",
)
@json_option
def sign_cmd(
    tree: str,
    out: str | None,
    entitlements: str | None,
    notary_timeout: int,
    as_json: bool,
) -> None:
    """Sign, notarize, and staple an unsigned mac bundle tree.

    The consumer-agnostic mac signer unit (workflows.lex 3.1): TREE carries
    the unsigned .app reseal payload (<name>.unsigned-app.tar.gz) and at
    most one .dmg. The unit unpacks the .app, codesigns every nested signable
    (Mach-O files and nested bundle roots) inner-first and the .app LAST
    (hardened runtime + timestamp), reseals
    the .dmg from the SIGNED .app via hdiutil, codesigns it, notarizes +
    staples, and stages the signed .dmg under the original dmg filename.
    Runs on a mac laptop outside CI given the credential env vars; missing
    signing or notary secrets is a hard fail naming the missing names —
    there is no warn-and-skip (the unsigned path is upstream --unsigned
    break-glass, never a skip in here). Act-untestable: remote verification
    is the TOL02-WS07 lex rc.
    """
    raise SystemExit(
        run_sign(
            tree,
            out=out,
            entitlements=entitlements,
            notary_timeout=notary_timeout,
            as_json=as_json,
        )
    )


_RESULT_CHOICE = click.Choice(publish_mod.STAGE_RESULTS)


@release.command(name="publish")
@click.argument("version", type=VERSION_SPEC)
@click.option(
    "--build-result",
    type=_RESULT_CHOICE,
    required=True,
    help=(
        "The build stage's result — `success` when the stage is live; "
        "`skipped` also passes when --matrix proves it non-live (scar #3)."
    ),
)
@click.option(
    "--bundle-result",
    type=_RESULT_CHOICE,
    required=True,
    help=(
        "The bundle stage's result — `success` when the stage is live; "
        "`skipped` also passes when --stages proves it non-live (scar #3)."
    ),
)
@click.option(
    "--sign-result",
    type=_RESULT_CHOICE,
    required=True,
    help=(
        "The sign stage's result — `success` (signed path) or `skipped` "
        "(unsigned path); a FAILED sign blocks everything (scar #3)."
    ),
)
@click.option(
    "--matrix",
    help=(
        "The preflight plan's `matrix` JSON, verbatim — the build stage's "
        "liveness fact: an empty matrix (the tag-is-the-release shape) "
        "proves build non-live, so `skipped` passes the gate. Omitted: "
        "build is treated as live (`success` required)."
    ),
)
@click.option(
    "--stages",
    help=(
        "The preflight plan's `stages` JSON, verbatim — the bundle stage's "
        "liveness fact: a list without `bundle` proves bundle non-live, so "
        "`skipped` passes the gate. Omitted: bundle is treated as live "
        "(`success` required)."
    ),
)
@click.option(
    "--assets",
    type=click.Path(file_okay=False),
    help=(
        f"The staged bundle tree the endpoints ship (default: "
        f"{DEFAULT_BUNDLE_DIR} at the repo root)."
    ),
)
@click.option(
    "--notes",
    type=click.Path(dir_okay=False),
    help=(
        f"The coalesced release-notes file for the GH release (default: "
        f"{DEFAULT_NOTES_FILE} at the repo root — where `release prepare` "
        f"writes it)."
    ),
)
@click.option(
    "--testpypi",
    is_flag=True,
    help=(
        "Reroute the pypi endpoint to test.pypi.org (staging lane; needs "
        "TESTPYPI_TOKEN instead of PYPI_TOKEN)."
    ),
)
@json_option
def publish_cmd(
    version: version_mod.VersionSpec,
    build_result: str,
    bundle_result: str,
    sign_result: str,
    matrix: str | None,
    stages: str | None,
    assets: str | None,
    notes: str | None,
    testpypi: bool,
    as_json: bool,
) -> None:
    """Publish the staged Artifacts to their declared Distribution endpoints.

    The terminal release stage: refuses unless every LIVE upstream stage
    succeeded and sign succeeded-or-was-skipped (the explicit result inputs —
    a partial release is structurally impossible). --matrix/--stages carry
    the preflight plan's liveness facts verbatim: a plan-proven non-live
    build/bundle (empty matrix / no bundle stage — "the tag is the release")
    may be `skipped`; omitted facts keep the strict success-only contract,
    and `failure`/`cancelled` always block. It then walks the [artifacts] map and
    dispatches each declared endpoint through the closed adapter registry
    (gh-release, crates, pypi, npm, brew). A -release-rc VERSION publishes
    ONLY to the GH release (as prerelease) — every external endpoint is
    skipped. `release` endpoints run before `derived` ones (brew renders
    against the final asset URLs/SHAs). Every adapter treats
    already-published as success, so a re-run after a partial publish
    converges. VERSION is the concrete semver prepare cut — never a bump
    word (publish must not re-resolve the version).
    """
    if version.semver is None:
        raise click.UsageError(
            "publish takes the concrete version `release prepare` cut "
            "(e.g. 1.2.3) — a bump word would re-resolve against the tags "
            "and could disagree with what was prepared"
        )
    raise SystemExit(
        run_publish(
            version,
            build_result=build_result,
            bundle_result=bundle_result,
            sign_result=sign_result,
            matrix=matrix,
            stages=stages,
            assets=assets,
            notes=notes,
            testpypi=testpypi,
            as_json=as_json,
        )
    )
