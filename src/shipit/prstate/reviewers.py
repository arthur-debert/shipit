"""Reviewer adapters. See docs/adr/0006-readiness-with-degraded-reviewers.md."""

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
from .roster import ReviewPolicy, Roster, RosterEntry

logger = logging.getLogger("shipit.prstate")


def _log_request_transition(reviewer: str, pr: PrId, transition: str) -> None:
    logger.info(
        "reviewer %s: %s on pr#%s",
        reviewer,
        transition,
        pr.number,
        extra={"reviewer": reviewer, "pr": pr.number, "transition": transition},
    )


# Default wait window past which a still-working required reviewer settles as
# TIMED_OUT; overridable per-reviewer via the `[reviewers]` `window` option.
DEFAULT_WAIT_WINDOW = timedelta(minutes=20)

# Every other state is terminal or has no timestamp to age against.
_AGEABLE = (FunnelState.IN_FLIGHT, FunnelState.REQUESTED)


def _age_to_timeout(
    state: FunnelState,
    request_at: str | None,
    window: timedelta,
    now: datetime | None,
) -> FunnelState:
    if state not in _AGEABLE:
        return state
    if not request_at or now is None:
        return state
    if now - datetime.fromisoformat(request_at) > window:
        return FunnelState.TIMED_OUT
    return state


_LIFECYCLE_TO_FUNNEL: dict[ReviewLifecycle, FunnelState] = {
    ReviewLifecycle.DONE_CLEAN: FunnelState.POSTED,
    ReviewLifecycle.DONE_COMMENTS: FunnelState.POSTED,
    ReviewLifecycle.IN_PROGRESS: FunnelState.IN_FLIGHT,
    ReviewLifecycle.REQUESTED: FunnelState.REQUESTED,
    ReviewLifecycle.NOT_REQUESTED: FunnelState.NEVER_REQUESTED,
}


def _funnel_state_from_check(check: ReviewFunnelCheck) -> FunnelState:
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
    if not check.started_at:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(check.started_at)


class ReviewerAdapter:
    """Base adapter: the read side (`matches`, `detect`) and the act side."""

    name: str = ""
    # A non-requestable reviewer can never be a required, blocking one.
    requestable: bool = False
    # Whether `requested_logins` is meaningful for this reviewer.
    has_requested_edge: bool = True
    instruction_files: tuple[str, ...] = ()
    # What an unclassified finding resolves to; None rides the `major` fail-safe.
    unclassified_severity: Severity | None = None

    def matches(self, login: str) -> bool:
        raise NotImplementedError

    def native_severity(self, body: str) -> Severity | None:
        return None

    def _rerun(self, ctx: ReadinessView) -> bool:
        """This reviewer's rerun policy: True (the default) is head-strict."""
        return ctx.roster.entry(self.name).rerun

    def _window(self, ctx: ReadinessView) -> timedelta:
        seconds = ctx.roster.entry(self.name).window_seconds
        return timedelta(seconds=seconds) if seconds else DEFAULT_WAIT_WINDOW

    def _requested_at(self, ctx: ReadinessView) -> str | None:
        for login, ts in ctx.requested_at.items():
            if self.matches(login):
                return ts
        return None

    def detect(self, ctx: ReadinessView) -> ReviewLifecycle:
        """Where this reviewer stands; head-strict under rerun, else any-head."""
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

    def request(
        self,
        pr: PrId,
        entry: RosterEntry | None = None,
        policy: ReviewPolicy | None = None,
    ) -> bool:
        """Request this reviewer on `pr`; False when it has no mechanism."""
        raise NotImplementedError

    def cancel(self, pr: PrId) -> bool:
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        return self.name

    def funnel_state(
        self, ctx: ReadinessView, lifecycle: ReviewLifecycle
    ) -> FunnelState:
        state = _LIFECYCLE_TO_FUNNEL[lifecycle]
        return _age_to_timeout(
            state, self._requested_at(ctx), self._window(ctx), ctx.now
        )

    def funnel_check(self, ctx: ReadinessView) -> ReviewFunnelCheck | None:
        return None

    def authored_threads(self, ctx: ReadinessView) -> list[Thread]:
        return [t for t in ctx.threads if t.author and self.matches(t.author)]

    def open_threads(self, ctx: ReadinessView) -> list[Thread]:
        return [t for t in self.authored_threads(ctx) if not t.is_resolved]

    def _done_state(self, ctx: ReadinessView) -> ReviewLifecycle:
        return (
            ReviewLifecycle.DONE_COMMENTS
            if self.authored_threads(ctx)
            else ReviewLifecycle.DONE_CLEAN
        )


