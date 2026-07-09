"""Reviewer adapters — the only place that knows reviewer-specific mechanics.

The state machine and the CLI consume the adapter interface (`required`,
`detect`, `open_threads` on the read side; `request`, `cancel`,
`instruction_files` on the act side) and never branch on a reviewer's name.
Adding a reviewer is adding an adapter to `REGISTRY`; nothing downstream
changes. This is what keeps the core stable as the coding-agent landscape
shifts.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from .. import gh
from ..agent import backend as _agent_backend
from ..finding import Severity
from ..pr import PrId
from .errors import PrStateError
from .model import (
    FunnelState,
    ReadinessView,
    ReviewFunnelCheck,
    ReviewLifecycle,
    Thread,
)
from .roster import Roster, RosterEntry

#: The engine's logger (shared name with :mod:`shipit.prstate.state`): reviewer
#: request/cancel transitions are lifecycle milestones (glassbox spray, LOG02) —
#: today their only record is the verb's user-facing print, which vanishes with
#: the terminal, so the transition is ALSO recorded here at the adapter act.
logger = logging.getLogger("shipit.prstate")


def _log_request_transition(reviewer: str, pr: PrId, transition: str) -> None:
    """One INFO record per reviewer request-edge transition (placed/withdrawn).

    Recorded HERE, at the adapter act, so every requestable adapter logs the
    same shape without each caller re-rolling it: flat ``reviewer``/``pr``/
    ``transition`` fields under the human-readable line.
    """
    logger.info(
        "reviewer %s: %s on pr#%s",
        reviewer,
        transition,
        pr.number,
        extra={"reviewer": reviewer, "pr": pr.number, "transition": transition},
    )


# The shipped uniform wait window (ADR-0006): a required reviewer still in flight /
# requested-but-silent that ages PAST this window settles as TIMED_OUT. 20m
# default, overridable per-reviewer via the `[reviewers]` `window` option (carried
# on the reviewer's Roster entry, `ctx.roster`). Uniform across reviewer kinds — a local reviewer
# ages against its check run's `started_at`, an App reviewer against its
# `review_requested` edge time — so a slow backend gets a longer window without
# loosening it for everyone.
DEFAULT_WAIT_WINDOW = timedelta(minutes=20)

# The ONLY funnel states the wait window ages: a reviewer still legitimately
# working. IN_FLIGHT (a review is running) and REQUESTED (an App request edge
# placed, no review yet) are the holds the window can convert to TIMED_OUT; every
# other state is already terminal (POSTED / FAILED / EMPTY / TIMED_OUT) or a
# never-started hold (NEVER_REQUESTED, which has no request timestamp to age) and
# is never re-aged.
_AGEABLE = (FunnelState.IN_FLIGHT, FunnelState.REQUESTED)


def _age_to_timeout(
    state: FunnelState,
    request_at: str | None,
    window: timedelta,
    now: datetime | None,
) -> FunnelState:
    """Age an in-flight / requested reviewer past its wait window into TIMED_OUT.

    The pure timeout function (ADR-0006/WS03): a function of (now, request
    timestamp, window) and nothing else — the engine calls no clock; "now" is the
    injected `ctx.now`. Only an `_AGEABLE` state (IN_FLIGHT / REQUESTED — a reviewer
    still legitimately working) can age; every other state is already settled and
    returned unchanged. With both timestamps present, a reviewer whose age
    (`now - request_at`) EXCEEDS its window has gone silent past the deadline →
    TIMED_OUT (the engine then settles it non-blocking + degraded); within the window
    it HOLDS (returned unchanged). Missing either timestamp — no injected `now`, or
    a reviewer with no recorded request time (best-effort Gemini has no requested
    edge) — cannot be aged and so holds: the window never invents a timeout from
    absent data.
    """
    if state not in _AGEABLE:
        return state
    if not request_at or now is None:
        return state
    if now - datetime.fromisoformat(request_at) > window:
        return FunnelState.TIMED_OUT
    return state


# How a reviewer's native `ReviewLifecycle` (the App-reviewer signal) folds into
# the normalized `FunnelState` (ADR-0006). This is the App/native side of the
# funnel: it has no check-run breadcrumb, so its whole funnel view comes from the
# lifecycle. A local-agent reviewer overrides `funnel_state` to read its breadcrumb
# instead (a posted review still short-circuits to POSTED there). `IN_PROGRESS`
# only arises for the best-effort Gemini adapter (an "eyes" reaction); it maps to
# IN_FLIGHT for uniformity, though Gemini is never a required/blocking reviewer.
_LIFECYCLE_TO_FUNNEL: dict[ReviewLifecycle, FunnelState] = {
    ReviewLifecycle.DONE_CLEAN: FunnelState.POSTED,
    ReviewLifecycle.DONE_COMMENTS: FunnelState.POSTED,
    ReviewLifecycle.IN_PROGRESS: FunnelState.IN_FLIGHT,
    ReviewLifecycle.REQUESTED: FunnelState.REQUESTED,
    ReviewLifecycle.NOT_REQUESTED: FunnelState.NEVER_REQUESTED,
}


def _funnel_state_from_check(check: ReviewFunnelCheck) -> FunnelState:
    """Normalize a local reviewer's OBS02/ADR-0005 check-run breadcrumb to a
    `FunnelState` — the conclusion mapping ADR-0005 fixes.

    A run that has not COMPLETED is still IN_FLIGHT (`status` is
    ``in_progress`` / ``queued`` / ``waiting`` / ...). A completed run maps by
    ``conclusion``:

      * ``SUCCESS`` → POSTED (a review landed, incl. a clean zero-findings one);
      * ``TIMED_OUT`` → TIMED_OUT (the producer recorded a timeout);
      * ``NEUTRAL`` → EMPTY (the producer's *empty* non-delivery — nothing
        parseable; ADR-0005's accepted ``neutral`` mapping, which lets THIS engine
        tell empty apart from a hard failure WITHOUT the snapshot carrying the
        check-run ``output`` text — see `shipit.review.service._FUNNEL_TERMINAL`);
      * anything else terminal (``FAILURE`` / ``CANCELLED`` / ``STARTUP_FAILURE``
        / ``ACTION_REQUIRED`` / ...) → FAILED.

    EMPTY / FAILED / TIMED_OUT are all *settled + degraded* in the engine; they
    differ only in the human-facing "why". WS03 adds the second path to TIMED_OUT:
    an IN_FLIGHT run whose `started_at` has aged past the wait window.
    """
    if (check.status or "").upper() != "COMPLETED":
        return FunnelState.IN_FLIGHT
    conclusion = (check.conclusion or "").upper()
    if conclusion == "SUCCESS":
        return FunnelState.POSTED
    if conclusion == "TIMED_OUT":
        return FunnelState.TIMED_OUT
    if conclusion == "NEUTRAL":
        return FunnelState.EMPTY
    return FunnelState.FAILED


def _funnel_recency_key(check: ReviewFunnelCheck) -> datetime:
    """Recency signal for picking the live run among same-name funnel checks.

    `statusCheckRollup` ordering is not a documented recency contract, so we sort
    on the run's own `started_at` (ISO-8601, tz-aware; the App stamps it at kickoff
    and it never moves) rather than trusting the rollup's list order. A breadcrumb
    that somehow arrives without a `started_at` sorts earliest so a timestamped run
    always wins over it.
    """
    if not check.started_at:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(check.started_at)


class ReviewerAdapter:
    """Base adapter. Subclasses define the read side (`matches`, `detect`) and
    the act side (`request`, `cancel`); `instruction_files` declares where the
    reviewer's per-repo code-review instructions live."""

    name: str = ""
    # Whether this adapter HAS a request mechanism (a real `review_requested`
    # edge it can place + the #614 attach-verification). Best-effort
    # auto-triggering backends (Gemini) set this False and can never be a
    # required, blocking reviewer. WHICH requestable adapters are *currently*
    # required is NOT decided here — it is the config knob in
    # `reviewers_config` (release#622); this flag only marks eligibility.
    requestable: bool = False
    # Whether this reviewer is addressed by a GitHub `review_requested` edge (so
    # `requested_logins` is meaningful for it). App reviewers (Copilot,
    # CodeRabbit) do; LOCAL backends (codex / agy) are run+posted synchronously
    # and have no requested edge — they override this False so base `detect`
    # never reads `requested_logins` for them.
    has_requested_edge: bool = True
    # Repo-relative path(s) of this reviewer's code-review instruction file(s).
    # Structure only: the adapter declares the location; whether content ships
    # there is a per-reviewer onboarding decision.
    instruction_files: tuple[str, ...] = ()

    def matches(self, login: str) -> bool:
        raise NotImplementedError

    def native_severity(self, body: str) -> Severity | None:
        """Map this reviewer's NATIVE severity format in a posted comment body
        to the shared 4-tier :class:`~shipit.finding.Severity` ladder, else None.

        The adapter rung of the severity precedence chain (ADR-0044): each app
        reviewer's adapter owns its native-format mapping (Gemini's
        Critical/High/Medium/Low badge, CodeRabbit's severity/kind markers), so
        the engine reads one ladder across reviewer kinds without ever
        branching on a name. Base: None — a reviewer with no native severity
        vocabulary (Copilot) contributes nothing here, and a LOCAL-agent
        reviewer's findings carry the machine marker (the chain's stronger
        rung) instead. None falls through to the chain's ``major`` fail-safe:
        an unmappable finding forces a round rather than slipping the Breaker.
        """
        return None

    def _rerun(self, ctx: ReadinessView) -> bool:
        """This reviewer's rerun policy for `ctx` (default False = review-once).

        rerun is read off this reviewer's Roster ENTRY (`ctx.roster`, loaded
        once at the verb boundary and threaded onto the context at the build
        site). False is the shipped default for EVERY reviewer (all reviewers
        are token-billed / cost a model run, so re-reviewing each push is
        explicit opt-in)."""
        return ctx.roster.entry(self.name).rerun

    def _window(self, ctx: ReadinessView) -> timedelta:
        """This reviewer's wait window — the per-reviewer `[reviewers]` `window`
        setting off its Roster entry, or the shipped 20m default. Read off the
        context like `_rerun`, so the engine never touches config; an absent
        setting falls back to `DEFAULT_WAIT_WINDOW`."""
        seconds = ctx.roster.entry(self.name).window_seconds
        return timedelta(seconds=seconds) if seconds else DEFAULT_WAIT_WINDOW

    def _requested_at(self, ctx: ReadinessView) -> str | None:
        """This reviewer's `review_requested` edge time (App side), matched off
        `ctx.requested_at` by login — the timestamp WS03 ages an App reviewer's
        wait window against. None when this reviewer has no recorded request edge:
        a LOCAL reviewer ages its check-run `started_at` instead (and overrides
        `funnel_state` to do so), and best-effort Gemini has no requested edge at
        all — neither appears in `requested_at`, so neither ages here."""
        for login, ts in ctx.requested_at.items():
            if self.matches(login):
                return ts
        return None

    def detect(self, ctx: ReadinessView) -> ReviewLifecycle:
        """Where this reviewer stands — rerun-aware, shared across adapters.

        The lifecycle depends on the reviewer's rerun flag:

          * rerun=False (default, review-once): a non-DISMISSED review by this
            reviewer on ANY commit of the PR reads DONE — it is NEVER stale
            after a push. The reviewer won't be asked to look again, so an
            earlier-head review still satisfies the requirement.
          * rerun=True (opt-in, head-strict): the review must be on the CURRENT
            head to count DONE; a review only on an older head is stale and the
            reviewer reads back as REQUESTED (needs re-request for the new head).

        When no review counts: REQUESTED if the reviewer is currently requested
        (only for adapters with a real requested edge — `has_requested_edge`),
        else NOT_REQUESTED. A DISMISSED review is a retracted verdict and never
        counts as done. Adapters differ only by `matches` / `has_requested_edge`
        / `requestable`, NOT by this detection algorithm (Gemini, which signals
        weakly and is not requestable, is the one exception and overrides this)."""
        candidates = (
            ctx.reviews_on_head() if self._rerun(ctx) else ctx.reviews_any_head()
        )
        if any(self.matches(r.author) and r.state != "DISMISSED" for r in candidates):
            return self._done_state(ctx)
        if self.has_requested_edge and any(
            self.matches(login) for login in ctx.requested_logins
        ):
            return ReviewLifecycle.REQUESTED
        return ReviewLifecycle.NOT_REQUESTED

    def request(self, pr: PrId, entry: RosterEntry | None = None) -> bool:
        """Request — or re-request, same call — this reviewer on `pr`.

        Returns True when a request was actually placed, False when the
        reviewer has no request mechanism (auto-triggering / best-effort
        backends). Re-request after a fixup push is not a separate verb:
        the state machine's never-requested vs stale-after-push distinction
        is a read-side concern (`state._has_stale_review`); the act is the
        same either way.

        `entry` is this reviewer's Roster entry (CLI01-WS04) — the request
        path passes it so per-reviewer settings arrive as a VALUE, never
        re-resolved from config here. Only the LOCAL-agent adapters read it
        (their `model` / `instructions` / `timeout` run options); the App
        adapters place a plain request edge and ignore it. `None` means
        all-defaults (an unconfigured reviewer).

        Placement only: True means the call was accepted, not that the
        `review_requested` edge exists — GitHub can silently drop the attach
        (release#614). The `pr review request` verb verifies the edge for
        every adapter that returns True, generically; False-returning
        (no-mechanism) adapters are never verified.
        """
        raise NotImplementedError

    def cancel(self, pr: PrId) -> bool:
        """Withdraw a pending review request on `pr`.

        Returns True when a request was withdrawn, False when there is no
        request mechanism to withdraw from (no-op backends).
        """
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        """The name to SHOW a human for this reviewer (defaults to the registry
        name). The local-agent adapters override it to their ``<agent>-local``
        funnel name, so a degraded annotation reads ``codex-local failed`` (the
        name the check run is published under), not the bare ``codex``."""
        return self.name

    def funnel_state(
        self, ctx: ReadinessView, lifecycle: ReviewLifecycle
    ) -> FunnelState:
        """This reviewer's normalized OBS04 funnel state (ADR-0006).

        The App/native side: an App reviewer (Copilot / CodeRabbit / Gemini) has no
        check-run breadcrumb, so its funnel view folds straight from the native
        `ReviewLifecycle` the engine already computed (passed in so `detect` is not
        re-run). The LOCAL-agent adapters override this to read their breadcrumb.

        Kept behind the adapter interface so the engine normalizes EVERY reviewer
        the same way — `status.reviewer_funnel[name].state` — without ever branching
        on a reviewer's name.

        WS03 ages the App side here: a REQUESTED reviewer (request edge placed, no
        review yet) silent past its wait window settles as TIMED_OUT, aged from its
        `review_requested` edge time (`ctx.requested_at`). Within the window it
        still holds; a reviewer with no recorded edge time (Gemini) never ages.
        """
        state = _LIFECYCLE_TO_FUNNEL[lifecycle]
        return _age_to_timeout(
            state, self._requested_at(ctx), self._window(ctx), ctx.now
        )

    def funnel_check(self, ctx: ReadinessView) -> ReviewFunnelCheck | None:
        """This reviewer's OBS02/ADR-0005 funnel check-run breadcrumb, if any.

        Base: ``None``. App/native reviewers (Copilot, CodeRabbit, Gemini) source
        their funnel from native GitHub signals — the ``review_requested`` edge +
        the review object — not from a shipit-authored check run, so they have no
        breadcrumb here. Only the LOCAL-agent adapters (codex / agy), which GitHub
        denies a native requested edge, override this to claim their
        ``review: <agent>-local`` run off ``ctx.review_funnel``.

        Keeping the reviewer→breadcrumb mapping behind the adapter interface is
        what lets the engine attach per-reviewer funnel state without ever
        branching on a reviewer's name — it just asks each adapter.
        """
        return None

    def authored_threads(self, ctx: ReadinessView) -> list[Thread]:
        """All threads (resolved or not) rooted in a comment by this reviewer."""
        return [t for t in ctx.threads if t.author and self.matches(t.author)]

    def open_threads(self, ctx: ReadinessView) -> list[Thread]:
        """Unresolved threads by this reviewer — the ones still needing action."""
        return [t for t in self.authored_threads(ctx) if not t.is_resolved]

    def _done_state(self, ctx: ReadinessView) -> ReviewLifecycle:
        return (
            ReviewLifecycle.DONE_COMMENTS
            if self.authored_threads(ctx)
            else ReviewLifecycle.DONE_CLEAN
        )


