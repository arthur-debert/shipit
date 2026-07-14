"""Typed data model for the PR state engine.

Plain dataclasses + enums over the raw JSON `gh` returns. Holding the raw
snapshot in a `ReadinessView` is what keeps the rest of the package pure: a
test builds a view from recorded JSON and asserts on adapter/state output
without touching the network.

`ReadinessView` is the **readiness path's** richer view (ADR-0024): it *composes*
a canonical `PR` (identity + cheap core) and adds the reviews / threads / funnel /
timing the engine reads. It replaces the old ``PullContext`` snapshot — the core
(`head_sha`, `is_draft`, `base_ref`, `merge_state`) now lives on the composed `PR`,
read via delegating properties so a core field is fetched one way (`pr.core_from_node`)
and never defaulted-in on this path.
"""

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
    DONE_CLEAN = "done_clean"  # finished, left no comments
    DONE_COMMENTS = "done_comments"  # finished, left comments


class FunnelState(StrEnum):
    """The ONE normalized funnel view per reviewer the OBS04 readiness engine reads (ADR-0006).

    The engine folds BOTH native reviewer signals (an App reviewer's
    ``review_requested`` edge + its review object, via ``ReviewLifecycle``) AND the
    OBS02/ADR-0005 check-run breadcrumb (a local-agent reviewer's
    ``review: <agent>-local`` run) into this single per-reviewer state, read
    uniformly across reviewer kinds. The mapping lives behind the adapter interface
    (`ReviewerAdapter.funnel_state`), so the engine never branches on a reviewer's
    name — it just asks each adapter for its funnel state and decides holds/settled
    on the result.

    The states split into three readiness verdicts (OBS04-WS02):

      * **holds** the PR at reviews-pending — ``NEVER_REQUESTED`` (start the loop)
        and ``IN_FLIGHT`` (a review is legitimately coming; WS03 splits this into
        within-window=holds vs past-window→``TIMED_OUT``). ``REQUESTED`` is the App
        reviewer's pre-review hold (the request edge placed, no review yet).
      * **settled, blocking-on-threads** — ``POSTED``: a review actually landed
        (incl. a clean zero-findings review *and* a salvaged COMMENT); its threads
        hold until resolved.
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
    POSTED = "posted"  # a review landed → settled, threads hold
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
    """A submitted review — one per reviewer per cycle.

    ``commit_id`` is the head the review was made against, carried as a
    :class:`shipit.identity.Sha` (COR02) so the staleness comparison against the
    PR's current head is full-vs-full by construction — a case or length mismatch
    can no longer silently flip a review to stale. ``None`` when the wire carried
    no commit (GitHub can omit it): honestly unknown, never a fake empty string.
    """

    review_id: int
    author: str
    state: str  # APPROVED / CHANGES_REQUESTED / COMMENTED / ...
    commit_id: Sha | None  # the head SHA this review was made against
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

    # ``reviewer`` carries the WIRE name verbatim — the ``review: `` prefix
    # stripped off the check-run's name — NOT a resolved Backend (#313, examined
    # against COR02's derive-from-the-registry invariant; the wire shape wins).
    # The rollup can legitimately carry funnel runs the registry cannot resolve
    # (a stale run from a since-removed backend, a foreign run squatting on the
    # reserved prefix), and the breadcrumb must carry those honestly rather than
    # crash or drop them at the fetch boundary. Nothing re-DERIVES a name from
    # this string: a consumer that needs the identity matches it against
    # ``Backend.check_run_name`` (`reviewers.funnel_check`) or resolves it via
    # ``agent.backend.by_check_run_name`` — the sanctioned registry inverse.
    reviewer: str  # the funnel reviewer name off the wire, e.g. "codex-local"
    status: str | None  # gh CheckRun status (COMPLETED ⇒ terminal; else in flight)
    conclusion: str | None  # terminal conclusion, or None while in flight
    started_at: str | None  # ISO-8601 tz-aware; WS03 ages the wait window against it


@dataclass
class ReadinessView:
    """The readiness path's view of one PR: a canonical :class:`shipit.pr.PR`
    (identity + cheap core) enriched with all the raw GitHub state the engine reads.

    Built once per call by `fetch.gather()`, then handed to the (pure) reviewer
    adapters and the state machine. It *composes* a :class:`PR` rather than
    re-declaring the core (ADR-0024): ``number`` / ``head_sha`` / ``is_draft`` /
    ``base_ref`` / ``merge_state`` are read straight off ``self.pr`` via delegating
    properties, so this path exposes exactly the core its ``PR`` fetched — no
    defaulted ``is_draft`` trap — while the engine's ``ctx.head_sha`` reads are
    unchanged.

    ``mergeable`` (gh: MERGEABLE / CONFLICTING / UNKNOWN) is a readiness-ONLY field
    — the async-stale fallback the merge-state check consults — so it lives on the
    view, not the shared core (the review path never fetches it).
    """

    pr: PR
    mergeable: str | None = None  # gh: MERGEABLE / CONFLICTING / UNKNOWN
    reviews: list[Review] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)  # issue-level (Gemini eyes)
    issue_comments: list[dict] = field(default_factory=list)  # Gemini bot comments
    requested_logins: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)  # gh statusCheckRollup entries
    # The OBS02/ADR-0005 local-review funnel breadcrumbs: the App-authored
    # `review: <reviewer>` check runs, lifted OUT of the CI `checks` rollup above
    # at the build site so a failed `review: codex-local` run can never make the
    # CI-checks verdict (`classify_checks`) read FAILING — the two concerns ride the
    # SAME `statusCheckRollup` on the wire but must not cross (see `fetch`). WS01
    # carries the raw breadcrumbs; OBS04-WS02/WS03 normalize + age them.
    review_funnel: list[ReviewFunnelCheck] = field(default_factory=list)
    # Injected wall-clock "now" (tz-aware UTC). The engine is a pure, stateless
    # function snapshot -> state and NEVER calls a clock itself; `gather()` stamps
    # this at fetch time and a test/fixture supplies a FIXED value, so a recorded
    # snapshot + a fixed "now" yields a deterministic state. WS01 only carries it;
    # OBS04-WS03 reads it to age the per-reviewer wait window.
    now: datetime | None = None
    # The reviewer configuration as ONE value (CLI01-WS04): the Roster, loaded
    # once at a verb boundary (`reviewers_config.load_roster`) and threaded on
    # here at the build site — so the engine/adapters read every per-reviewer
    # setting (required, rerun, wait window, run options) off the snapshot,
    # never the filesystem, and the settings can never disagree with each other.
    # rerun=True (the DEFAULT, including for any reviewer absent from the roster)
    # means head-strict (re-review every push); rerun=False means review-once: a
    # review on ANY commit of the PR counts as done and is never
    # stale-after-push. A reviewer without a `window_seconds` uses the shipped
    # 20m default (`reviewers.DEFAULT_WAIT_WINDOW`, applied by the adapter);
    # OBS04-WS03 ages an in-flight / requested-but-silent reviewer past its
    # window into TIMED_OUT (settled + degraded). The EMPTY roster is the
    # honest fixture default: no reviewer required, every setting at its
    # shipped default.
    roster: Roster = field(default_factory=Roster)
    # The `review_requested` edge time per requested login (login -> ISO-8601
    # tz-aware), sourced from the PR timeline's ReviewRequestedEvent at the build
    # site. GraphQL `reviewRequests` carries NO timestamp, so this is where an App
    # reviewer's request time — what WS03 ages its wait window against — lives. A
    # LOCAL reviewer has no requested edge (it ages its check run's `started_at`
    # instead) and so never appears here. Empty in a light/skip context.
    requested_at: dict[str, str] = field(default_factory=dict)
    # The write-once Severity overrides for this PR (ADR-0044): finding comment
    # id -> Severity, loaded ONCE from the dev-cycle event log at the gather
    # seam (`prstate.overrides.load_overrides`) and threaded on here — the
    # roster precedent — so the breaker and the classify verb read recorded
    # overrides off the snapshot, never the filesystem. An override is the TOP
    # rung of the severity precedence chain (it beats the machine marker, the
    # adapter mapping, the adapter's unclassified-severity policy, and the
    # `major` fail-safe); an id absent here simply resolves through the rest
    # of the chain, so nothing gates on this store. Empty is the honest
    # fixture default.
    overrides: dict[int, Severity] = field(default_factory=dict)
    # The first-sight registry for the OBSERVATIONAL dev-cycle events this
    # snapshot's evaluations witness (`round.detected`, `breaker.fired`,
    # `review.degraded` — ADR-0032). A passed value, never a module global
    # (ADR-0021 rule 4): `gather()` stamps the invocation's registry here (a
    # multi-gather verb like `pr next` threads ONE across its gathers), so
    # re-evaluating the same milestone within an invocation tags it once,
    # while a fresh view (a test fixture, a later invocation) starts clean.
    # Excluded from equality: it is invocation bookkeeping riding the view,
    # not snapshot data.
    sightings: events.Sightings = field(
        default_factory=events.Sightings, repr=False, compare=False
    )
    # Whether evaluating this snapshot should emit observational dev-cycle
    # events. Read-only status renders use False so repeated status reads do
    # not mint duplicate historical flow milestones; mutating/waiting drivers
    # keep the default True and thread Sightings through their invocation.
    emit_events: bool = field(default=True, repr=False, compare=False)

    # --- core, delegated to the composed PR (ADR-0024) ----------------------
    # The engine and adapters read `ctx.head_sha` / `ctx.is_draft` / … as before;
    # the fields themselves live once, on `self.pr`, so this path can only expose
    # a core it actually fetched (no re-declared, defaultable copy).
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
        """Reviews made against the current head — stale reviews don't count.

        A ``Sha``-vs-``Sha`` comparison (COR02): both sides are validated,
        lowercase-normalized FULL shas, so a case or length mismatch can no
        longer silently flip a review to stale — a raw-string ``commit_id``
        would refuse to compare at all. ``commit_id is None`` (wire carried no
        commit) honestly reads as not-on-head.
        """
        return [
            r
            for r in self.reviews
            if r.commit_id is not None and r.commit_id == self.head_sha
        ]

    def reviews_any_head(self) -> list[Review]:
        """All reviews on the PR, regardless of which commit they were made
        against — the review-once (rerun=False) lens: a review on an earlier head
        still counts, since the reviewer won't be asked to look again."""
        return list(self.reviews)

    def open_threads(self) -> list[Thread]:
        return [t for t in self.threads if not t.is_resolved]


