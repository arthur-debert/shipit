"""`shipit release` — the Release pipeline's stage verbs.

Each stage is independently invocable: preflight, prepare, notes, bundle,
assert-bundle, sign, publish.
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

from .. import checks, config, events, execrun, gh, git, redact
from ..changelog import is_prerelease
from ..release import ReleaseError
from ..release import bump as bump_mod
from ..release import bundle as bundle_mod
from ..release import integrity as integrity_mod
from ..release import preflight as preflight_mod
from ..release import provisioning as provisioning_mod
from ..release import publish as publish_mod
from ..release import sign as sign_mod
from ..release import version as version_mod
from . import changelog as changelog_verb
from ._errors import cli_errors
from ._params import BARE_SEMVER, VERSION_SPEC, json_option
from ._render import emit
from ._tool import load_config

logger = logging.getLogger("shipit.release")

BUMP_TIMEOUT: float = 600.0

DEFAULT_NOTES_FILE = "RELEASE_NOTES.md"

_CHANGELOG_STAGE: tuple[str, ...] = ("CHANGELOG.md", "CHANGELOG/*")

BUNDLE_TIMEOUT: float = 3600.0

DEFAULT_BUNDLE_DIR = "dist"


@dataclass(frozen=True)
class PrepareResult:
    version: str
    tag: str
    release_sha: str
    prerelease: bool
    resume: bool
    tag_only: bool
    branch: str | None
    notes_path: str

    def to_dict(self) -> dict:
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
    plan_only: bool = False,
    as_json: bool = False,
    gitio: Any = git,
    env: Mapping[str, str] | None = None,
    resolve_ref: Callable[[str, str], bool] | None = None,
) -> int:
    """Plan the release; returns 0 on success, 1 on refusal."""
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
        events.emit(
            logger,
            "release.unsigned",
            "release preflight --unsigned: sign stage skipped for %s (%s)",
            release_plan.version,
            release_plan.tag,
            extra={"version": release_plan.version, "tag": release_plan.tag},
        )
    if not plan_only:
        missing = preflight_mod.missing_secrets(
            release_plan, os.environ if env is None else env
        )
        if missing:
            raise ReleaseError(
                f"missing required secrets: {', '.join(missing)} — the plan "
                "cannot run to publish; failing now, before prepare writes "
                "any history"
            )
        resolve = resolve_ref or gh.workflow_ref_resolves
        pins = checks.workflow_pin_refs(
            str(root / ".github" / "workflows" / checks.RELEASE_CALLER_WORKFLOW)
        )
        unresolved = [(repo, ref) for repo, ref in pins if not resolve(repo, ref)]
        if unresolved:
            raise ReleaseError(preflight_mod.missing_pin_refusal(unresolved))
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


def _run_bump(argv: Sequence[str], cwd: Path) -> None:
    execrun.run(list(argv), cwd=str(cwd), timeout=BUMP_TIMEOUT)


def _write_notes(notes_path: Path, text: str) -> None:
    try:
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"cannot write notes to {notes_path}: {exc}") from exc


def _unquote_status_path(field: str) -> str:
    """Decode one ``git status --porcelain`` path field."""
    if not field.startswith('"'):
        return field
    inner = field[1:-1]
    raw = inner.encode("latin-1", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "backslashreplace").decode("utf-8", "replace")


def _changed_paths(status_lines: list[str]) -> list[str]:
    paths = []
    for line in status_lines:
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(_unquote_status_path(path))
    return paths


def _leg_pathspecs(leg_path: str, patterns: Sequence[str]) -> list[str]:
    """``patterns`` joined onto a leg's map path (``"."`` is the repo root)."""
    if leg_path in (".", ""):
        return list(patterns)
    return [f"{leg_path}/{p}" for p in patterns]