class CopilotAdapter(ReviewerAdapter):
    """Copilot posts a discrete review object on the PR head SHA.

    Copilot has a real `review_requested` edge and no observable mid-review
    signal, so it goes REQUESTED -> DONE. Whether an earlier-head review counts
    as done is the per-reviewer rerun policy (see base `detect`): review-once
    (default) counts any-head; rerun=True is head-strict (an earlier-head review
    is stale and the reviewer reads back REQUESTED for the new head).

    Copilot emits NO native severity vocabulary, so `native_severity` stays the
    base None (deliberate, ADR-0044): its findings resolve through the chain's
    ``major`` fail-safe — forcing a round rather than slipping the Breaker —
    correctable per finding via the write-once Severity override.
    """

    name = "copilot"
    requestable = True
    instruction_files = (".github/copilot-instructions.md",)

    def matches(self, login: str) -> bool:
        return "copilot" in login.lower()

    def request(self, pr: PrId, entry: RosterEntry | None = None) -> bool:
        # `gh pr edit --add-reviewer @copilot` — GraphQL with the bot's real
        # node_id (via gh.pr_edit_reviewer; the REST requested_reviewers
        # POST silently no-ops for Copilot). Re-request is the same call.
        gh.pr_edit_reviewer(pr, "@copilot")
        _log_request_transition(self.name, pr, "request placed")
        return True

    def cancel(self, pr: PrId) -> bool:
        gh.pr_edit_reviewer(pr, "@copilot", remove=True)
        _log_request_transition(self.name, pr, "request withdrawn")
        return True


