"""Opportunity Capture domain and Git-backed store writer."""

from __future__ import annotations

import re
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml

from . import config, git
from .execrun import ExecError
from .identity import Repo, repo_from_slug

SCHEMA_VERSION = 1
LIFECYCLE_INBOX = "inbox"


class OpportunityError(RuntimeError):
    """Opportunity capture failed with a user-actionable message."""


@dataclass(frozen=True)
class OpportunityStoreConfig:
    """The configured GitHub-backed Opportunity store."""

    repo: str


@dataclass(frozen=True)
class OpportunityCapture:
    """A validated v1 Opportunity capture payload."""

    repo: str
    source: str
    tags: tuple[str, ...]
    observation: str
    evidence: str
    suggested_next_step: str
    created_at: datetime


@dataclass(frozen=True)
class StoredOpportunity:
    """The result of writing one Opportunity to the store."""

    store_repo: str
    path: str
    commit_message: str


class StoreGitBoundary(Protocol):
    """The Git operations the store writer needs, injectable for tests."""

    def clone(self, url: str, dest: str) -> None: ...

    def add(self, paths: list[str], *, cwd: str) -> None: ...

    def commit(self, message: str, paths: list[str], *, cwd: str) -> None: ...

    def pull_rebase(self, branch: str, *, cwd: str, remote: str = "origin") -> None: ...

    def push(self, branch: str, *, cwd: str, remote: str = "origin") -> None: ...

    def current_branch(self, *, cwd: str) -> str | None: ...


class RealStoreGitBoundary:
    """Real Git-backed store boundary over :mod:`shipit.git`."""

    clone = staticmethod(git.clone)
    add = staticmethod(git.add)
    commit = staticmethod(git.commit)
    pull_rebase = staticmethod(git.pull_rebase)
    push = staticmethod(git.push)
    current_branch = staticmethod(git.current_branch)


def load_store_config(cfg: dict) -> OpportunityStoreConfig:
    """Read ``[project.opportunities].repo`` from a loaded ``.shipit.toml``."""

    project = cfg.get("project")
    if not isinstance(project, dict) or "opportunities" not in project:
        raise config.ConfigError(
            "missing [project.opportunities] in .shipit.toml; "
            'configure an Opportunity store with repo = "owner/name"'
        )
    opportunities = project["opportunities"]
    if not isinstance(opportunities, dict):
        raise config.ConfigError("[project.opportunities] must be a table")
    repo = opportunities.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        raise config.ConfigError(
            '[project.opportunities].repo must be a non-empty "owner/name" string'
        )
    try:
        parsed_repo = repo_from_slug(repo)
    except ValueError as exc:
        raise config.ConfigError(
            f'[project.opportunities].repo must look like "owner/name"; got {repo!r}'
        ) from exc
    return OpportunityStoreConfig(repo=parsed_repo.slug)


def validate_capture(capture: OpportunityCapture) -> None:
    """Validate the required v1 capture fields and body sections."""

    missing: list[str] = []
    if not capture.repo.strip():
        missing.append("repo")
    if not capture.source.strip():
        missing.append("source")
    if not capture.tags or any(not tag.strip() for tag in capture.tags):
        missing.append("tags")
    if capture.created_at.tzinfo is None:
        missing.append("created_at timezone")
    if not capture.observation.strip():
        missing.append("observation")
    if not capture.evidence.strip():
        missing.append("evidence")
    if not capture.suggested_next_step.strip():
        missing.append("suggested_next_step")
    if missing:
        raise OpportunityError(
            "missing required Opportunity capture field(s): " + ", ".join(missing)
        )


def render_opportunity(capture: OpportunityCapture) -> str:
    """Render a validated capture as a v1 inbox Opportunity markdown file."""

    validate_capture(capture)
    created = capture.created_at.astimezone(UTC).replace(microsecond=0)
    front_matter = {
        "schema_version": SCHEMA_VERSION,
        "repo": capture.repo.strip(),
        "source": capture.source.strip(),
        "tags": [tag.strip() for tag in capture.tags],
        "status": LIFECYCLE_INBOX,
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }
    header = yaml.safe_dump(front_matter, sort_keys=False).strip()
    return (
        f"---\n{header}\n---\n\n"
        f"## Observation\n\n{capture.observation.strip()}\n\n"
        f"## Evidence\n\n{capture.evidence.strip()}\n\n"
        f"## Suggested next step\n\n{capture.suggested_next_step.strip()}\n"
    )


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].strip("-") or "opportunity"


