"""Typed data model for the PR state engine, over the raw JSON `gh` returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .. import events
from ..finding import Severity
from ..identity import Repo, Sha, repo_from_slug
from ..pr import PR, PrId
from .roster import Roster


class ReviewLifecycle(StrEnum):
    """Where a single reviewer stands on a PR's *current head*."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    DONE_CLEAN = "done_clean"
    DONE_COMMENTS = "done_comments"


class FunnelState(StrEnum):
    """The normalized per-reviewer funnel view: the first three hold the PR,
    POSTED settles with its threads, the last three settle degraded."""

    NEVER_REQUESTED = "never_requested"
    REQUESTED = "requested"
    IN_FLIGHT = "in_flight"
    POSTED = "posted"
    FAILED = "failed"
    EMPTY = "empty"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class ReviewComment:
    """One inline review comment; `review_id` groups findings into cycles."""

    comment_id: int
    path: str
    line: int | None
    body: str
    author: str
    review_id: int | None = None


@dataclass(frozen=True)
class Thread:
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
    """A submitted review; ``commit_id`` is ``None`` when the wire carried none."""

    review_id: int
    author: str
    state: str
    commit_id: Sha | None
    body: str


@dataclass(frozen=True)
class ReviewFunnelCheck:
    """One funnel breadcrumb: the App-authored ``review: <reviewer>`` check
    run standing in for the request edge GitHub denies a local-agent bot."""

    # The wire name verbatim: the rollup can carry funnel runs the registry
    # cannot resolve, and the breadcrumb must carry those honestly.
    reviewer: str
    status: str | None  # COMPLETED ⇒ terminal; else in flight
    conclusion: str | None
    started_at: str | None  # ISO-8601 tz-aware; the wait window ages against it


@dataclass
class ReadinessView:
    """One PR's :class:`PR` plus the raw GitHub state the engine reads."""

    pr: PR
    mergeable: str | None = None
    reviews: list[Review] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    issue_comments: list[dict] = field(default_factory=list)
    requested_logins: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    # Lifted out of `checks`, so a failed funnel run never reads as bad CI.
    review_funnel: list[ReviewFunnelCheck] = field(default_factory=list)
    now: datetime | None = None
    roster: Roster = field(default_factory=Roster)
    # Per-login `review_requested` edge time; a local reviewer has none.
    requested_at: dict[str, str] = field(default_factory=dict)
    # Write-once Severity overrides by comment id; the chain's top rung.
    overrides: dict[int, Severity] = field(default_factory=dict)
    sightings: events.Sightings = field(
        default_factory=events.Sightings, repr=False, compare=False
    )
    # Read-only renders pass False so repeated reads mint no milestones.
    emit_events: bool = field(default=True, repr=False, compare=False)

    @property
    def number(self) -> int:
        return self.pr.number

    @property
    def head_sha(self) -> Sha:
        return self.pr.head_sha

    @property
    def is_draft(self) -> bool:
        return self.pr.is_draft

    @property
    def base_ref(self) -> str | None:
        return self.pr.base_ref

    @property
    def merge_state(self) -> str | None:
        return self.pr.merge_state

    def reviews_on_head(self) -> list[Review]:
        """Reviews on the current head; a commit-less one is not-on-head."""
        return [
            r
            for r in self.reviews
            if r.commit_id is not None and r.commit_id == self.head_sha
        ]

    def reviews_any_head(self) -> list[Review]:
        return list(self.reviews)

    def open_threads(self) -> list[Thread]:
        return [t for t in self.threads if not t.is_resolved]


#: The placeholder repo: the engine keys on ``number``, never identity.
_HANDBUILT_REPO = repo_from_slug("local/local")


def readiness_view(
    *,
    number: int,
    head_sha: str | Sha,
    is_draft: bool,
    base_ref: str | None = None,
    merge_state: str | None = None,
    repo: Repo | None = None,
    mergeable: str | None = None,
    reviews: list[Review] | None = None,
    threads: list[Thread] | None = None,
    reactions: list[dict] | None = None,
    issue_comments: list[dict] | None = None,
    requested_logins: list[str] | None = None,
    checks: list[dict] | None = None,
    review_funnel: list[ReviewFunnelCheck] | None = None,
    now: datetime | None = None,
    roster: Roster | None = None,
    requested_at: dict[str, str] | None = None,
    overrides: dict[int, Severity] | None = None,
    sightings: events.Sightings | None = None,
    emit_events: bool = True,
) -> ReadinessView:
    """Compose a :class:`ReadinessView` from flattened core values; a raw
    ``head_sha`` string is minted here, so a malformed head raises."""
    pr = PR(
        id=PrId(repo=repo or _HANDBUILT_REPO, number=number),
        head_sha=head_sha if isinstance(head_sha, Sha) else Sha(head_sha),
        base_ref=base_ref,
        is_draft=is_draft,
        merge_state=merge_state,
    )
    return ReadinessView(
        pr=pr,
        mergeable=mergeable,
        reviews=reviews if reviews is not None else [],
        threads=threads if threads is not None else [],
        reactions=reactions if reactions is not None else [],
        issue_comments=issue_comments if issue_comments is not None else [],
        requested_logins=requested_logins if requested_logins is not None else [],
        checks=checks if checks is not None else [],
        review_funnel=review_funnel if review_funnel is not None else [],
        now=now,
        roster=roster if roster is not None else Roster(),
        requested_at=requested_at if requested_at is not None else {},
        overrides=overrides if overrides is not None else {},
        sightings=sightings if sightings is not None else events.Sightings(),
        emit_events=emit_events,
    )