class CodeRabbitAdapter(ReviewerAdapter):
    """CodeRabbit is a requestable GitHub App that posts a discrete review on the
    PR head SHA — structurally the same model as Copilot. It is being PILOTED on
    the phos-org repos (the only place the App is installed); a pilot repo opts
    in via the `[reviewers]` table in its `.shipit.toml`. It is NOT in the
    default required set: on a repo without the App, the request edge silently
    drops (#613-style) and a required reviewer would park every PR at
    REVIEWS_PENDING. Whether it blocks is a config decision, not an adapter
    property — this adapter only declares CodeRabbit *requestable* (it has a
    real request edge + the #614 attach-verification, so it is ELIGIBLE to be
    required wherever the App is installed).

    When a repo requires both Copilot and CodeRabbit, the policy is
    parallel-required, not fallback: each holds Ready, so a PR is reviewed only
    when BOTH have a fresh review on the current head. The accepted trade-off is
    availability — one required reviewer's outage holds Ready until it recovers —
    in exchange for always-on dual coverage and no single point of failure on
    review *quality*.

    Structurally identical to Copilot: a real `review_requested` edge and a
    discrete head-SHA review. Whether an earlier-head review counts as done is
    the per-reviewer rerun policy (base `detect`), not an adapter property. The
    request goes through `gh pr edit --add-reviewer` (the GraphQL path that
    resolves the App's real node id and creates a real `review_requested` edge)
    — so the generic #614 attach-verification in `pr review request` applies
    unchanged: a silently dropped attach fails loud.
    """

    name = "coderabbit"
    requestable = True
    instruction_files = (".coderabbit.yaml",)
    # The reviewer handle `gh pr edit --add-reviewer` resolves to the App's node
    # id. CodeRabbit's bot login on submitted reviews / pending requests is
    # `coderabbitai[bot]`; `matches` keys off the stable `coderabbit` substring.
    _REVIEWER_HANDLE = "coderabbitai[bot]"

    # CodeRabbit's native severity format → the shared ladder (ADR-0044): a
    # finding comment opens with either an explicit severity pill (`🔴 Critical`
    # / `🟠 Major` / `🟡 Minor`) or a kind marker (`_⚠️ Potential issue_` /
    # `_🛠️ Refactor suggestion_` / a `Nitpick` fold). Declaration ORDER is
    # precedence: an explicit pill beats the kind marker riding the same
    # comment. Matched case-insensitively as literal substrings; anything
    # outside this table is unmappable → the chain's `major` fail-safe.
    _SEVERITY_TOKENS: tuple[tuple[str, Severity], ...] = (
        ("🔴 critical", Severity.CRITICAL),
        ("🟠 major", Severity.MAJOR),
        ("🟡 minor", Severity.MINOR),
        ("potential issue", Severity.MAJOR),
        ("refactor suggestion", Severity.MINOR),
        ("nitpick", Severity.NIT),
    )

    def matches(self, login: str) -> bool:
        return "coderabbit" in login.lower()

    def native_severity(self, body: str) -> Severity | None:
        """CodeRabbit's native format mapped to the ladder — first token of
        `_SEVERITY_TOKENS` (precedence order) present in the body wins."""
        low = body.lower()
        for token, severity in self._SEVERITY_TOKENS:
            if token in low:
                return severity
        return None

    def request(self, pr: PrId, entry: RosterEntry | None = None) -> bool:
        # Same GraphQL add-reviewer path Copilot uses: it resolves the App's
        # real node id and creates a real review_requested edge (the REST
        # requested_reviewers POST silently no-ops for App reviewers).
        gh.pr_edit_reviewer(pr, self._REVIEWER_HANDLE)
        _log_request_transition(self.name, pr, "request placed")
        return True

    def cancel(self, pr: PrId) -> bool:
        gh.pr_edit_reviewer(pr, self._REVIEWER_HANDLE, remove=True)
        _log_request_transition(self.name, pr, "request withdrawn")
        return True