def _matching(changed: Sequence[str], patterns: Sequence[str]) -> list[str]:
    return [
        path
        for path in changed
        if any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
    ]


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
    """Cut the release; returns 0 on success, 1 on refusal."""
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
    if notes_out:
        notes_arg = Path(notes_out)
        notes_path = notes_arg if notes_arg.is_absolute() else root / notes_arg
    else:
        notes_path = root / DEFAULT_NOTES_FILE

    _tree, plan = changelog_verb.plan_cut(
        root, version, read_tree=read_tree, today=today
    )

    if resolved.resume:
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
        raise ReleaseError(
            "detached HEAD — `release prepare` commits on the release branch"
        )
    base_sha = gitio.head_commit(cwd=cwd)
    if base_sha is None:
        raise ReleaseError("cannot read HEAD — is this an empty repository?")

    intended: list[str] = []
    expects: list[tuple[str, list[str]]] = []
    for entry in entries:
        adapter = bump_mod.adapter_for(entry.toolchain)
        leg_dir = root if entry.path in (".", "") else root / entry.path
        for argv in adapter.commands(version):
            try:
                run_cmd(argv, leg_dir)
            except execrun.ExecError as exc:
                remedy = provisioning_mod.missing_tool_remedy(
                    argv, exc.cause
                ) or bump_mod.explain_command_failure(argv, exc.stderr)
                if remedy is None:
                    raise
                raise ReleaseError(remedy) from exc
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
        gitio.add(to_commit, cwd=cwd)
        gitio.commit(f"release: {version}", to_commit, cwd=cwd)
    release_sha = gitio.head_commit(cwd=cwd)
    if release_sha is None:  # pragma: no cover — HEAD read just succeeded
        raise ReleaseError("cannot read HEAD after the bump commit")

    gitio.tag_annotated(resolved.tag, plan.notes, cwd=cwd)
    try:
        if resolved.tag_only:
            if to_commit:
                gitio.reset_hard(str(base_sha), cwd=cwd)
            gitio.push_tag(resolved.tag, cwd=cwd)
        else:
            gitio.push_atomic(branch, resolved.tag, cwd=cwd)
    except Exception:
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


@cli_errors
def run_notes(
    version: str,
    *,
    out: str | None = None,
    gitio: Any = git,
    read_tree: Callable[[Path], changelog_verb.ChangelogTree] | None = None,
) -> int:
    """Re-emit the coalesced notes for an already-cut ``version``; returns 0 or 1."""
    root_s = gitio.repo_root(cwd=".")
    if root_s is None:
        raise ReleaseError(
            "not inside a git checkout — `release notes` re-derives the "
            "notes from a tag's checked-out CHANGELOG/ tree"
        )
    root = Path(root_s)

    _tree, plan = changelog_verb.plan_cut(root, version, read_tree=read_tree)
    if plan.mutates:
        raise ReleaseError(
            f"CHANGELOG/ carries uncut fragments and no {version} section — "
            "this checkout is not an already-cut state, so there are no "
            f"prepare-produced notes for {version} to re-emit; `release "
            "notes` re-derives, it never cuts (run `shipit release prepare` "
            "for a fresh cut)"
        )

    if out:
        out_arg = Path(out)
        notes_path = out_arg if out_arg.is_absolute() else root / out_arg
        _write_notes(notes_path, plan.notes)
        print(f"notes: wrote {notes_path} ({version})")
    else:
        print(plan.notes, end="")
    logger.info(
        "release notes re-derived",
        extra={"version": version, "out": out or "-"},
    )
    return 0


@dataclass(frozen=True)
class BundleResult:
    target: str
    out: str
    composed: tuple[bundle_mod.Composed, ...]
    skipped: tuple[tuple[str, str], ...]
    passthrough: tuple[str, ...]

    def to_dict(self) -> dict:
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
    """Compose the unsigned Artifacts; returns 0 on success, 1 on refusal."""
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
    artifact_deps = config.load_artifact_deps(cfg)
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
    build_target = target
    out_arg = Path(out) if out else Path(DEFAULT_BUNDLE_DIR)
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
                    build_target=build_target,
                    artifact_deps=artifact_deps,
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


def format_assert_bundle(verdict: integrity_mod.BundleVerdict) -> str:
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
    """Guard the bundle tree at ``tree``; returns 0 on success, 1 on refusal."""
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


def format_sign(result: sign_mod.SignResult) -> str:
    staple = "stapled" if result.stapled else "staple failed (non-fatal)"
    return "\n".join(
        (
            f"release: signed + notarized {result.app} -> {result.dmg}",
            f"  identity  {result.identity}",
            f"  nested    {result.nested_signed} nested signable(s) signed before the .app",
            f"  notary    {result.submission_id} ({staple})",
        )
    )