class CopilotAdapter(ReviewerAdapter):
    """Copilot posts a discrete review object on the PR head SHA."""

    name = "copilot"
    requestable = True
    instruction_files = (".github/copilot-instructions.md",)
    unclassified_severity = Severity.MINOR

    def matches(self, login: str) -> bool:
        return "copilot" in login.lower()

    def request(
        self,
        pr: PrId,
        entry: RosterEntry | None = None,
        policy: ReviewPolicy | None = None,
    ) -> bool:
        # The REST requested_reviewers POST silently no-ops for Copilot.
        gh.pr_edit_reviewer(pr, "@copilot")
        _log_request_transition(self.name, pr, "request placed")
        return True

    def cancel(self, pr: PrId) -> bool:
        gh.pr_edit_reviewer(pr, "@copilot", remove=True)
        _log_request_transition(self.name, pr, "request withdrawn")
        return True


class CodeRabbitAdapter(ReviewerAdapter):
    """CodeRabbit — a requestable App posting a discrete head-SHA review."""

    name = "coderabbit"
    requestable = True
    instruction_files = (".coderabbit.yaml",)
    _REVIEWER_HANDLE = "coderabbitai[bot]"

    # Matched case-insensitively as substrings; declaration order is precedence.
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
        low = body.lower()
        for token, severity in self._SEVERITY_TOKENS:
            if token in low:
                return severity
        return None

    def request(
        self,
        pr: PrId,
        entry: RosterEntry | None = None,
        policy: ReviewPolicy | None = None,
    ) -> bool:
        # The REST requested_reviewers POST silently no-ops for App reviewers.
        gh.pr_edit_reviewer(pr, self._REVIEWER_HANDLE)
        _log_request_transition(self.name, pr, "request placed")
        return True

    def cancel(self, pr: PrId) -> bool:
        gh.pr_edit_reviewer(pr, self._REVIEWER_HANDLE, remove=True)
        _log_request_transition(self.name, pr, "request withdrawn")
        return True