class GeminiAdapter(ReviewerAdapter):
    """Gemini signals weakly and is best-effort.

    The app triggers automatically (no discrete request event); an eyes reaction
    from the bot means it is looking; a review or issue comment means it is done.
    It goes over quota silently, so the state machine treats a timed-out Gemini
    as skipped rather than blocking Ready — that timing decision lives in the
    state machine, not here.

    Crucially, **Gemini reviews a PR once and does not re-review pushes** — so a
    review on *any* commit of this PR counts as done, unlike Copilot's
    head-strict model. (The eyes reaction is not commit-scoped and lingers after
    the review, so a fixup that creates a new head would otherwise read as a
    fresh "in_progress" forever.) This per-reviewer difference is exactly what
    the adapter layer exists to hold.
    """

    name = "gemini"
    requestable = False  # auto-triggers; no request edge, so never a required blocker
    has_requested_edge = False  # no requested edge; overrides detect entirely anyway
    # Declared location only — no content shipped until Gemini is onboarded
    # as a required reviewer.
    instruction_files = (".gemini/styleguide.md",)

    # Gemini Code Assist's native 4-level priority → the shared ladder
    # (ADR-0044): Critical/High/Medium/Low, one level per finding comment.
    _SEVERITY_MAP: dict[str, Severity] = {
        "critical": Severity.CRITICAL,
        "high": Severity.MAJOR,
        "medium": Severity.MINOR,
        "low": Severity.NIT,
    }
    # The level rides the comment as a severity badge image whose alt text IS
    # the native token: `![critical](https://…/critical.svg)`. Built from the
    # table so the two can never disagree; anything outside it is unmappable →
    # the chain's `major` fail-safe.
    _BADGE_RE = re.compile(
        r"!\[(" + "|".join(map(re.escape, _SEVERITY_MAP)) + r")\]\(",
        re.IGNORECASE,
    )

    def matches(self, login: str) -> bool:
        return "gemini" in login.lower()

    def native_severity(self, body: str) -> Severity | None:
        """Gemini's Critical/High/Medium/Low badge mapped to the ladder — the
        FIRST severity badge in the body decides."""
        match = self._BADGE_RE.search(body)
        return self._SEVERITY_MAP[match.group(1).lower()] if match else None

    def request(self, pr: PrId, entry: RosterEntry | None = None) -> bool:
        # The Gemini app auto-triggers on PR open; there is no request
        # mechanism, and it is best-effort anyway — a no-op, not an error.
        # A mechanic, not a milestone: no edge changed, so it records at DEBUG.
        logger.debug(
            "reviewer %s: no request mechanism (auto-triggers) — no-op on pr#%s",
            self.name,
            pr.number,
            extra={"reviewer": self.name, "pr": pr.number},
        )
        return False

    def cancel(self, pr: PrId) -> bool:
        return False

    def detect(self, ctx: ReadinessView) -> ReviewLifecycle:
        # Any-head, not head-strict: Gemini won't review the new head again.
        # A DISMISSED review is retracted, so it doesn't count as done.
        if any(self.matches(r.author) and r.state != "DISMISSED" for r in ctx.reviews):
            return self._done_state(ctx)
        if any(
            self.matches((c.get("user") or {}).get("login", ""))
            for c in ctx.issue_comments
        ):
            return ReviewLifecycle.DONE_COMMENTS
        if self._is_looking(ctx):
            return ReviewLifecycle.IN_PROGRESS
        return ReviewLifecycle.NOT_REQUESTED

    def _is_looking(self, ctx: ReadinessView) -> bool:
        return any(
            r.get("content") == "eyes"
            and self.matches((r.get("user") or {}).get("login", ""))
            for r in ctx.reactions
        )


