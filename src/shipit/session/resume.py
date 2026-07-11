"""Backend-neutral coordinator session resume resolution.

The resume command takes human-facing shipit session ids (``codex-...`` /
``sess-...``), backend-native ids, or ``--last --repo`` and resolves them from
shipit's durable per-repo JSONL logs. The resolver is deliberately read-only:
it turns records into a typed target; backend-specific launch mechanics stay in
``shipit.verbs.session``.
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
_SESSION_PREFIXES = {CODEX_BACKEND: "codex-", CLAUDE_BACKEND: "sess-"}


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
    """Resolve ``target`` or ``--last`` to a unique backend-specific resume target.

    ``target`` may be a shipit session id (preferred) or a backend-native id.
    When ``last`` is true, ``repo`` is required and the newest complete session
    for that repository wins. Without ``repo`` a target lookup searches every
    repo log under the durable log base and fails closed if multiple repos match.
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
    """Return a deterministic local checkout that can seed a fresh Tree for ``repo``.

    The ambient checkout wins only when its origin resolves to ``repo``. Otherwise
    shipit's established local source root (``~/h``, shared with fleet sweep) is
    searched to a bounded depth for a matching canonical checkout. Trees are not
    source checkouts: choosing one makes resume depend on disposable agent/review
    state, and recursively walking a busy Tree root is unbounded. If no stable
    source exists, fail actionably instead of falling back to an arbitrary Tree.
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
    """Matching clones at the canonical root, one or two path segments deep."""

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
        else logsetup.resolve_log_dir(identity.repo_from_slug("x/y")).parents[1]
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
    by_session: dict[str, ResumeTarget] = {}
    for record in records:
        session_id = record.get("session")
        if not isinstance(session_id, str) or not session_id:
            continue
        backend = _backend_for_session(session_id)
        if backend is None:
            continue
        record_repo = repo or _repo_from_record(record)
        if record_repo is None:
            continue
        previous = by_session.get(session_id)
        native = _native_id(record, backend) or (
            previous.native_session_id if previous is not None else ""
        )
        tree = _str_field(record, "tree") or (previous.tree if previous else None)
        # Dict assignment preserves a key's original position. Reinsert so
        # ``--last`` follows the session's newest record even when its earlier
        # fields came from a rotated file before another session was seen.
        by_session.pop(session_id, None)
        by_session[session_id] = ResumeTarget(
            repo=record_repo,
            backend=backend,
            shipit_session_id=session_id,
            native_session_id=native,
            tree=tree,
        )
    return [target for target in by_session.values() if target.native_session_id]


def _backend_for_session(session_id: str) -> str | None:
    for backend, prefix in _SESSION_PREFIXES.items():
        if session_id.startswith(prefix):
            return backend
    return None


def is_shipit_session_id(value: str) -> bool:
    """Whether ``value`` uses a recognizable shipit coordinator-session prefix."""

    return _backend_for_session(value) is not None


def _native_id(record: dict[str, Any], backend: str) -> str | None:
    if backend == CODEX_BACKEND:
        return _str_field(record, "codex_thread") or _str_field(record, "session_id")
    return _str_field(record, "session_id")


def _repo_from_record(record: dict[str, Any]) -> Repo | None:
    raw = _str_field(record, "repo")
    if raw is None:
        return None
    try:
        return identity.repo_from_slug(raw)
    except ValueError:
        return None


def _str_field(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value else None
