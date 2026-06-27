"""Typed data model for the PR state engine.

Plain dataclasses + enums over the raw JSON `gh` returns. Holding the raw
snapshot in a `PullContext` is what keeps the rest of the package pure: a
test builds a context from recorded JSON and asserts on adapter/state output
without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ReviewLifecycle(StrEnum):
    """Where a single reviewer stands on a PR's *current head*."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    DONE_CLEAN = "done_clean"  # finished, left no comments
    DONE_COMMENTS = "done_comments"  # finished, left comments


class FunnelState(StrEnum):
    """The ONE normalized funnel view per reviewer the OBS04 gate reads (ADR-0006).

    The engine folds BOTH native reviewer signals (an App reviewer's
    ``review_requested`` edge + its review object, via ``ReviewLifecycle``) AND the
    OBS02/ADR-0005 check-run breadcrumb (a local-agent reviewer's
    ``review: <agent>-local`` run) into this single per-reviewer state, read
    uniformly across reviewer kinds. The mapping lives behind the adapter interface
    (`ReviewerAdapter.funnel_state`), so the engine never branches on a reviewer's
    name — it just asks each adapter for its funnel state and gates on the result.

    The states split into three gate verdicts (OBS04-WS02):

      * **holds** the PR at reviews-pending — ``NEVER_REQUESTED`` (start the loop)
        and ``IN_FLIGHT`` (a review is legitimately coming; WS03 splits this into
        within-window=holds vs past-window→``TIMED_OUT``). ``REQUESTED`` is the App
        reviewer's pre-review hold (the request edge placed, no review yet).
      * **settled, blocking-on-threads** — ``POSTED``: a review actually landed
        (incl. a clean zero-findings review *and* a salvaged COMMENT); its threads
        gate until resolved.
      * **settled, NON-blocking + degraded** — ``FAILED`` / ``EMPTY`` /
        ``TIMED_OUT``: a recorded terminal outcome that is NOT a delivered review.
        It settles (does not hold Ready) but is surfaced loud as *degraded* so the
        state is never silently "fine."

    "Settled" is therefore *outcome-recorded*, not *review-succeeded*: every state
    except the three holds is a recorded terminal outcome.
    """

    NEVER_REQUESTED = "never_requested"  # no signal at all → holds (start the loop)
    REQUESTED = "requested"  # App request edge placed, no review yet → holds
    IN_FLIGHT = "in_flight"  # a review is running → holds (WS03 ages the window)
    POSTED = "posted"  # a review landed → settled, threads gate
    FAILED = "failed"  # the run errored → settled, degraded (non-blocking)
    EMPTY = "empty"  # nothing parseable returned → settled, degraded
    TIMED_OUT = "timed_out"  # exceeded the wait window → settled, degraded


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


@dataclass(frozen=True)
class ReviewFunnelCheck:
    """One OBS02/ADR-0005 funnel breadcrumb: the App-authored ``review: <reviewer>``
    check run that stands in for the ``review_requested`` edge GitHub denies a
    local-agent bot.

    A local reviewer (``codex-local`` / ``agy-local``) has no native pre-post
    signal, so shipit opens a check run named ``review: <reviewer>`` at kickoff
    (``status=in_progress``, ``started_at=now``) and closes it to a terminal
    ``conclusion`` at completion. This dataclass carries that run's RAW state off
    the head commit's status rollup. The funnel-STATE normalization
    (requested / in-flight / posted / failed / empty / timed-out) and the
    wait-window ageing of ``started_at`` are OBS04-WS02 / WS03 — WS01 only carries
    the breadcrumb so those workstreams read structure, not prose.

    Field names/casing mirror the gh ``statusCheckRollup`` CheckRun node the run
    arrives on: ``status`` (e.g. ``IN_PROGRESS`` / ``COMPLETED``), ``conclusion``
    (``SUCCESS`` / ``FAILURE`` / ``TIMED_OUT`` / ``NEUTRAL`` / ...), ``startedAt``.
    """

    reviewer: str  # the funnel reviewer name, e.g. "codex-local"
    status: str | None  # gh CheckRun status (COMPLETED ⇒ terminal; else in flight)
    conclusion: str | None  # terminal conclusion, or None while in flight
    started_at: str | None  # ISO-8601 tz-aware; WS03 ages the wait window against it


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
    # The OBS02/ADR-0005 local-review funnel breadcrumbs: the App-authored
    # `review: <reviewer>` check runs, lifted OUT of the CI `checks` rollup above
    # at the build site so a failed `review: codex-local` run can never make the
    # CI-checks gate (`classify_checks`) read FAILING — the two concerns ride the
    # SAME `statusCheckRollup` on the wire but must not cross (see `fetch`). WS01
    # carries the raw breadcrumbs; OBS04-WS02/WS03 normalize + age them.
    review_funnel: list[ReviewFunnelCheck] = field(default_factory=list)
    # Injected wall-clock "now" (tz-aware UTC). The engine is a pure, stateless
    # function snapshot -> state and NEVER calls a clock itself; `gather()` stamps
    # this at fetch time and a test/fixture supplies a FIXED value, so a recorded
    # snapshot + a fixed "now" yields a deterministic state. WS01 only carries it;
    # OBS04-WS03 reads it to age the per-reviewer wait window.
    now: datetime | None = None
    # Per-reviewer rerun policy (name -> rerun flag), resolved from config at the
    # build site (`fetch`/the CLI). rerun=True means head-strict (re-review every
    # push); rerun=False (the DEFAULT for any reviewer absent here) means
    # review-once: a review on ANY commit of the PR counts as done and is never
    # stale-after-push. The adapters read this to pick head-strict vs any-head
    # detection — keeping the policy data here, not a code branch per reviewer.
    reviewer_rerun: dict[str, bool] = field(default_factory=dict)
    # Per-reviewer wait-window override in SECONDS (name -> seconds), resolved from
    # the `[reviewers]` `window` option at the build site and threaded on here
    # EXACTLY like `reviewer_rerun` — so the engine reads the window off the
    # snapshot, never the filesystem. A reviewer ABSENT here uses the shipped 20m
    # default (`reviewers.DEFAULT_WAIT_WINDOW`, applied by the adapter). OBS04-WS03
    # ages an in-flight / requested-but-silent reviewer past this window into
    # TIMED_OUT (settled + degraded). Empty in a light/skip context that never gates.
    reviewer_window: dict[str, int] = field(default_factory=dict)
    # The `review_requested` edge time per requested login (login -> ISO-8601
    # tz-aware), sourced from the PR timeline's ReviewRequestedEvent at the build
    # site. GraphQL `reviewRequests` carries NO timestamp, so this is where an App
    # reviewer's request time — what WS03 ages its wait window against — lives. A
    # LOCAL reviewer has no requested edge (it ages its check run's `started_at`
    # instead) and so never appears here. Empty in a light/skip context.
    requested_at: dict[str, str] = field(default_factory=dict)

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