class _LocalReviewAdapter(ReviewerAdapter):
    """A LOCAL review backend (codex / agy) surfaced as a reviewer adapter.

    Unlike the GitHub-App reviewers (Copilot / CodeRabbit), these reviewers do
    not exist as an installed App that GitHub auto-triggers or that an
    `--add-reviewer` edge addresses. The review is GENERATED locally — the agent
    CLI runs in the PR checkout — and POSTED as the agent's own bot identity. In
    release that runs through its `review` service's run-and-post, so `request`
    is SYNCHRONOUS (it runs the review + posts it now); there is no
    `review_requested` edge to place or withdraw, so `cancel` is a no-op.

    In shipit the local-agent review *execution* engine lives in
    `shipit.review` (PRF01-WS07): this adapter's `request` LAZILY imports
    `shipit.review.service`. Since OBS03 the run is ASYNC — `request` detaches the
    agent run and returns IN-FLIGHT rather than blocking for the length of a model
    run (see `request` for the inverted contract). The import is lazy so the
    optional `review` extra (pyjwt) is only pulled in when a local review is
    actually requested. The DETECTION path is fully intact — `detect` reads an
    existing local-agent review exactly as in release.

    Detection is the shared rerun-aware base `detect`: review-once (default)
    counts a non-DISMISSED review by `matches` on any head as done; rerun=True
    is head-strict. There is no requested edge for a local reviewer
    (`has_requested_edge = False`), so `requested_logins` is never consulted —
    either a counting review exists (done) or the reviewer hasn't run
    (NOT_REQUESTED). The bot login is matched on BOTH the GitHub App `[bot]` suffix
    AND a stable slug fragment (`codex-review` / `agy-review`) — so a future
    prefix (`adr-codex-review[bot]`) still matches, but a bare human login that
    merely contains `codex` / `agy` (e.g. `codexdev`, `agytron`) does NOT, which
    would otherwise misread as a bot review and falsely report DONE. The
    user-specific app-name prefix (e.g. `adr-`) is never hardcoded.
    """

    requestable = True
    # No `review_requested` edge: the review is generated + posted synchronously,
    # so base `detect` must never consult `requested_logins` for these.
    has_requested_edge = False
    # The ONE agent-backend identity this adapter fronts (ADR-0025) — set by each
    # subclass to a registry entry. Every derived name below (`name`, the login
    # slug fragment, the funnel reviewer name) reads off it, and `request` threads
    # it into `shipit.review.service`, so the funnel path never re-composes or
    # re-parses an identity string.
    backend: _agent_backend.Backend

    @property
    def bot_slug_fragment(self) -> str:
        """The stable bot-login slug fragment `matches` requires (with the `[bot]`
        suffix) — the registry's `<agent>-review` alias, never composed here."""
        return self.backend.bot_slug_fragment

    def matches(self, login: str) -> bool:
        # Require the GitHub App `[bot]` SUFFIX (not just the substring
        # anywhere) AND the stable slug fragment. `adr-codex-review[bot]` /
        # `adr-agy-review[bot]` end with `[bot]`, so they still match; a login
        # that merely contains `[bot]` mid-string (e.g. `x[bot]y`) does not.
        low = login.lower()
        return low.endswith("[bot]") and self.bot_slug_fragment in low

    def request(self, pr: PrId, entry: RosterEntry | None = None) -> bool:
        """DETACH a local-agent review and return IN-FLIGHT (OBS03).

        Fire-and-forget: this does the cheap, synchronous work — resolve the PR's
        ``(repo, head_sha)``, open the OBS02 ``in_progress`` funnel check run — then
        spawns a DETACHED child process that runs the agent over the PR diff, posts
        the verdict as the bot, and closes that SAME check run to its terminal
        state. It returns immediately (``True`` = in-flight); the OUTCOME is read
        LATER from the PR (the funnel check run + the posted review), never from
        this return. This inverts the pre-OBS03 contract — there is no longer a
        blocking model run inside `request`. There is still no `review_requested`
        edge, so a local reviewer is never edge-verified.

        `shipit.review.service` is imported LAZILY here, so the optional `review`
        extra (pyjwt) is only pulled in when a local review is actually requested —
        the detection path and every non-local reviewer stay free of that
        dependency. The agent's per-reviewer `model` / `instructions` / `timeout`
        (the `[reviewers]` options) are read OFF this reviewer's Roster `entry`
        (CLI01-WS04) — a value the caller loaded once at the verb boundary,
        never a config re-read here — and threaded to the detached child.

        Any failure in the SYNCHRONOUS part — a `gh`/auth failure resolving the PR,
        a spawn failure — is normalized to `PrStateError`, the one error type the
        `pr review request` CLI renders as a clean message + exit 1, so a request
        never crashes with a raw traceback. An EXPECTED, operator-actionable auth
        failure (`ReviewAuthError` — e.g. the optional `review` extra / pyjwt is
        absent) already carries its own install hint, so it is surfaced as that
        clean message WITHOUT the ERROR-level traceback spray reserved for a
        genuinely-unexpected crash. (A failure INSIDE the detached child resolves
        to a visible failed/timed-out check run on the PR, not to this return —
        that is the whole point of detaching.)
        """
        # Lazy: keep the optional `review`/pyjwt import off the detection path
        # and out of every non-local reviewer. `review` never imports `prstate`,
        # so this one-way edge has no cycle.
        try:
            from ..review import service
        except ImportError as exc:  # pragma: no cover - only when the extra is absent
            raise PrStateError(
                f"{self.funnel_reviewer_name()} review needs the optional `review` "
                f"extra "
                f"(pyjwt): install shipit with `pip install 'shipit[review]'`. ({exc})"
            ) from exc

        entry = entry if entry is not None else RosterEntry(name=self.name)
        run_kwargs: dict[str, object] = {"as_app": True}
        if entry.model is not None:
            run_kwargs["model"] = entry.model
        if entry.instructions is not None:
            run_kwargs["instructions_path"] = entry.instructions
        if entry.timeout is not None:
            run_kwargs["timeout"] = entry.timeout

        # Lazy, same reason as `service` above: `ReviewAuthError` lives in the
        # optional `review` package, so name it only here — where that package has
        # just imported cleanly — not at module top on the always-loaded path.
        from ..review.ghauth import ReviewAuthError

        try:
            started = service.start_detached_review(self.backend, pr, **run_kwargs)
        except ReviewAuthError as exc:
            # An EXPECTED, operator-actionable auth failure (e.g. the `review`
            # extra / pyjwt is absent) — it already carries a clean install hint.
            # Surface that hint as the CLI-clean PrStateError; NO traceback spray,
            # so the operator reads the fix, not a raw `ModuleNotFoundError` dump.
            # The LOG RECORD carries no exc_info and no exception detail — just a
            # DEBUG breadcrumb of the mechanic; this is the EXPECTED path, so the
            # record stays quiet. The RAISED PrStateError below deliberately
            # interpolates `{exc}` — the exc's actionable install hint belongs in
            # the message the operator reads, just not on the log record.
            logger.debug(
                "reviewer %s: local review auth failed on pr#%s (expected, "
                "surfaced as a clean error)",
                self.display_name,
                pr.number,
                extra={"reviewer": self.display_name, "pr": pr.number},
            )
            # `from None` (not `from exc`): the install hint already rides into the
            # message via `{exc}`, so severing the chain loses nothing the operator
            # needs while making the no-traceback contract airtight — an uncaught or
            # downstream-logged raise can never spray the `ModuleNotFoundError` cause.
            raise PrStateError(
                f"{self.funnel_reviewer_name()} review failed on #{pr.number}: {exc}"
            ) from None
        except Exception as exc:
            # A propagating failure (glassbox spray): the request act died before
            # the review could even detach — record it at ERROR with the exception
            # attached, then normalize to the one error the CLI renders cleanly.
            logger.error(
                "reviewer %s: local review request failed on pr#%s",
                self.display_name,
                pr.number,
                exc_info=True,
                extra={"reviewer": self.display_name, "pr": pr.number},
            )
            if isinstance(exc, PrStateError):
                raise
            raise PrStateError(
                f"{self.funnel_reviewer_name()} review failed on #{pr.number}: {exc}"
            ) from exc
        if started:
            # A fresh child was detached: a real request edge to narrate.
            _log_request_transition(self.display_name, pr, "detached local review")
        else:
            # Reconciled against an already in-flight run (idempotent re-request):
            # nothing transitioned, so it is a DEBUG mechanic, not an INFO edge.
            logger.debug(
                "reviewer %s: re-request reconciled against an in-flight run on "
                "pr#%s — no new detach",
                self.display_name,
                pr.number,
                extra={"reviewer": self.display_name, "pr": pr.number},
            )
        return True

    def cancel(self, pr: PrId) -> bool:
        """No-op: a posted review can't be withdrawn.

        A local reviewer leaves a real, submitted review rather than a pending
        `review_requested` edge — there is nothing to cancel. Returns False, the
        same shape a no-mechanism backend uses.
        """
        return False

    def funnel_reviewer_name(self) -> str:
        """The funnel reviewer name (`codex-local`) — the suffix the OBS02 check
        run is named after (`review: <agent>-local`, ADR-0005). The registry's
        `check_run_name` alias (ADR-0025), read off the backend identity — never
        composed here — and available without importing the optional `review`
        extra."""
        return self.backend.check_run_name

    @property
    def display_name(self) -> str:
        # A local reviewer is shown under the name its check run is published as
        # (`codex-local`), so a degraded annotation matches the PR's funnel run.
        return self.funnel_reviewer_name()

    def funnel_state(
        self, ctx: ReadinessView, lifecycle: ReviewLifecycle
    ) -> FunnelState:
        """A local-agent reviewer's funnel state, read from its breadcrumb (ADR-0006).

        Two sources fold here, in priority order:

          1. A POSTED review wins outright. If `detect` already found a counting
             review by this bot (`lifecycle` is DONE), the review LANDED → POSTED,
             regardless of the breadcrumb. This is the load-bearing
             **provisioning-as-flake** path (ADR-0005/0006): a consumer whose review
             App still lacks ``checks:write`` opens NO check run, but the review
             *still posts* — so it reads POSTED → settled, never blocked. The salvage
             COMMENT (#76) posts a real review too, so a content-but-unparseable run
             also lands here as POSTED, not degraded.

          2. Otherwise the check-run breadcrumb decides
             (`_funnel_state_from_check`): in-flight / failed / empty / timed-out.

        The genuinely-no-outcome case — NO breadcrumb AND no posted review — is, in
        a pure snapshot, INDISTINGUISHABLE from never-requested (provisioning
        failure leaves no artifact at all; ADR-0005 rejected comment markers, so
        there is nothing else to read). It maps to NEVER_REQUESTED, which HOLDS at
        reviews-pending with an actionable *request* next-step — NEVER a BLOCKED
        terminal state. So "not provisioned" still never *blocks* the PR (ADR-0006's
        load-bearing guarantee): the realistic unprovisioned run settles via path 1,
        and the pathological double-failure (no breadcrumb AND no posted review)
        holds-with-an-action rather than parking silently. Marking an ABSENT signal
        as degraded would instead silently skip a reviewer that simply has not run
        yet (breaking the start-the-loop story), so the engine does not — the
        not-required-until-provisioned rollout policy (INS01) owns that residual.
        """
        if lifecycle in (ReviewLifecycle.DONE_CLEAN, ReviewLifecycle.DONE_COMMENTS):
            return FunnelState.POSTED
        check = self.funnel_check(ctx)
        if check is None:
            return FunnelState.NEVER_REQUESTED
        # WS03 ages the LOCAL side here: an IN_FLIGHT run (status not yet COMPLETED)
        # silent past its wait window settles as TIMED_OUT, aged from the run's OWN
        # `started_at` (ADR-0005's load-bearing timestamp). A terminal breadcrumb
        # (posted / failed / empty / producer-recorded timeout) is already settled
        # and `_age_to_timeout` returns it unchanged; within the window the run holds.
        return _age_to_timeout(
            _funnel_state_from_check(check),
            check.started_at,
            self._window(ctx),
            ctx.now,
        )

    def funnel_check(self, ctx: ReadinessView) -> ReviewFunnelCheck | None:
        """This local reviewer's funnel breadcrumb off `ctx.review_funnel`.

        Matches the `review: <agent>-local` check run by its funnel reviewer name.
        If several runs carry the name (a re-request that opened a second run, or
        a stale earlier-head run), the one with the latest `started_at` is the live
        one — we select on that timestamp rather than rollup list order, which is
        not a documented recency contract (see `_funnel_recency_key`); rollup
        position only breaks an exact-timestamp tie. `None` when no funnel run is
        present (the breadcrumb absent: never run, or the App still lacks
        `checks:write` before the ADR-0005 re-grant — read as degraded downstream,
        never as a block)."""
        target = self.funnel_reviewer_name()
        matches = [c for c in ctx.review_funnel if c.reviewer == target]
        if not matches:
            return None
        return max(
            enumerate(matches), key=lambda ic: (_funnel_recency_key(ic[1]), ic[0])
        )[1]