#: The placeholder :class:`Repo` a hand-built :class:`ReadinessView` composes when
#: no repo identity is supplied. The readiness ENGINE keys on ``number``, never repo
#: identity, so a fixture/unit-test view that omits the repo still resolves
#: deterministically — the placeholder is invisible to every readiness decision. The
#: live path (`fetch.gather`) always passes the real, origin-derived repo.
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
    """Compose a :class:`ReadinessView` from flattened core values — the ergonomic
    builder for callers (and tests) that hold the core directly rather than a raw
    GitHub node.

    The core (`number`/`head_sha`/`is_draft`/`base_ref`/`merge_state`) is packed
    into a :class:`PR` — ``is_draft`` is required here too, so this convenience
    cannot reintroduce the defaulted-``is_draft`` trap. ``head_sha`` may arrive as
    a raw string and is minted into a :class:`shipit.identity.Sha` HERE (mirroring
    how ``review_view`` parses a slug into a ``Repo``), so a hand-built view
    carries the same validated identity the wire path does — a malformed head
    raises at construction. ``repo`` defaults to a placeholder identity because
    the readiness engine never keys on it. ``sightings`` threads a caller's
    first-sight registry across several hand-built views (mirroring `gather`);
    omitted, the view gets its own fresh one.
    """
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