def format_sign_archives(result: sign_mod.ArchiveSignResult) -> str:
    count = len(result.binaries)
    lines = [
        f"release: signed + notarized {count} "
        f"binar{'ies' if count != 1 else 'y'} across "
        f"{len(result.archives)} archive(s)",
        f"  identity  {result.identity}",
    ]
    lines.extend(
        f"  notary    {name}: {submission_id} (no staple — bare binary)"
        for name, submission_id in zip(
            result.binaries, result.submission_ids, strict=True
        )
    )
    lines.extend(f"  archive   {archive}" for archive in result.archives)
    return "\n".join(lines)


def _run_sign_cmd(argv: Sequence[str], timeout: float) -> execrun.ExecResult:
    return execrun.run(list(argv), timeout=timeout)


@cli_errors
def run_sign(
    tree: str,
    *,
    out: str | None = None,
    notary_timeout: int = sign_mod.DEFAULT_NOTARY_TIMEOUT_MIN,
    as_json: bool = False,
    run_cmd: sign_mod.RunCmd | None = None,
    env: Mapping[str, str] | None = None,
    uniq: Callable[[], str] | None = None,
    mint_pass: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Sign the bundle tree at ``tree``; returns 0 on success, 1 on refusal."""
    run_cmd = run_cmd or _run_sign_cmd
    tree_path = Path(tree)
    out_arg = Path(out) if out else tree_path
    seams: dict[str, Any] = {}
    for name, value in (("uniq", uniq), ("mint_pass", mint_pass), ("sleep", sleep)):
        if value is not None:
            seams[name] = value
    shape = sign_mod.detect_shape(tree_path)
    scratch = Path(tempfile.mkdtemp(prefix="shipit-sign-"))
    try:
        request = sign_mod.SignRequest(
            tree=tree_path,
            out_dir=out_arg,
            scratch=scratch,
            run_cmd=run_cmd,
            env=os.environ if env is None else env,
            timeout_minutes=notary_timeout,
            **seams,
        )
        if shape == "archive":
            archive_result = sign_mod.sign_archives(request)
        else:
            result = sign_mod.sign_bundle(request)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if shape == "archive":
        emit(archive_result, format_sign_archives, as_json=as_json)
        logger.info(
            "release sign complete (archive leg)",
            extra={
                "archives": ", ".join(archive_result.archives),
                "binaries": ", ".join(archive_result.binaries),
                "submission_ids": ", ".join(archive_result.submission_ids),
            },
        )
        return 0
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


PUBLISH_TIMEOUT: float = 3600.0


@dataclass(frozen=True)
class PublishResult:
    version: str
    tag: str
    prerelease: bool
    live_fire: bool
    published: tuple[publish_mod.Published, ...]
    skipped: tuple[tuple[str, str, str], ...]

    def to_dict(self) -> dict:
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
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        env=dict(env) if env else None,
        timeout=PUBLISH_TIMEOUT,
    )


def _probe_publish_cmd(
    argv: Sequence[str], cwd: Path, env: Any = None
) -> execrun.ExecResult:
    """Run one adapter command as a probe: a nonzero rc is data, not a failure."""
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
    endpoint_selector: Sequence[str] | None = None,
    as_json: bool = False,
    gitio: Any = git,
    ghio: Any = gh,
    run_cmd: publish_mod.RunCmd | None = None,
    probe: publish_mod.Probe | None = None,
    env: Any = None,
) -> int:
    """Publish the staged Artifacts; returns 0 on success, 1 on refusal."""
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

    assert spec.semver is not None
    version = spec.semver
    tag = f"{version_mod.TAG_PREFIX}{version}"
    prerelease = is_prerelease(version)
    live_fire = publish_mod.is_live_fire(version)

    assets_arg = Path(assets) if assets else Path(DEFAULT_BUNDLE_DIR)
    assets_dir = assets_arg if assets_arg.is_absolute() else root / assets_arg
    notes_arg = Path(notes) if notes else Path(DEFAULT_NOTES_FILE)
    notes_path = notes_arg if notes_arg.is_absolute() else root / notes_arg

    dispatches = publish_mod.plan(
        artifacts,
        prerelease=prerelease,
        live_fire=live_fire,
        selector=endpoint_selector,
    )

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

    repo: str | None = None
    if any(d.skip is None and d.adapter.needs_repo for d in dispatches):
        repo = ghio.current_repo(cwd=str(root)).slug

    published: list[publish_mod.Published] = []
    skipped: list[tuple[str, str, str]] = []
    for dispatch in dispatches:
        if dispatch.skip is not None:
            skipped.append(
                (dispatch.artifact.name, dispatch.adapter.name, dispatch.skip)
            )
            continue
        try:
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
        except execrun.ExecError as exc:
            remedy = provisioning_mod.missing_tool_remedy(exc.argv, exc.cause)
            if remedy is None:
                raise
            raise ReleaseError(
                f"[artifacts.{dispatch.artifact.name}] "
                f"{dispatch.adapter.name}: {remedy}"
            ) from exc

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


@click.group(name="release")
def release() -> None:
    """The release pipeline, one independently invocable stage per subcommand."""


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
@click.option(
    "--plan-only",
    is_flag=True,
    help=(
        "Emit the plan facts without the secret-presence hard-fail: the "
        "stage blocks' standalone plan job (per-stage dispatch, #780) "
        "re-derives the plan at the tag in a secret-free environment — "
        "presence was the source run's preflight's job, and each stage's "
        "verb still validates its own names before acting."
    ),
)
@json_option
def preflight_cmd(
    version: version_mod.VersionSpec,
    event: str,
    unsigned: bool,
    plan_only: bool,
    as_json: bool,
) -> None:
    """Plan the release: matrix, live stages, endpoints, required secrets."""
    raise SystemExit(
        run_preflight(
            version,
            event=event,
            unsigned=unsigned,
            plan_only=plan_only,
            as_json=as_json,
        )
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
    """Prepare the release: bump, changelog roll, commit, annotated tag, push."""
    raise SystemExit(run_prepare(version, notes_out=notes_out, as_json=as_json))


@release.command(name="notes")
@click.argument("version", type=BARE_SEMVER)
@click.option(
    "--out",
    type=click.Path(dir_okay=False),
    help=(
        "Write the notes text to FILE (relative paths anchor to the repo "
        "root, like prepare's --notes-out) with a report on stdout; omitted, "
        "the notes print verbatim to stdout."
    ),
)
def notes_cmd(version: str, out: str | None) -> None:
    """Re-emit the release-notes text for an already-cut VERSION."""
    raise SystemExit(run_notes(version, out=out))


@release.command(name="bundle")
@click.option(
    "--target",
    metavar="TRIPLE",
    help=(
        "The target triple the bundles are named for (<name>-<target>); "
        "default: derived from this host. An EXPLICIT --target is also the "
        "cross signal (TOL02-WS11): the build was `shipit build --target "
        "<triple>`, so archive/deb read the binary from target/<triple>/"
        "release/. Omitted (host default) reads the native target/release/. "
        "Pass the SAME triple to build and bundle."
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
    """Compose build outputs into the unsigned Artifacts."""
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
    """Assert the bundle tree's main binary is the expected app."""
    raise SystemExit(
        run_assert_bundle(tree, artifact=artifact, expected=expected, as_json=as_json)
    )


@release.command(name="sign")
@click.argument("tree", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--out",
    type=click.Path(file_okay=False),
    help=(
        "Stage the signed outputs here — the mac-app leg's .dmg or the "
        "archive leg's re-emitted .tar.gz (default: TREE itself, replacing "
        "the unsigned files under their original filenames)."
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
    notary_timeout: int,
    as_json: bool,
) -> None:
    """Sign and notarize an unsigned mac bundle tree."""
    raise SystemExit(
        run_sign(
            tree,
            out=out,
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
@click.option(
    "--endpoint",
    "endpoints",
    multiple=True,
    help=(
        "Publish ONLY this endpoint (repeatable); every other declared "
        "endpoint is skipped with a stated selector reason. Per-invocation "
        "only — never a .shipit.toml field. `gh-release` cannot be "
        "deselected (it is the Release). Omitted: the full plan fires."
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
    endpoints: tuple[str, ...],
    as_json: bool,
) -> None:
    """Publish the staged Artifacts to their declared Distribution endpoints."""
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
            endpoint_selector=list(endpoints) or None,
            as_json=as_json,
        )
    )
