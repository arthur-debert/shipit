"""Typed data model for the PR state engine.

Plain dataclasses + enums over the raw JSON `gh` returns. Holding the raw
snapshot in a `PullContext` is what keeps the rest of the package pure: a
test builds a context from recorded JSON and asserts on adapter/state output
without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ReviewLifecycle(StrEnum):
    """Where a single reviewer stands on a PR's *current head*."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    DONE_CLEAN = "done_clean"  # finished, left no comments
    DONE_COMMENTS = "done_comments"  # finished, left comments


@dataclass(frozen=True)
class ReviewComment:
    """One inline review comment (REST `databaseId` is the stable handle).

    `review_id` is the database id of the pull-request review the comment was
    submitted with (GraphQL `pullRequestReview.databaseId` == the REST review
    `id`). It is what groups thread findings into per-review cycles for the
    circuit breakers — there is no separate REST comment fetch anymore.
    """

    comment_id: int
    path: str
    line: int | None
    body: str
    author: str
    review_id: int | None = None


@dataclass(frozen=True)
class Thread:
    """A review thread (GraphQL node) and its resolution state.

    A thread's location/author come from its root comment; the GraphQL
    `thread_id` is what `resolveReviewThread` needs.
    """

    thread_id: str
    is_resolved: bool
    comments: tuple[ReviewComment, ...]

    @property
    def root(self) -> ReviewComment | None:
        return self.comments[0] if self.comments else None

    @property
    def path(self) -> str | None:
        return self.root.path if self.root else None

    @property
    def line(self) -> int | None:
        return self.root.line if self.root else None

    @property
    def root_comment_id(self) -> int | None:
        return self.root.comment_id if self.root else None

    @property
    def author(self) -> str | None:
        return self.root.author if self.root else None


@dataclass(frozen=True)
class Review:
    """A submitted review — one per reviewer per cycle."""

    review_id: int
    author: str
    state: str  # APPROVED / CHANGES_REQUESTED / COMMENTED / ...
    commit_id: str  # the head SHA this review was made against
    body: str


@dataclass
class PullContext:
    """Snapshot of all raw GitHub state the engine reads for one PR.

    Built once per call by `fetch.gather()`, then handed to the (pure)
    reviewer adapters and — in Phase 2 — the state machine.
    """

    number: int
    head_sha: str
    is_draft: bool
    base_ref: str | None = None  # base branch name (for diff-size breaker)
    mergeable: str | None = None  # gh: MERGEABLE / CONFLICTING / UNKNOWN
    merge_state: str | None = None  # gh: CLEAN / BLOCKED / BEHIND / ...
    reviews: list[Review] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)  # issue-level (Gemini eyes)
    issue_comments: list[dict] = field(default_factory=list)  # Gemini bot comments
    requested_logins: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)  # gh statusCheckRollup entries
    # Per-reviewer rerun policy (name -> rerun flag), resolved from config at the
    # build site (`fetch`/the CLI). rerun=True means head-strict (re-review every
    # push); rerun=False (the DEFAULT for any reviewer absent here) means
    # review-once: a review on ANY commit of the PR counts as done and is never
    # stale-after-push. The adapters read this to pick head-strict vs any-head
    # detection — keeping the policy data here, not a code branch per reviewer.
    reviewer_rerun: dict[str, bool] = field(default_factory=dict)

    def reviews_on_head(self) -> list[Review]:
        """Reviews made against the current head — stale reviews don't count."""
        return [r for r in self.reviews if r.commit_id == self.head_sha]

    def reviews_any_head(self) -> list[Review]:
        """All reviews on the PR, regardless of which commit they were made
        against — the review-once (rerun=False) lens: a review on an earlier head
        still counts, since the reviewer won't be asked to look again."""
        return list(self.reviews)

    def open_threads(self) -> list[Thread]:
        return [t for t in self.threads if not t.is_resolved]