class GeminiAdapter(ReviewerAdapter):
    """Gemini — best-effort, auto-triggering, and weakly signalled."""

    name = "gemini"
    requestable = False
    has_requested_edge = False
    instruction_files = (".gemini/styleguide.md",)

    _SEVERITY_MAP: dict[str, Severity] = {
        "critical": Severity.CRITICAL,
        "high": Severity.MAJOR,
        "medium": Severity.MINOR,
        "low": Severity.NIT,
    }
    # Anchoring on the `codereviewagent/` URL segment keeps an unrelated
    # image that merely shares an alt text from reading as a badge.
    _BADGE_RE = re.compile(
        r"!\[(" + "|".join(map(re.escape, _SEVERITY_MAP)) + r")\]"
        r"\([^)]*codereviewagent/[^)]*\)",
        re.IGNORECASE,
    )

    def matches(self, login: str) -> bool:
        return "gemini" in login.lower()

    def native_severity(self, body: str) -> Severity | None:
        match = self._BADGE_RE.search(body)
        return self._SEVERITY_MAP[match.group(1).lower()] if match else None

    def request(
        self,
        pr: PrId,
        entry: RosterEntry | None = None,
        policy: ReviewPolicy | None = None,
    ) -> bool:
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
    """A local review backend (codex / agy) surfaced as a reviewer adapter."""

    requestable = True
    has_requested_edge = False
    backend: _agent_backend.Backend

    @property
    def bot_slug_fragment(self) -> str:
        return self.backend.bot_slug_fragment

    def matches(self, login: str) -> bool:
        # Requiring both keeps a human login containing `codex` / `agy` out.
        low = login.lower()
        return low.endswith("[bot]") and self.bot_slug_fragment in low

    def request(
        self,
        pr: PrId,
        entry: RosterEntry | None = None,
        policy: ReviewPolicy | None = None,
    ) -> bool:
        """Detach a local-agent review; the outcome is read later off the PR."""
        # Lazy so the review engine's import cost stays off the detection path.
        from ..review import service

        entry = entry if entry is not None else RosterEntry(name=self.name)
        run_kwargs: dict[str, object] = {"as_app": True}
        if entry.model is not None:
            run_kwargs["model"] = entry.model
        if entry.instructions is not None:
            run_kwargs["instructions_path"] = entry.instructions
        if entry.timeout is not None:
            run_kwargs["timeout"] = entry.timeout
        if entry.dimensions is not None:
            run_kwargs["dimensions"] = entry.dimensions
        if policy is not None:
            if policy.calibrator is not None:
                run_kwargs["calibrator"] = policy.calibrator
            if policy.nit_cap is not None:
                run_kwargs["nit_cap"] = policy.nit_cap

        from ..review.ghauth import ReviewAuthError

        try:
            started = service.start_detached_review(self.backend, pr, **run_kwargs)
        except ReviewAuthError as exc:
            # An expected auth failure already carries its own remedy.
            logger.debug(
                "reviewer %s: local review auth failed on pr#%s (expected, "
                "surfaced as a clean error)",
                self.display_name,
                pr.number,
                extra={"reviewer": self.display_name, "pr": pr.number},
            )
            # `from None`: the remedy already rides into the message.
            raise PrStateError(
                f"{self.funnel_reviewer_name()} review failed on #{pr.number}: {exc}"
            ) from None
        except Exception as exc:
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
            _log_request_transition(self.display_name, pr, "detached local review")
        else:
            logger.debug(
                "reviewer %s: re-request reconciled against an in-flight run on "
                "pr#%s — no new detach",
                self.display_name,
                pr.number,
                extra={"reviewer": self.display_name, "pr": pr.number},
            )
        return True

    def cancel(self, pr: PrId) -> bool:
        """No-op: a posted review can't be withdrawn."""
        return False

    def funnel_reviewer_name(self) -> str:
        return self.backend.check_run_name

    @property
    def display_name(self) -> str:
        return self.funnel_reviewer_name()

    def funnel_state(
        self, ctx: ReadinessView, lifecycle: ReviewLifecycle
    ) -> FunnelState:
        """A posted review wins; else the breadcrumb, and no signal never blocks."""
        if lifecycle in (ReviewLifecycle.DONE_CLEAN, ReviewLifecycle.DONE_COMMENTS):
            return FunnelState.POSTED
        check = self.funnel_check(ctx)
        if check is None:
            return FunnelState.NEVER_REQUESTED
        return _age_to_timeout(
            _funnel_state_from_check(check),
            check.started_at,
            self._window(ctx),
            ctx.now,
        )

    def funnel_check(self, ctx: ReadinessView) -> ReviewFunnelCheck | None:
        target = self.funnel_reviewer_name()
        matches = [c for c in ctx.review_funnel if c.reviewer == target]
        if not matches:
            return None
        return max(
            enumerate(matches), key=lambda ic: (_funnel_recency_key(ic[1]), ic[0])
        )[1]


class CodexAdapter(_LocalReviewAdapter):
    backend = _agent_backend.CODEX
    name = backend.funnel_agent or backend.name
    instruction_files = (".github/codex-review-instructions.md",)


class AgyAdapter(_LocalReviewAdapter):
    backend = _agent_backend.ANTIGRAVITY
    name = backend.funnel_agent or backend.name
    instruction_files = (".github/agy-review-instructions.md",)


# Which of these hold Ready is a `reviewers_config` decision, not a property here.
REGISTRY: list[ReviewerAdapter] = [
    CopilotAdapter(),
    CodeRabbitAdapter(),
    GeminiAdapter(),
    CodexAdapter(),
    AgyAdapter(),
]


def required_adapters(roster: Roster) -> list[ReviewerAdapter]:
    adapters: list[ReviewerAdapter] = []
    for name in roster.required_names:
        adapter = by_name(name)
        if adapter is None:  # unreachable post-load_roster — fail loud if it isn't
            raise PrStateError(f"required reviewer {name!r} has no adapter")
        adapters.append(adapter)
    return adapters


def by_name(name: str) -> ReviewerAdapter | None:
    for r in REGISTRY:
        if r.name == name.lower():
            return r
    return None


def resolve_reviewer(name: str) -> ReviewerAdapter:
    """Resolve a registry name or an ``<agent>-local`` name, else raise."""
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