class CodexAdapter(_LocalReviewAdapter):
    """Codex — a LOCAL review backend posted as the `adr-codex-review[bot]`
    identity. See :class:`_LocalReviewAdapter` for the synchronous-request /
    no-cancel / head-strict contract."""

    # ONE registry entry (ADR-0025) — the adapter fronts this identity; the name
    # (and, via the base class, the login slug fragment + funnel reviewer name)
    # derive from it, so the funnel axis and the launch axis share one definition
    # of the codex identity — no duplicated alias tables.
    backend = _agent_backend.CODEX
    name = backend.funnel_agent or backend.name
    instruction_files = (".github/codex-review-instructions.md",)


class AgyAdapter(_LocalReviewAdapter):
    """Agy — a LOCAL review backend posted as the `adr-agy-review[bot]` identity.

    Matches on the `agy-review` slug fragment + `[bot]` suffix (NOT `gemini`:
    the bot login is `adr-agy-review`, and `gemini` belongs to the separate
    auto-triggering GeminiAdapter). See :class:`_LocalReviewAdapter` for the
    request/cancel/detect contract."""

    # ONE registry entry (ADR-0025): the adapter's registry name is the backend's
    # funnel-agent alias (`agy`); every other derived name reads off the identity.
    backend = _agent_backend.ANTIGRAVITY
    name = backend.funnel_agent or backend.name
    instruction_files = (".github/agy-review-instructions.md",)


