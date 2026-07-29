"""``session/resume`` — backend-neutral coordinator session resume resolution.

Read-only: turns durable JSONL records into a typed target, never launching one.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import git, identity, logsetup
from ..fleetsweep import DEFAULT_SOURCE_ROOT
from ..identity import Repo
from ..logread.records import parse_record

CODEX_BACKEND = "codex"
CLAUDE_BACKEND = "claude"


class ResumeError(RuntimeError):
    """A resume target could not be resolved safely."""


@dataclass(frozen=True)
class ResumeTarget:
    """The resolved target a backend-neutral resume will launch."""

    repo: Repo
    backend: str
    shipit_session_id: str
    native_session_id: str
    tree: str | None = None


def resolve(
    target: str | None,
    *,
    repo: Repo | None = None,
    last: bool = False,
    base_dir: str | Path | None = None,
) -> ResumeTarget:
    """``target`` is a shipit session id or a backend-native one; ``last`` needs
    ``repo``. Without ``repo`` every repo log is searched, failing closed on a tie.
    """

    if last:
        if target is not None:
            raise ResumeError("pass either a target or --last, not both")
        if repo is None:
            raise ResumeError("--last requires --repo owner/name")
        complete = _records_for_repos([repo], base_dir=base_dir)
        if not complete:
            raise ResumeError(f"no resumable sessions found for {repo.slug}")
        return complete[-1]

    if not target:
        raise ResumeError("pass a shipit session id, backend-native id, or --last")

    repos = [repo] if repo is not None else _discover_repos(base_dir=base_dir)
    records = _records_for_repos(repos, base_dir=base_dir)
    matches = _matches(records, target)
    if not matches:
        hint = (
            " in the selected repo"
            if repo is not None
            else "; pass --repo owner/name if needed"
        )
        raise ResumeError(f"no resume records found for {target!r}{hint}")

    unique = {
        (
            match.repo.slug,
            match.backend,
            match.shipit_session_id,
            match.native_session_id,
        )
        for match in matches
    }
    if len(unique) > 1:
        candidates = ", ".join(
            f"{m.repo.slug}:{m.backend}:{m.shipit_session_id}" for m in matches
        )
        raise ResumeError(
            f"resume target {target!r} is ambiguous; candidates: {candidates}. "
            "Pass --repo owner/name or a shipit session id."
        )
    return matches[-1]


def source_checkout_for_repo(repo: Repo, *, cwd: str | None = None) -> str:
    """A deterministic local checkout that can seed a fresh Tree for ``repo``.

    A Tree is never a candidate: resume must not depend on disposable state.
    """

    ambient = git.repo_root(cwd=cwd)
    if ambient is not None:
        try:
            if identity.resolve_repo(ambient) == repo:
                return ambient
        except Exception:  # noqa: BLE001 - a bad ambient checkout is not a match.
            pass

    matches = _matching_source_checkouts(repo, DEFAULT_SOURCE_ROOT.expanduser())
    if matches:
        return matches[0]
    raise ResumeError(
        f"no stable source checkout found for {repo.slug}; run from that repo's "
        f"checkout or clone it under {DEFAULT_SOURCE_ROOT.expanduser()} before resuming"
    )


def _matching_source_checkouts(repo: Repo, source_root: Path) -> list[str]:
    preferred = (
        source_root / repo.name,
        source_root / repo.owner.login / repo.name,
        source_root,
    )
    for checkout in preferred:
        if _checkout_matches(checkout, repo):
            return [str(checkout)]

    git_dirs: list[Path] = []
    if source_root.is_dir():
        git_dirs.extend(source_root.glob("*/.git"))
        git_dirs.extend(source_root.glob("*/*/.git"))
    matches: list[str] = []
    for path in sorted(git_dirs):
        checkout = path.parent
        if checkout not in preferred and _checkout_matches(checkout, repo):
            matches.append(str(checkout))
    return matches


def _checkout_matches(checkout: Path, repo: Repo) -> bool:
    if not (checkout / ".git").is_dir():
        return False
    try:
        return identity.resolve_repo(str(checkout)) == repo
    except Exception:  # noqa: BLE001 - ignore non-checkout/odd dirs.
        return False


def _matches(records: list[ResumeTarget], target: str) -> list[ResumeTarget]:
    return [
        record
        for record in records
        if record.shipit_session_id == target or record.native_session_id == target
    ]


def _records_for_repos(
    repos: Iterable[Repo], *, base_dir: str | Path | None
) -> list[ResumeTarget]:
    found: list[ResumeTarget] = []
    for repo in repos:
        path = logsetup.log_file_path(repo, base_dir=base_dir)
        paths = [
            path.with_name(f"{path.name}.{index}")
            for index in range(logsetup.BACKUP_COUNT, 0, -1)
        ]
        paths.append(path)
        records = (
            record
            for candidate in paths
            if candidate.exists()
            for record in _read_jsonl(candidate)
        )
        found.extend(_sessions(records, repo=repo))
    return found


def _discover_repos(*, base_dir: str | Path | None) -> list[Repo]:
    base = (
        Path(base_dir)
        if base_dir is not None
        # resolve_log_dir is <base>/<owner>/<repo>; climb to the owners root.
        else logsetup.resolve_log_dir(identity.repo_from_slug("x/y")).parent.parent
    )
    if not base.is_dir():
        return []
    repos: list[Repo] = []
    for owner in sorted(p for p in base.iterdir() if p.is_dir()):
        for name in sorted(p for p in owner.iterdir() if p.is_dir()):
            if (name / logsetup.LOG_FILENAME).exists():
                repos.append(identity.repo_from_slug(f"{owner.name}/{name.name}"))
    return repos


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = parse_record(line)
            if record is not None:
                yield record


def _sessions(
    records: Iterable[dict[str, Any]], *, repo: Repo | None = None
) -> list[ResumeTarget]:
    """One resumable target per session, folding fields across its records.

    A session missing a ``backend`` or a native id is dropped, not guessed at.
    """
    # Insertion order IS recency order: pop-and-reinsert moves a re-seen session
    # to the end, so one dict is both accumulator and newest-last ordering.
    fields: dict[str, dict[str, str]] = {}
    for record in records:
        session_id = record.get("session")
        if not isinstance(session_id, str) or not session_id:
            continue
        entry = fields.pop(session_id, {})
        for key in ("backend", "session_id", "codex_thread", "tree", "repo"):
            value = _str_field(record, key)
            if value:
                entry[key] = value
        fields[session_id] = entry

    targets: list[ResumeTarget] = []
    for session_id, entry in fields.items():
        backend = entry.get("backend")
        if backend is None:
            continue
        record_repo = repo or _repo_from_slug_field(entry.get("repo"))
        if record_repo is None:
            continue
        if backend == CODEX_BACKEND:
            native = entry.get("codex_thread") or entry.get("session_id")
        else:
            native = entry.get("session_id")
        if not native:
            continue
        targets.append(
            ResumeTarget(
                repo=record_repo,
                backend=backend,
                shipit_session_id=session_id,
                native_session_id=native,
                tree=entry.get("tree"),
            )
        )
    return targets


def _repo_from_slug_field(raw: str | None) -> Repo | None:
    if raw is None:
        return None
    try:
        return identity.repo_from_slug(raw)
    except ValueError:
        return None


def _str_field(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value else None