def allocate_inbox_path(
    store_root: Path,
    capture: OpportunityCapture,
    *,
    token_factory: Callable[[], str] | None = None,
) -> Path:
    """Allocate a collision-resistant ``inbox`` path for ``capture``."""

    token_factory = token_factory or (lambda: uuid.uuid4().hex[:12])
    stamp = (
        capture.created_at.astimezone(UTC)
        .replace(microsecond=0)
        .strftime("%Y%m%dT%H%M%SZ")
    )
    prefix = f"{stamp}-{_slug(capture.observation)}"
    inbox = store_root / LIFECYCLE_INBOX
    for _ in range(20):
        raw_token = re.sub(r"[^a-zA-Z0-9]", "", token_factory())[:12]
        token = raw_token or uuid.uuid4().hex[:12]
        candidate = inbox / f"{prefix}-{token}.md"
        if not candidate.exists():
            return candidate
    raise OpportunityError(
        "could not allocate a unique Opportunity path after 20 attempts"
    )


def store_clone_url(store_repo: str) -> str:
    """Return the HTTPS clone URL for a configured GitHub ``owner/name`` repo."""

    return f"https://github.com/{store_repo}.git"


def write_to_store(
    store: OpportunityStoreConfig,
    capture: OpportunityCapture,
    *,
    boundary: StoreGitBoundary | None = None,
    token_factory: Callable[[], str] | None = None,
    max_push_attempts: int = 2,
) -> StoredOpportunity:
    """Write, commit, and push one inbox Opportunity to the configured store."""

    boundary = boundary or RealStoreGitBoundary()
    content = render_opportunity(capture)
    with tempfile.TemporaryDirectory(prefix="shipit-opportunity-") as tmp:
        root = Path(tmp) / "store"
        try:
            boundary.clone(store_clone_url(store.repo), str(root))
            path = allocate_inbox_path(root, capture, token_factory=token_factory)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            rel = path.relative_to(root).as_posix()
            message = f"Capture Opportunity: {rel}"
            boundary.add([rel], cwd=str(root))
            boundary.commit(message, [rel], cwd=str(root))
            branch = boundary.current_branch(cwd=str(root)) or "main"
            _push_with_rebase_retry(
                boundary,
                cwd=str(root),
                branch=branch,
                attempts=max_push_attempts,
            )
        except OSError as exc:
            raise OpportunityError(
                f"failed to write Opportunity store file: {exc}"
            ) from exc
        except ExecError as exc:
            raise OpportunityError(f"failed to write Opportunity store: {exc}") from exc
    return StoredOpportunity(store_repo=store.repo, path=rel, commit_message=message)


def _push_with_rebase_retry(
    boundary: StoreGitBoundary, *, cwd: str, branch: str, attempts: int
) -> None:
    attempts = max(1, attempts)
    last: ExecError | None = None
    attempts_performed = 0
    for attempt in range(1, attempts + 1):
        attempts_performed = attempt
        try:
            boundary.push(branch, cwd=cwd)
            return
        except ExecError as exc:
            last = exc
            if attempt == attempts or not _is_non_fast_forward_push(exc):
                break
            boundary.pull_rebase(branch, cwd=cwd)
    raise OpportunityError(
        "failed to push Opportunity store update after "
        f"{attempts_performed} push attempt(s): {last}"
    )


def _is_non_fast_forward_push(exc: ExecError) -> bool:
    """Whether a failed push is the concurrent-writer race we can rebase."""

    if exc.cause != "exit":
        return False
    output = f"{exc.stdout}\n{exc.stderr}".lower()
    return "non-fast-forward" in output or (
        "[rejected]" in output and "(fetch first)" in output
    )


def make_capture(
    *,
    repo: Repo | str,
    source: str,
    tags: Sequence[str],
    observation: str,
    evidence: str,
    suggested_next_step: str,
    now: Callable[[], datetime] | None = None,
) -> OpportunityCapture:
    """Build a capture payload from CLI values and the ambient repo."""

    created_at = (now or (lambda: datetime.now(UTC)))()
    repo_slug = repo.slug if isinstance(repo, Repo) else str(repo)
    return OpportunityCapture(
        repo=repo_slug,
        source=source,
        tags=tuple(tags),
        observation=observation,
        evidence=evidence,
        suggested_next_step=suggested_next_step,
        created_at=created_at,
    )