# The adapter CATALOG: every reviewer the engine knows how to read/request. This
# is the registry (#558) — adding a backend is adding an adapter here. WHICH of
# these hold Ready is NOT decided here: that is the config knob in
# `reviewers_config` (release#622), default [copilot] (coderabbit is a
# phos-org pilot, opted in per-repo). codex / agy are LOCAL review backends
# (generated + posted locally), unified under the same adapter interface.
REGISTRY: list[ReviewerAdapter] = [
    CopilotAdapter(),
    CodeRabbitAdapter(),
    GeminiAdapter(),
    CodexAdapter(),
    AgyAdapter(),
]


def required_adapters(roster: Roster) -> list[ReviewerAdapter]:
    """Map a Roster's required reviewers → their registry adapters, config order.

    The required SET is data (the Roster, loaded once at a verb boundary by
    `reviewers_config.load_roster`), not the registry's structure — so
    swapping/re-ordering required reviewers is a one-line config edit. There is
    no module-global cache anymore (CLI01-WS04): the caller holds the Roster
    and passes it down, so this is a pure name→adapter mapping. The loader
    guarantees every required name resolves; the explicit guard turns any
    future registry/validation mismatch into a loud error instead of a None
    leaking to callers."""
    adapters: list[ReviewerAdapter] = []
    for name in roster.required_names:
        adapter = by_name(name)
        if adapter is None:  # unreachable post-load_roster — fail loud if it isn't
            raise PrStateError(f"required reviewer {name!r} has no adapter")
        adapters.append(adapter)
    return adapters


def by_name(name: str) -> ReviewerAdapter | None:
    """Look an adapter up by its registry name (the `--reviewer` selector)."""
    for r in REGISTRY:
        if r.name == name.lower():
            return r
    return None


def resolve_reviewer(name: str) -> ReviewerAdapter:
    """Resolve a ``--reviewer`` selector to its ONE adapter, or refuse loud.

    The reviewer-name resolution the `pr review request` scope uses (CLI01-WS03
    promoted it out of the verb: which adapter a name means is registry
    knowledge, not click glue). Two spellings resolve:

      * the adapter registry name (``copilot``, ``codex``, …) via :func:`by_name`;
      * the PRD/glossary spelling of a local-agent reviewer (``codex-local`` /
        ``agy-local``) — a REGISTRY LOOKUP, not a string parse (COR02-WS03): the
        name resolves through :func:`shipit.agent.backend.by_check_run_name` —
        the inverse of the registry's ``check_run_name`` alias — so only a real
        funnel backend's reviewer name is reachable this way (``copilot-local``
        matches no registry entry and does not alias to ``copilot``: the alias
        names the local-agent reviewer family specifically).

    An unknown name raises :class:`~shipit.prstate.errors.PrStateError` naming
    the known reviewers — a typo never silently drops a request.
    """
    adapter = by_name(name)
    if adapter is None:
        try:
            backend = _agent_backend.by_check_run_name(name)
        except KeyError:
            backend = None
        if backend is not None:
            adapter = by_name(backend.funnel_agent or backend.name)
    if adapter is None:
        known = ", ".join(r.name for r in REGISTRY)
        raise PrStateError(f"unknown reviewer {name!r} (known: {known})")
    return adapter
